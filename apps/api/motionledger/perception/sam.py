from __future__ import annotations

import base64
import json
import zlib
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np

from .geometry import mask_features


def _plain_rle(value: Any) -> dict[str, Any]:
    """Convert the parser's frozen RLE dataclass to JSON-safe data."""
    if is_dataclass(value):
        raw = asdict(value)
    elif hasattr(value, "__dict__"):
        raw = dict(value.__dict__)
    else:
        raw = {"size": getattr(value, "size", None), "counts": getattr(value, "counts", None)}
    if isinstance(raw.get("size"), tuple):
        raw["size"] = list(raw["size"])
    counts = raw.get("counts")
    if isinstance(counts, bytes):
        raw["counts"] = counts.decode("ascii")
    elif isinstance(counts, tuple):
        raw["counts"] = list(counts)
    return raw


def decode_video_records(
    records: tuple[Any, ...] | list[Any],
    *,
    raster_decoder: Callable[[Any], bytes] | None = None,
    rle_decoder: Callable[[Any], Any] | None = None,
) -> list[dict[str, Any]]:
    """Pair SAM boxes and masks and derive source-frame geometry.

    The parser returns mask rasters relative to their boxes. This function
    scales each raster to the half-open source-pixel box before calculating its
    area, centroid and principal-axis angle.
    """
    if raster_decoder is None or rle_decoder is None:
        try:
            from meta_sam_parser import decode_mask_to_raster, decode_mask_to_rle
        except ImportError as exc:  # pragma: no cover - exercised in installed environment
            raise RuntimeError("Install meta-sam-parser>=0.1.0 to decode SAM masks") from exc
        raster_decoder = raster_decoder or decode_mask_to_raster
        rle_decoder = rle_decoder or decode_mask_to_rle

    boxes = {
        (record.frame.frame_index, record.object_id): record
        for record in records
        if getattr(record, "kind", None) == "box"
    }
    decoded: list[dict[str, Any]] = []
    for record in records:
        if getattr(record, "kind", None) != "mask":
            continue
        frame_index = int(record.frame.frame_index)
        box = boxes.get((frame_index, record.object_id))
        if box is None:
            continue
        left, top = int(box.left), int(box.top)
        right, bottom = int(box.right), int(box.bottom)
        box_w, box_h = max(1, right - left), max(1, bottom - top)
        raster_value = raster_decoder(record.mask)
        raw = (np.frombuffer(raster_value, dtype=np.uint8) if isinstance(raster_value, (bytes, bytearray, memoryview))
               else np.asarray(raster_value, dtype=np.uint8).reshape(-1))
        mask_w, mask_h = int(record.mask.width), int(record.mask.height)
        if raw.size != mask_w * mask_h:
            raise ValueError(f"SAM mask {record.object_id} on frame {frame_index} has an invalid raster size")
        raster = raw.reshape(mask_h, mask_w)
        if raster.shape != (box_h, box_w):
            raster = cv2.resize(raster, (box_w, box_h), interpolation=cv2.INTER_NEAREST)
        features = mask_features(raster)
        local_centroid = features["centroid"]
        centroid = None if local_centroid is None else (left + local_centroid[0], top + local_centroid[1])
        confidence = getattr(record, "confidence", None)
        if confidence is None:
            confidence = getattr(box, "confidence", None)
        rle = _plain_rle(rle_decoder(record.mask))
        decoded.append({
            "frame": frame_index,
            "object_id": str(record.object_id),
            "box": [left, top, right, bottom],
            "confidence": float(confidence) if confidence is not None else None,
            "area": int(features["area"]),
            "centroid": list(centroid) if centroid is not None else None,
            "angle_deg": features["angle_deg"],
            "mask_size": [box_h, box_w],
            "mask_zlib_b64": base64.b64encode(zlib.compress(raster.astype(np.uint8).tobytes(), level=6)).decode("ascii"),
            "mask_rle": rle,
        })
    return decoded


