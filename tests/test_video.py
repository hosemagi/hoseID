"""Video support: schema, sampling policy, frame scoring, and invariant 7."""
from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hoseid import db, landing, paths, review, video
from hoseid.sidecar import (
    SUPPORTED_SCHEMA_VERSIONS, CaptureTimeSource, MediaType, ResolutionClass, Sidecar,
    SidecarError, StationSource, compute_asset_id, validate_sidecar,
)

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


@pytest.fixture(autouse=True)
def tmp_root(tmp_path, monkeypatch):
    monkeypatch.setenv("HOSEID_ROOT", str(tmp_path))
    paths.ensure_layout()
    return tmp_path


def _base(src: Path, **over):
    d = dict(
        asset_id=compute_asset_id(src), media_type=MediaType.image, source="sd_card",
        resolution_class=ResolutionClass.original, station="S", station_source=StationSource.manual,
        vendor="v", capture_time=datetime(2026, 8, 3, tzinfo=timezone.utc),
        capture_time_source=CaptureTimeSource.exif, capture_time_confidence="high",
        ingested_at=datetime(2026, 8, 3, tzinfo=timezone.utc), bytes=src.stat().st_size,
    )
    d.update(over)
    return d


def _f(tmp_path, name="a.jpg", content=b"x"):
    p = tmp_path / name
    p.write_bytes(content)
    return p


# --- schema: v2 adds video, v1 image sidecars stay readable --------------------
def test_v1_image_sidecars_remain_valid(tmp_path):
    """The landing zone is immutable, so anything already written can never be migrated.
    Readers must keep understanding v1 forever."""
    src = _f(tmp_path)
    d = _base(src)
    d["schema_version"] = 1
    sc = validate_sidecar(json.loads(json.dumps(Sidecar(**d).model_dump(mode="json"))))
    assert sc.schema_version == 1 and not sc.is_video


def test_supported_versions_include_both():
    assert SUPPORTED_SCHEMA_VERSIONS == {1, 2}


def test_video_sidecar_requires_duration_fps_framecount(tmp_path):
    src = _f(tmp_path, "a.mp4")
    with pytest.raises(Exception):
        Sidecar(**_base(src, media_type=MediaType.video))


def test_video_sidecar_valid_with_video_fields(tmp_path):
    src = _f(tmp_path, "a.mp4")
    sc = Sidecar(**_base(src, media_type=MediaType.video,
                         duration_s=8.2, fps=10.0, frame_count=82))
    assert sc.is_video and sc.duration_s == 8.2


def test_image_sidecar_must_not_carry_video_fields(tmp_path):
    src = _f(tmp_path)
    with pytest.raises(Exception):
        Sidecar(**_base(src, duration_s=8.2, fps=10.0, frame_count=82))


def test_v1_cannot_declare_video(tmp_path):
    src = _f(tmp_path, "a.mp4")
    d = _base(src, media_type=MediaType.video, duration_s=1.0, fps=10.0, frame_count=10)
    d["schema_version"] = 1
    with pytest.raises(SidecarError):
        validate_sidecar(json.loads(json.dumps(Sidecar(**d).model_dump(mode="json"))))


def test_zero_duration_video_rejected(tmp_path):
    src = _f(tmp_path, "a.mp4")
    with pytest.raises(Exception):
        Sidecar(**_base(src, media_type=MediaType.video, duration_s=0, fps=10.0, frame_count=0))


# --- sampling policy ----------------------------------------------------------
def test_short_clip_uses_nominal_fps():
    p = video.SamplingPolicy(nominal_fps=2.0, max_frames=40)
    assert p.effective_fps(8.0) == 2.0          # 16 frames, under the cap


def test_cap_binds_by_spreading_across_full_clip_not_truncating():
    """A 60 s clip at a literal '2 fps stop at 40' would only cover the first 20 s, so an animal
    appearing later would produce no detection at all -- a systematic bias toward the start and a
    strictly worse lower bound. Even spreading costs the same 40 decoded frames."""
    p = video.SamplingPolicy(nominal_fps=2.0, max_frames=40)
    eff = p.effective_fps(60.0)
    assert eff < 2.0
    assert abs(eff * 60.0 - 40) < 1e-6, "40 frames must span the whole clip"


