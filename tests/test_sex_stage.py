"""stages/sex.py: the monthly buck/doe model scores blacktail crops only, once,
and is a logged no-op without a model or on backbone weights the head wasn't
trained on. The DINOv2 backbone is stubbed; the head is a real tiny torch head."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")  # torch/numpy/PIL: ~/venvs/megadetector (AGENTS.md)
pytest.importorskip("torch")
Image = pytest.importorskip("PIL.Image")

from hoseid import db, paths

STAGE = Path(__file__).resolve().parents[1] / "stages" / "sex.py"


def _load_stage():
    spec = importlib.util.spec_from_file_location("stage_sex", STAGE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sex = _load_stage()

WEIGHTS = b"pretend dinov2 weights"


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("HOSEID_ROOT", str(tmp_path))
    paths.ensure_layout()
    with db.detections() as conn:
        conn.execute("INSERT INTO runs (run_id, started_at, detector_model, detector_version, "
                     "detector_threshold) VALUES ('nightly', '2026-11-01', 'md', 'v5a', 0.1)")
        rows = [("d1", "sha256:a1", "blacktail", (200, 0, 0)),
                ("d2", "sha256:a1", "blacktail", (40, 0, 0)),
                ("d3", "sha256:a2", "mountain_lion", (250, 0, 0)),
                ("d4", "sha256:a3", None, (10, 0, 0))]
        for det_id, asset, taxon, color in rows:
            Image.new("RGB", (8, 8), color).save(paths.crops_dir() / f"{det_id}.jpg")
            conn.execute(
                "INSERT INTO detections (detection_id, asset_id, run_id, bbox_x, bbox_y, bbox_w, "
                "bbox_h, detector_class, detector_confidence, taxon, crop_path) "
                "VALUES (?,?,'nightly',0,0,1,1,'animal',0.9,?,?)",
                (det_id, asset, taxon, f"{det_id}.jpg"))
        conn.commit()
    ckpt = tmp_path / "dinov2_vitb14_pretrain.pth"
    ckpt.write_bytes(WEIGHTS)
    monkeypatch.setattr(sex, "hub_checkpoint", lambda weights: ckpt)
    return tmp_path


def _model(root: Path, sha: str | None = None, run: str = "2026-11-01") -> Path:
    """A promoted run with a real logistic head on 2-d (cls+mean) features:
    logit = 4 * (standardized cls) -> brighter red crop = more buck."""
    import torch

    d = root / "derived/training/sex/runs" / run
    d.mkdir(parents=True)
    head = torch.nn.Linear(2, 1)
    with torch.no_grad():
        head.weight[:] = torch.tensor([[4.0, 0.0]])
        head.bias[:] = 0.0
    torch.save({"kind": "logistic", "features": {"keys": ["cls", "mean"]},
                "mu": np.array([0.5, 0.0], dtype=np.float32),
                "sd": np.array([0.25, 1.0], dtype=np.float32),
                "state_dict": head.state_dict()}, d / "sex_head.pt")
    (d / "metrics.json").write_text(json.dumps({"threshold": 0.5}))
    (d / "backbone.json").write_text(json.dumps({
        "model": "dinov2_vitb14", "size": 448, "weights": "dinov2_vitb14_pretrain.pth",
        "sha256": sha or hashlib.sha256(WEIGHTS).hexdigest()}))
    current = root / "derived/training/sex/current"
    current.symlink_to(d.relative_to(current.parent))
    return d


@pytest.fixture
def backbone(monkeypatch):
    """Stub DINOv2: cls feature = mean red of the crop, mean token = 0."""
    loads = []

    def load(name, size):
        loads.append((name, size))
        return lambda imgs: (np.array([[np.asarray(im)[..., 0].mean() / 255] for im in imgs],
                                      dtype=np.float32),
                             np.zeros((len(imgs), 1), dtype=np.float32))
    monkeypatch.setattr(sex, "load_backbone", load)
    return loads


def _scores() -> dict:
    with db.detections() as conn:
        return {r["detection_id"]: (r["buck_prob"], r["sex_model"])
                for r in conn.execute("SELECT detection_id, buck_prob, sex_model FROM detections")}


def test_scores_only_blacktail_rows(root, backbone):
    _model(root)
    assert sex.main(["--run-id", "nightly"]) == 0
    s = _scores()
    assert s["d3"] == (None, None) and s["d4"] == (None, None)  # lion, unclassified
    assert s["d1"][1] == s["d2"][1] == "2026-11-01"
    # real head: sigmoid(4 * (red - 0.5) / 0.25); same on the mirror (uniform crop)
    red = np.asarray(Image.open(paths.crops_dir() / "d1.jpg"))[..., 0].mean() / 255
    assert s["d1"][0] == pytest.approx(1 / (1 + np.exp(-16 * (red - 0.5))), abs=1e-5)
    assert s["d1"][0] > 0.9 > 0.1 > s["d2"][0]
    assert backbone == [("dinov2_vitb14", 448)]


def test_already_scored_rows_are_skipped(root, backbone):
    _model(root)
    with db.detections() as conn:
        conn.execute("UPDATE detections SET buck_prob=0.42, sex_model='old' WHERE detection_id='d1'")
        conn.commit()
    sex.main(["--run-id", "nightly"])
    s = _scores()
    assert s["d1"] == (0.42, "old")
    assert s["d2"][1] == "2026-11-01"

    backbone.clear()
    sex.main(["--run-id", "nightly"])
    assert backbone == []  # nothing left to score: the backbone isn't even loaded


def test_missing_current_is_a_noop(root, backbone, capsys):
    assert sex.main(["--run-id", "nightly"]) == 0
    assert "no model" in capsys.readouterr().out
    assert backbone == []
    assert all(v == (None, None) for v in _scores().values())


def test_missing_head_is_a_noop(root, backbone, capsys):
    (_model(root) / "sex_head.pt").unlink()
    assert sex.main(["--run-id", "nightly"]) == 0
    assert "no model" in capsys.readouterr().out
    assert backbone == []


def test_backbone_sha_mismatch_skips(root, backbone, capsys):
    _model(root, sha="0" * 64)
    assert sex.main(["--run-id", "nightly"]) == 0
    out = capsys.readouterr()
    assert "backbone mismatch" in out.err
    assert backbone == []
    assert all(v == (None, None) for v in _scores().values())


def test_limit_scores_newest_first(root, backbone):
    _model(root)
    sex.main(["--run-id", "nightly", "--limit", "1"])
    s = _scores()
    assert s["d2"][0] is not None and s["d1"] == (None, None)


def test_dino_features_cls_and_mean_patch_token():
    """The shared feature function (embed_dino.py and the stage): 448 px input,
    CLS token and the mean over patch tokens."""
    import torch

    from training.sex.common import dino_features

    class Fake:
        def forward_features(self, x):
            self.shape = tuple(x.shape)
            n = x.shape[0]
            return {"x_norm_clstoken": torch.ones(n, 3),
                    "x_norm_patchtokens": torch.arange(n * 4 * 3, dtype=torch.float32)
                    .view(n, 4, 3)}

    m = Fake()
    cls, mean = dino_features(m, [Image.new("RGB", (20, 10))] * 2, "cpu")
    assert m.shape == (2, 3, 448, 448)
    assert cls.shape == (2, 3) and mean.shape == (2, 3)
    assert mean[0].tolist() == [4.5, 5.5, 6.5]
