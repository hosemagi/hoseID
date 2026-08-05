"""Video probing and frame sampling.

Decoding goes through ffmpeg/ffprobe via subprocess rather than PyAV or opencv. ffmpeg 8.1.1 is
already installed system-wide, handles whatever containers Arlo and Reveal emit, and keeps this
package dependency-free -- which matters because `hoseid` is installed into both ML venvs and
must not drag a compiled decoder into either.

Sampled frames are temporary by design. Only the selected frame's crop persists (invariant 2:
the derived layer is regenerable, and intermediate frames are not worth storing).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, asdict
from pathlib import Path

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"

VIDEO_SUFFIXES = {".mp4", ".avi", ".mov", ".mkv", ".m4v"}


class VideoError(RuntimeError):
    """Probing or decoding failed."""


@dataclass(frozen=True)
class VideoMeta:
    duration_s: float
    fps: float
    frame_count: int
    width: int | None
    height: int | None


@dataclass(frozen=True)
class SamplingPolicy:
    """Recorded in run provenance so a later re-run at different density is comparable.

    `nominal_fps` is the policy; `effective_fps` is what was actually used for a given clip.

    When a clip is long enough that `nominal_fps` would exceed `max_frames`, we spread the frame
    budget evenly across the FULL clip rather than truncating at the cap. Truncating would mean a
    30 s clip is only examined for its first 20 s, so an animal appearing later produces no
    detection at all -- a systematic bias toward the start of the clip, and a strictly worse
    lower bound (see invariant 7). Even spreading costs exactly the same number of decoded frames,
    which is the entire stated rationale for having a cap.
    """
    version: str = "1"
    nominal_fps: float = 2.0
    max_frames: int = 40

    def effective_fps(self, duration_s: float) -> float:
        if duration_s <= 0:
            return self.nominal_fps
        if duration_s * self.nominal_fps <= self.max_frames:
            return self.nominal_fps
        return self.max_frames / duration_s

    def as_provenance(self, duration_s: float | None = None) -> dict:
        d = asdict(self)
        if duration_s is not None:
            d["effective_fps"] = round(self.effective_fps(duration_s), 4)
        return d


DEFAULT_POLICY = SamplingPolicy()


def is_video(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_SUFFIXES


def _run(cmd: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    except FileNotFoundError as e:
        raise VideoError(f"ffmpeg/ffprobe not found: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise VideoError(f"timed out: {' '.join(cmd[:3])}") from e


def probe(path: Path) -> VideoMeta:
    """Read duration, frame rate and frame count.

    Trail-cam clips frequently lack a container-level nb_frames, so frame_count falls back to
    duration x fps. It is recorded for provenance, not relied upon for sampling.
    """
    cmd = [FFPROBE, "-v", "error", "-print_format", "json",
           "-show_streams", "-select_streams", "v:0", "-show_format", str(path)]
    p = _run(cmd, timeout=60)
    if p.returncode != 0:
        raise VideoError(f"ffprobe failed on {path.name}: {p.stderr.decode()[:200]}")
    try:
        data = json.loads(p.stdout)
        st = (data.get("streams") or [{}])[0]
        fmt = data.get("format") or {}
    except (json.JSONDecodeError, IndexError) as e:
        raise VideoError(f"unparseable ffprobe output for {path.name}: {e}") from e

    fps = 0.0
    for key in ("avg_frame_rate", "r_frame_rate"):
        raw = st.get(key) or ""
        if "/" in raw:
            num, den = raw.split("/", 1)
            try:
                num_f, den_f = float(num), float(den)
                if den_f > 0 and num_f > 0:
                    fps = num_f / den_f
                    break
            except ValueError:
                pass

    duration = 0.0
    for src in (st.get("duration"), fmt.get("duration")):
        try:
            if src is not None and float(src) > 0:
                duration = float(src)
                break
        except (TypeError, ValueError):
            pass

    frame_count = 0
    try:
        frame_count = int(st.get("nb_frames") or 0)
    except (TypeError, ValueError):
        frame_count = 0
    if frame_count <= 0 and duration > 0 and fps > 0:
        frame_count = int(round(duration * fps))

    if duration <= 0 and frame_count > 0 and fps > 0:
        duration = frame_count / fps

    if duration <= 0:
        raise VideoError(f"could not determine duration for {path.name}")

    w = st.get("width")
    h = st.get("height")
    return VideoMeta(duration_s=round(duration, 3), fps=round(fps, 4),
                     frame_count=frame_count,
                     width=int(w) if w else None, height=int(h) if h else None)


@dataclass(frozen=True)
class SampledFrame:
    frame_index: int          # index within the sampled sequence, not the source stream
    offset_s: float           # seconds into the clip -- lets review scrub straight to it
    path: Path                # temporary; caller must not persist this


class FrameSampler:
    """Context manager yielding sampled frames in a temp dir that is cleaned up on exit.

    Usage:
        with FrameSampler(clip, policy) as frames:
            for f in frames:
                ...
    """

    def __init__(self, path: Path, policy: SamplingPolicy = DEFAULT_POLICY,
                 meta: VideoMeta | None = None):
        self.path = path
        self.policy = policy
        self.meta = meta or probe(path)
        self.fps = policy.effective_fps(self.meta.duration_s)
        self._tmp: tempfile.TemporaryDirectory | None = None

    def __enter__(self) -> list[SampledFrame]:
        self._tmp = tempfile.TemporaryDirectory(prefix="hoseid-frames-")
        out = Path(self._tmp.name)
        cmd = [FFMPEG, "-v", "error", "-nostdin", "-i", str(self.path),
               "-vf", f"fps={self.fps:.6f}",
               "-frames:v", str(self.policy.max_frames),
               "-q:v", "2", str(out / "f_%05d.jpg")]
        p = _run(cmd)
        files = sorted(out.glob("f_*.jpg"))
        if p.returncode != 0 and not files:
            raise VideoError(f"ffmpeg failed on {self.path.name}: {p.stderr.decode()[:200]}")
        frames = []
        for i, f in enumerate(files):
            # The fps filter emits frames at a fixed cadence, so frame i sits at i/fps seconds.
            offset = i / self.fps if self.fps > 0 else 0.0
            frames.append(SampledFrame(frame_index=i,
                                       offset_s=round(min(offset, self.meta.duration_s), 3),
                                       path=f))
        return frames

    def __exit__(self, *exc) -> None:
        if self._tmp:
            self._tmp.cleanup()
            self._tmp = None


def score_frame(detections: list[dict]) -> float:
    """Rank a sampled frame by `detector_confidence x bbox_area`, summed over animal boxes.

    Deliberately NOT a motion heuristic. Motion is a proxy for "an animal is here"; the detector
    is the direct measure and is cheap enough to run on every sampled frame. Motion is actively
    worse for this job: wind moves branches, so it selects for exactly the false triggers this
    pipeline exists to drop, and the highest-motion frame is often the most motion-blurred --
    the worst frame to identify an animal from.

    Area is included so a close, clear subject beats a distant one at similar confidence.
    """
    total = 0.0
    for d in detections:
        if str(d.get("category")) != "1":       # animals only
            continue
        bbox = d.get("bbox") or [0, 0, 0, 0]
        total += float(d.get("conf", 0.0)) * float(bbox[2]) * float(bbox[3])
    return total