def test_policy_is_recorded_for_provenance():
    p = video.SamplingPolicy()
    prov = p.as_provenance(duration_s=60.0)
    assert prov["nominal_fps"] == 2.0 and prov["max_frames"] == 40
    assert prov["effective_fps"] < 2.0 and prov["version"] == "1"


def test_zero_duration_falls_back_to_nominal():
    assert video.SamplingPolicy().effective_fps(0) == 2.0


# --- frame scoring: detector-driven, never motion -----------------------------
def _det(conf, w, h, category="1"):
    return {"category": category, "conf": conf, "bbox": [0.1, 0.1, w, h]}


def test_score_prefers_confident_and_large():
    assert video.score_frame([_det(0.9, 0.4, 0.4)]) > video.score_frame([_det(0.9, 0.1, 0.1)])
    assert video.score_frame([_det(0.9, 0.2, 0.2)]) > video.score_frame([_det(0.3, 0.2, 0.2)])


def test_score_ignores_non_animal_categories():
    """Person and vehicle boxes must not win frame selection away from an animal."""
    assert video.score_frame([_det(0.99, 0.9, 0.9, category="2")]) == 0.0
    assert video.score_frame([_det(0.99, 0.9, 0.9, category="3")]) == 0.0


def test_empty_frame_scores_zero():
    assert video.score_frame([]) == 0.0


def test_score_sums_multiple_animals():
    two = video.score_frame([_det(0.8, 0.2, 0.2), _det(0.8, 0.2, 0.2)])
    one = video.score_frame([_det(0.8, 0.2, 0.2)])
    assert two > one


# --- INVARIANT 7: video counts are lower bounds, not censuses -----------------
def _seed_mixed(run_id="r1"):
    with db.detections() as conn:
        db.start_run(conn, run_id=run_id, started_at="2026-08-05T00:00:00Z",
                     detector_model="md", detector_version="1", detector_threshold=0.2)
        # image capture: complete count of 3 animals
        conn.execute("""INSERT INTO captures (asset_id, run_id, station, capture_time,
            time_trusted, n_detections, has_animal, is_empty, media_type, count_is_lower_bound)
            VALUES ('sha256:img',?,'S','2026-08-03T04:00:00Z',1,3,1,0,'image',0)""", (run_id,))
        # video capture: one frame kept, so 1 is a lower bound -- the clip may show more
        conn.execute("""INSERT INTO captures (asset_id, run_id, station, capture_time,
            time_trusted, n_detections, has_animal, is_empty, media_type, count_is_lower_bound,
            sampled_frames) VALUES ('sha256:vid',?,'S','2026-08-03T05:00:00Z',1,1,1,0,'video',1,16)""",
                     (run_id,))
        conn.commit()
    return run_id


def test_video_captures_are_flagged_as_lower_bounds():
    run_id = _seed_mixed()
    with db.detections() as c:
        rows = {r["media_type"]: r["count_is_lower_bound"]
                for r in c.execute("SELECT media_type, count_is_lower_bound FROM captures "
                                   "WHERE run_id=?", (run_id,)).fetchall()}
    assert rows == {"image": 1, "video": 0} or rows == {"image": 0, "video": 1}
    assert rows["video"] == 1 and rows["image"] == 0


def test_census_view_excludes_video():
    run_id = _seed_mixed()
    with db.detections() as c:
        n = c.execute("SELECT COUNT(*) n FROM captures_census WHERE run_id=?",
                      (run_id,)).fetchone()["n"]
    assert n == 1, "captures_census must contain only complete counts"


def test_group_size_stats_excludes_video_by_default():
    """The specific failure this prevents: an aggregate silently mixing a complete image count
    with a truncated video count and reporting it as mean animals present."""
    run_id = _seed_mixed()
    s = review.group_size_stats(run_id)
    assert s["is_census"] is True
    assert s["captures"] == 1
    assert s["mean_animals_per_capture"] == 3.0, "must not be dragged down by the video's 1"
    assert s["excluded_lower_bound_captures"] == 1


def test_including_lower_bounds_is_possible_but_labelled():
    run_id = _seed_mixed()
    s = review.group_size_stats(run_id, include_lower_bounds=True)
    assert s["is_census"] is False
    assert s["captures"] == 2
    assert "LOWER BOUNDS" in s["_note"] and "NOT a group size" in s["_note"]


