from pathlib import Path
from types import SimpleNamespace

import numpy as np

from motionledger.perception.sam import decode_video_records, save_track_artifact


def _record(kind, frame, object_id, **kwargs):
    return SimpleNamespace(kind=kind, frame=SimpleNamespace(frame_index=frame), object_id=object_id, **kwargs)


def test_decode_pairs_box_and_mask_and_computes_geometry(tmp_path: Path):
    # The 2x3 mask scales to a 4x6 box; the six positive pixels should become 24.
    mask = SimpleNamespace(width=3, height=2, payload="fake")
    box = _record("box", 12, 7, left=10, top=20, right=16, bottom=24, confidence=0.91)
    mask_record = _record("mask", 12, 7, mask=mask, confidence=0.89)

    decoded = decode_video_records(
        [box, mask_record],
        raster_decoder=lambda _: bytes([1, 1, 0, 0, 1, 1]),
        rle_decoder=lambda _: SimpleNamespace(size=(2, 3), counts=b"abc"),
    )

    assert len(decoded) == 1
    item = decoded[0]
    assert item["frame"] == 12
    assert item["object_id"] == "7"
    assert item["box"] == [10, 20, 16, 24]
    assert item["area"] == 16
    assert item["centroid"] is not None
    assert item["confidence"] == 0.89
    assert item["mask_rle"] == {"size": [2, 3], "counts": "abc"}

    artifact = tmp_path / "tracks" / "sam-object.npz"
    summary = save_track_artifact(decoded, artifact)
    assert artifact.exists()
    assert summary["records"] == 1
    with np.load(artifact, allow_pickle=False) as stored:
        assert stored["frames"].tolist() == [12]
        assert stored["boxes"].tolist() == [[10, 20, 16, 24]]
        assert stored["object_ids"].tolist() == ["7"]
        assert '"counts":"abc"' in stored["mask_rle_json"][0]


def test_empty_sam_result_is_a_valid_empty_track(tmp_path: Path):
    artifact = tmp_path / "empty.npz"
    summary = save_track_artifact([], artifact)
    assert summary["records"] == 0
    assert summary["objects"] == []
    with np.load(artifact, allow_pickle=False) as stored:
        assert stored["frames"].shape == (0,)
        assert stored["boxes"].shape == (0, 4)


def test_physical_signal_stage_extracts_hand_contact_and_carry(tmp_path: Path, procedure):
    from motionledger.perception.sam import save_track_artifact
    from motionledger.signals.physical import extract_physical_primitives

    def row(frame, object_id, left, top=20, angle=0.0):
        width, height = 10, 10
        mask = np.ones((height, width), dtype=np.uint8)
        return {
            "frame": frame, "object_id": str(object_id),
            "box": [left, top, left + width, top + height],
            "confidence": .9, "area": 100,
            "centroid": [left + 4.5, top + 4.5], "angle_deg": angle,
            "mask_size": [height, width], "mask_zlib_b64": __import__("base64").b64encode(
                __import__("zlib").compress(mask.tobytes())).decode("ascii"),
            "mask_rle": {"size": [height, width], "counts": "fake"},
        }

    hand = [row(frame, 1, 28) for frame in range(2, 8)]
    # The object translates beneath a stationary hand, creating measurable transport.
    bolt = [row(frame, 2, 20 + frame) for frame in range(2, 8)]
    hand_path = tmp_path / "hand.npz"
    bolt_path = tmp_path / "bolt.npz"
    save_track_artifact(hand, hand_path)
    save_track_artifact(bolt, bolt_path)

    # Restrict concepts to the two tracks under test.
    from motionledger.schemas.procedure import ProcedureSpec
    focused = procedure.model_copy(update={
        "objects": [o for o in procedure.objects if o.key in {"hand", "bolt"}],
        "steps": [], "zones": [], "postconditions": [],
    })
    primitives = extract_physical_primitives(
        procedure=focused, track_files={"hand": hand_path, "bolt": bolt_path},
        frame_count=12, fps=10, width=100, height=80,
        thresholds={"contact":{"start_ratio":.02,"stop_ratio":.01,"min_duration_s":.3,"dilation_px":8},
                    "carried":{"centroid_diagonal_ratio":.05}},
    )
    assert any(p.type == "contact" and p.objects == ["hand", "bolt"] for p in primitives)
    assert any(p.type == "carried" and p.objects[0] == "bolt" for p in primitives)