def save_track_artifact(records: list[dict[str, Any]], path: Path) -> dict[str, Any]:
    """Persist compact numeric track features and compressed masks in an NPZ."""
    path.parent.mkdir(parents=True, exist_ok=True)
    count = len(records)
    frames = np.asarray([r["frame"] for r in records], dtype=np.int32)
    boxes = np.asarray([r["box"] for r in records], dtype=np.int32).reshape(count, 4)
    centroids = np.asarray([r["centroid"] or (np.nan, np.nan) for r in records], dtype=np.float32).reshape(count, 2)
    confidence = np.asarray([r["confidence"] if r["confidence"] is not None else np.nan for r in records], dtype=np.float32)
    areas = np.asarray([r["area"] for r in records], dtype=np.int32)
    angles = np.asarray([r["angle_deg"] if r["angle_deg"] is not None else np.nan for r in records], dtype=np.float32)
    object_ids = np.asarray([r["object_id"] for r in records], dtype="U32")
    rle_json = np.asarray([json.dumps(r["mask_rle"], separators=(",", ":")) for r in records], dtype="U")
    mask_zlib_b64 = np.asarray([r["mask_zlib_b64"] for r in records], dtype="U")
    np.savez_compressed(path, frames=frames, boxes=boxes, centroids=centroids, confidence=confidence,
                        areas=areas, angles=angles, object_ids=object_ids, mask_rle_json=rle_json, mask_zlib_b64=mask_zlib_b64)

    by_id: dict[str, list[dict[str, Any]]] = {}
    for item in records:
        by_id.setdefault(item["object_id"], []).append(item)
    objects = []
    for object_id, items in sorted(by_id.items()):
        confs = [r["confidence"] for r in items if r["confidence"] is not None]
        objects.append({
            "sam_object_id": object_id,
            "visible_frames": len(items),
            "first_frame": min(r["frame"] for r in items),
            "last_frame": max(r["frame"] for r in items),
            "mean_confidence": round(float(np.mean(confs)), 4) if confs else None,
        })
    return {"records": count, "objects": objects, "artifact": str(path)}


def load_track_artifact(path: Path) -> list[dict[str, Any]]:
    """Load decoded SAM records without pickle-enabled NPZ deserialization."""
    with np.load(path, allow_pickle=False) as data:
        frames = data["frames"]
        boxes = data["boxes"]
        centroids = data["centroids"]
        confidence = data["confidence"]
        areas = data["areas"]
        angles = data["angles"]
        object_ids = data["object_ids"]
        masks = data["mask_zlib_b64"]
        output = []
        for i in range(len(frames)):
            h = max(1, int(boxes[i, 3] - boxes[i, 1]))
            w = max(1, int(boxes[i, 2] - boxes[i, 0]))
            mask = np.frombuffer(zlib.decompress(base64.b64decode(str(masks[i]))), dtype=np.uint8)
            if mask.size != h * w:
                raise ValueError(f"Corrupt mask payload in {path} at row {i}")
            center = centroids[i].tolist()
            output.append({
                "frame": int(frames[i]), "box": boxes[i].tolist(), "centroid": center,
                "confidence": None if not np.isfinite(confidence[i]) else float(confidence[i]),
                "area": int(areas[i]), "angle_deg": None if not np.isfinite(angles[i]) else float(angles[i]),
                "object_id": str(object_ids[i]), "mask": mask.reshape(h, w),
            })
    return output


def load_track_frame(path: Path, frame_index: int) -> list[dict[str, Any]]:
    """Read just one frame's SAM records from a compressed track archive."""
    if not path.is_file():
        return []
    output: list[dict[str, Any]] = []
    with np.load(path, allow_pickle=False) as data:
        frames = data["frames"]
        indices = np.flatnonzero(frames == int(frame_index))
        if not len(indices):
            return []
        boxes = data["boxes"]
        confidence = data["confidence"]
        areas = data["areas"]
        angles = data["angles"]
        object_ids = data["object_ids"]
        masks = data["mask_zlib_b64"]
        centroids = data["centroids"]
        for i in indices:
            left, top, right, bottom = map(int, boxes[i])
            height, width = max(1, bottom - top), max(1, right - left)
            raster = np.frombuffer(zlib.decompress(base64.b64decode(str(masks[i]))), dtype=np.uint8)
            if raster.size != height * width:
                raise ValueError(f"Corrupt mask payload in {path} at frame {frame_index}")
            center = centroids[i].tolist()
            output.append({
                "frame": int(frames[i]), "object_id": str(object_ids[i]), "box": [left, top, right, bottom],
                "confidence": None if not np.isfinite(confidence[i]) else float(confidence[i]),
                "area": int(areas[i]), "angle_deg": None if not np.isfinite(angles[i]) else float(angles[i]),
                "centroid": center, "mask": raster.reshape(height, width),
            })
    return output