# --- end-to-end on a real clip ------------------------------------------------
@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg/ffprobe not installed")
def test_probe_and_sample_real_clip(tmp_path):
    clip = tmp_path / "c.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                    "-i", "color=gray:s=320x240:d=6", "-r", "10",
                    "-pix_fmt", "yuv420p", str(clip)], check=True)
    meta = video.probe(clip)
    assert meta.duration_s > 5.5 and meta.fps == 10.0 and meta.width == 320

    policy = video.SamplingPolicy(nominal_fps=2.0, max_frames=40)
    with video.FrameSampler(clip, policy, meta=meta) as frames:
        assert 10 <= len(frames) <= 13           # ~6 s at 2 fps
        assert frames[0].offset_s == 0.0
        assert frames[-1].offset_s <= meta.duration_s
        assert all(f.path.exists() for f in frames)
        tmpdir = frames[0].path.parent
    assert not tmpdir.exists(), "sampled frames are temporary and must not persist"


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg/ffprobe not installed")
def test_cap_binds_on_long_clip(tmp_path):
    clip = tmp_path / "long.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                    "-i", "color=gray:s=160x120:d=60", "-r", "10",
                    "-pix_fmt", "yuv420p", str(clip)], check=True)
    policy = video.SamplingPolicy(nominal_fps=2.0, max_frames=40)
    with video.FrameSampler(clip, policy) as frames:
        assert len(frames) <= 40, "the cap must bind"
        # and the frames must span the clip, not stop at 20 s
        assert frames[-1].offset_s > 50.0, "sampling must cover the tail of a long clip"


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg/ffprobe not installed")
def test_probe_rejects_non_video(tmp_path):
    bad = tmp_path / "not.mp4"
    bad.write_bytes(b"definitely not a video")
    with pytest.raises(video.VideoError):
        video.probe(bad)


# --- decode failure is its own state, never an empty capture ------------------
# A codec hoseID cannot read would otherwise look exactly like a station where nothing walked
# past: same n_detections=0, same is_empty=1, no reason to investigate. Same silent-failure shape
# as the geofence that no-op'd while recording itself as applied.

def test_decode_state_video_with_zero_sampled_frames_is_a_failure():
    assert video.decode_state(True, 8.0, 30.0, 0) == video.STATE_DECODE_FAILED


def test_decode_state_video_with_frames_is_ok():
    assert video.decode_state(True, 8.0, 30.0, 16) == video.STATE_OK


def test_decode_state_unusable_duration_or_fps_is_a_failure():
    """Same treatment when ffprobe cannot return usable metadata."""
    assert video.decode_state(True, 0, 30.0, 5) == video.STATE_DECODE_FAILED
    assert video.decode_state(True, None, 30.0, 5) == video.STATE_DECODE_FAILED
    assert video.decode_state(True, 8.0, 0, 5) == video.STATE_DECODE_FAILED
    assert video.decode_state(True, 8.0, None, 5) == video.STATE_DECODE_FAILED


def test_decode_state_probe_failure_is_a_failure():
    assert video.decode_state(True, None, None, None, probe_ok=False) == video.STATE_DECODE_FAILED


def test_images_are_never_decode_failures():
    """An image with no detections is genuinely empty, not a decode failure."""
    assert video.decode_state(False, None, None, None) == video.STATE_OK


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg/ffprobe not installed")
def test_probe_safe_reports_corrupt_file_without_raising(tmp_path):
    bad = tmp_path / "corrupt.mp4"
    bad.write_bytes(b"\x00\x01\x02not a video at all" * 50)
    meta, err = video.probe_safe(bad)
    assert meta is None and err, "probe_safe must report, not raise"


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg/ffprobe not installed")
def test_corrupt_clip_still_lands_in_the_landing_zone(tmp_path, monkeypatch):
    """Ingest must not fail (invariant 6). The bytes are the irreplaceable part and a future
    ffmpeg may decode them, so an unreadable clip is landed and flagged, not discarded."""
    from hoseid.ingest_sd import ingest_directory
    card = tmp_path / "card"
    card.mkdir()
    (card / "corrupt.mp4").write_bytes(b"\x00\x01\x02not a video at all" * 50)

    rep = ingest_directory(card, "S", vendor="arlo")
    assert rep.ingested == 1, "the asset must still land"
    assert rep.probe_failed == 1, "and be counted as a probe failure"
    assert not rep.errors, "a probe failure is not an ingest error"

    sc = validate_sidecar(next(iter(landing.iter_sidecars())))
    assert sc.is_video and not sc.probe_ok and sc.probe_error
    assert sc.duration_s is None, "no usable duration to record"