def test_physical_signal_stage_detects_tool_rotation_while_subject_contact(tmp_path: Path, procedure):
    from motionledger.perception.sam import save_track_artifact
    from motionledger.signals.physical import extract_physical_primitives

    def row(frame, object_id, left, angle):
        mask = np.ones((12, 12), dtype=np.uint8)
        return {
            "frame": frame, "object_id": str(object_id), "box": [left, 18, left + 12, 30],
            "confidence": .9, "area": 144, "centroid": [left + 5.5, 23.5], "angle_deg": angle,
            "mask_size": [12, 12], "mask_zlib_b64": __import__("base64").b64encode(
                __import__("zlib").compress(mask.tobytes())).decode("ascii"),
            "mask_rle": {"size": [12, 12], "counts": "fake"},
        }

    tool_path = tmp_path / "wrench.npz"
    bolt_path = tmp_path / "bolt.npz"
    angles = [0, 15, 30, 45, 60, 75, 90, 105]
    save_track_artifact([row(frame + 2, 1, 32, angle) for frame, angle in enumerate(angles)], tool_path)
    save_track_artifact([row(frame + 2, 2, 34, 0) for frame in range(len(angles))], bolt_path)

    primitives = extract_physical_primitives(
        procedure=procedure, track_files={"wrench": tool_path, "bolt": bolt_path},
        frame_count=14, fps=10, width=100, height=80,
        thresholds={"contact":{"start_ratio":.02,"stop_ratio":.01,"min_duration_s":.3,"dilation_px":8},
                    "rotated":{"min_total_degrees":90,"min_reversals":3}},
    )
    assert any(p.type == "rotated" and p.objects == ["wrench", "bolt"] for p in primitives)


def test_load_track_frame_and_render_transparent_overlay(tmp_path: Path, monkeypatch):
    from motionledger.perception.sam import load_track_frame, save_track_artifact
    from motionledger.media import ingest
    from motionledger.media.render import render_sam_overlay
    import cv2

    mask = np.zeros((8, 8), dtype=np.uint8)
    mask[1:7, 1:7] = 1
    decoded = [{
        "frame": 3, "object_id": "1", "box": [12, 15, 20, 23], "confidence": .95,
        "area": 36, "centroid": [15.5, 18.5], "angle_deg": 0.0, "mask_size": [8, 8],
        "mask_zlib_b64": __import__("base64").b64encode(__import__("zlib").compress(mask.tobytes())).decode("ascii"),
        "mask_rle": {"size": [8, 8], "counts": "fake"},
    }]
    path = tmp_path / "sam-hand.npz"
    save_track_artifact(decoded, path)
    frame_records = load_track_frame(path, 3)
    assert len(frame_records) == 1 and int(frame_records[0]["mask"].sum()) == 36
    assert load_track_frame(path, 2) == []

    base = np.zeros((40, 40, 3), dtype=np.uint8)
    ok, jpg = cv2.imencode(".jpg", base)
    assert ok
    monkeypatch.setattr(ingest, "read_frame", lambda *_: jpg.tobytes())
    png = render_sam_overlay(Path("unused.mp4"), [{"key": "hand", "role": "actor", "path": str(path)}], 3)
    overlay = cv2.imdecode(np.frombuffer(png, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    assert overlay.shape == (40, 40, 4)
    assert int(overlay[:, :, 3].max()) > 0