def test_video_sidecar_may_omit_duration_when_probe_failed(tmp_path):
    src = _f(tmp_path, "a.mp4")
    sc = Sidecar(**_base(src, media_type=MediaType.video, probe_status="failed",
                         probe_error="ffprobe failed"))
    assert not sc.probe_ok and sc.duration_s is None


def test_probe_status_is_meaningless_for_images(tmp_path):
    src = _f(tmp_path)
    with pytest.raises(Exception):
        Sidecar(**_base(src, probe_status="failed"))


def _seed_states(run_id="rs"):
    with db.detections() as conn:
        db.start_run(conn, run_id=run_id, started_at="2026-08-05T00:00:00Z",
                     detector_model="md", detector_version="1", detector_threshold=0.2)
        # genuinely empty image: we looked and saw nothing
        conn.execute("""INSERT INTO captures (asset_id, run_id, station, capture_time,
            time_trusted, n_detections, has_animal, is_empty, media_type, count_is_lower_bound,
            decode_status) VALUES ('sha256:e',?,'S','2026-08-03T01:00:00Z',1,0,0,1,'image',0,'ok')""",
                     (run_id,))
        # undecodable clip: we could not look
        conn.execute("""INSERT INTO captures (asset_id, run_id, station, capture_time,
            time_trusted, n_detections, has_animal, is_empty, media_type, count_is_lower_bound,
            sampled_frames, decode_status, decode_error)
            VALUES ('sha256:d',?,'S','2026-08-03T02:00:00Z',1,0,0,0,'video',1,0,
                    'decode_failed','no frames decoded')""", (run_id,))
        conn.commit()
    return run_id


def test_decode_failure_is_excluded_from_empty_capture_stats():
    run_id = _seed_states()
    act = {a["station"]: a for a in review.station_activity(run_id)}["S"]
    assert act["captures"] == 2
    assert act["empty_captures"] == 1, "only the genuinely empty image counts as empty"
    assert act["decode_failures"] == 1, "the undecodable clip is counted separately"


def test_decode_failures_are_countable_and_inspectable():
    run_id = _seed_states()
    fails = review.decode_failures(run_id)
    assert len(fails) == 1
    assert fails[0]["asset_id"] == "sha256:d"
    assert fails[0]["decode_error"] == "no frames decoded"
    assert fails[0]["sampled_frames"] == 0


def test_decode_failure_excluded_from_census():
    """A clip that could not be opened contributes no count at all -- not even a lower bound."""
    run_id = _seed_states()
    with db.detections() as c:
        n = c.execute("SELECT COUNT(*) n FROM captures_census WHERE run_id=?",
                      (run_id,)).fetchone()["n"]
    assert n == 1, "only the image is a census row"


# --- frame_offset_s must be a real timestamp, not derived from a nominal rate --
@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg/ffprobe not installed")
def test_offsets_come_from_real_pts_on_variable_frame_rate(tmp_path):
    """Offsets must be measured timestamps, not a derived cadence.

    This asserts the PTS parsing is actually in effect (offsets diverge from `index / rate`)
    rather than having silently fallen back. It does NOT by itself prove the derived value was
    wrong -- on smoothly-warped VFR the two selections differ but each labels its own frame
    correctly. The case where deriving is genuinely wrong is a stalled clip; see
    test_stalled_clip_offsets_match_content.
    """
    src = tmp_path / "cfr.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                    "-i", "color=gray:s=160x120:d=30:r=30", "-pix_fmt", "yuv420p", str(src)],
                   check=True)
    vfr = tmp_path / "vfr.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src),
                    "-vf", "setpts='PTS*(1.0+1.2*abs(sin(N/40)))'",
                    "-fps_mode", "passthrough", "-pix_fmt", "yuv420p", str(vfr)], check=True)

    meta = video.probe(vfr)
    policy = video.SamplingPolicy()
    eff = policy.effective_fps(meta.duration_s)
    with video.FrameSampler(vfr, policy, meta=meta) as frames:
        assert len(frames) >= 8
        offsets = [f.offset_s for f in frames]
        derived = [i / eff for i in range(len(frames))]
        # Offsets must be strictly increasing real timestamps within the clip.
        assert offsets == sorted(offsets)
        assert offsets[-1] <= meta.duration_s + 0.5
        # And they must NOT simply be the derived cadence -- on this clip the two diverge.
        max_div = max(abs(a - b) for a, b in zip(offsets, derived))
        assert max_div > 0.3, (
            f"offsets look derived rather than measured (max divergence {max_div:.3f}s); "
            "PTS parsing may have silently fallen back")


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg/ffprobe not installed")
def test_offsets_are_exact_on_constant_frame_rate(tmp_path):
    """On CFR the measured timestamps should agree closely with the nominal cadence."""
    src = tmp_path / "cfr.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                    "-i", "color=gray:s=160x120:d=10:r=30", "-pix_fmt", "yuv420p", str(src)],
                   check=True)
    policy = video.SamplingPolicy()
    with video.FrameSampler(src, policy) as frames:
        for i, f in enumerate(frames):
            assert abs(f.offset_s - i * 0.5) < 0.15, "CFR offsets should track the 2 fps cadence"


def test_showinfo_pts_parser():
    stderr = ("[Parsed_showinfo_1 @ 0x1] n:   0 pts:      0 pts_time:0       duration:1\n"
              "[Parsed_showinfo_1 @ 0x1] n:   1 pts:   1501 pts_time:1.567   duration:1\n"
              "[Parsed_showinfo_1 @ 0x1] n:   2 pts:   3003 pts_time:3.133   duration:1\n")
    assert video._parse_showinfo_pts(stderr) == [0.0, 1.567, 3.133]


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg/ffprobe not installed")
def test_stalled_clip_offsets_match_content(tmp_path):
    """The case where deriving offsets is genuinely wrong: a clip with a real gap in its
    presentation timestamps.

    The `fps` filter maintains a constant output cadence by DUPLICATING the last frame across
    the gap, then those duplicates carry fabricated offsets -- a frame whose content is from
    t=4 s gets labelled t=24 s. Measured on a 20 s stall, the derived approach was off by up to
    20.5 s, which for a field whose job is scrubbing to the moment is worse than useless.
    Selecting on elapsed presentation time emits only real frames and reports their true PTS.
    """
    src = tmp_path / "src.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                    "-i", "color=black:s=160x120:d=28:r=25",
                    "-vf", "geq=lum='clip((T/28)*255,0,255)':cb=128:cr=128",
                    "-pix_fmt", "yuv420p", str(src)], check=True)
    stalled = tmp_path / "stalled.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src),
                    "-vf", "select='lt(t,4)+gt(t,24)'", "-fps_mode", "passthrough",
                    "-pix_fmt", "yuv420p", str(stalled)], check=True)

    def luminance(p):
        r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(p), "-vf", "scale=1:1",
                            "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True)
        return r.stdout[0] if r.stdout else None

    policy = video.SamplingPolicy(nominal_fps=1.0, max_frames=40)
    with video.FrameSampler(stalled, policy) as frames:
        assert frames, "the stalled clip must still yield frames"
        # No frame may be emitted from inside the 4-24 s gap: nothing was recorded there.
        for f in frames:
            in_gap = 4.5 < f.offset_s < 23.5
            assert not in_gap, f"frame claimed offset {f.offset_s}s inside a gap with no frames"
        # Luminance encodes source time over 0-28 s; offsets must agree with content.
        for f in frames:
            lum = luminance(f.path)
            if lum is None or lum >= 250:      # saturated end of the ramp, not informative
                continue
            true_t = lum / 255 * 28
            assert abs(f.offset_s - true_t) < 3.0, (
                f"frame at claimed {f.offset_s:.2f}s has content from {true_t:.2f}s")
