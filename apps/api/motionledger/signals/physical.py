from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from ..perception.sam import load_track_artifact
from ..schemas.procedure import ProcedureSpec
from ..schemas.report import Primitive
from ..perception.geometry import mask_features, contact_ratio
from .detectors import hysteresis_intervals, detect_rotation_events


def _frame_records(rows: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[int(row["frame"])].append(row)
    return grouped


def _compose_roi(rows: list[dict[str, Any]], width: int, height: int, pad: int = 8) -> tuple[np.ndarray, tuple[int, int]]:
    """Composite all concept masks into a compact shared ROI for one frame."""
    if not rows:
        return np.zeros((1, 1), dtype=np.uint8), (0, 0)
    x0 = max(0, min(int(r["box"][0]) for r in rows) - pad)
    y0 = max(0, min(int(r["box"][1]) for r in rows) - pad)
    x1 = min(width, max(int(r["box"][2]) for r in rows) + pad)
    y1 = min(height, max(int(r["box"][3]) for r in rows) + pad)
    if x1 <= x0 or y1 <= y0:
        return np.zeros((1, 1), dtype=np.uint8), (x0, y0)
    canvas = np.zeros((y1 - y0, x1 - x0), dtype=np.uint8)
    for row in rows:
        left, top, right, bottom = map(int, row["box"])
        mask = np.asarray(row["mask"], dtype=np.uint8)
        expected = (max(1, bottom - top), max(1, right - left))
        if mask.shape != expected:
            mask = cv2.resize(mask, (expected[1], expected[0]), interpolation=cv2.INTER_NEAREST)
        xa, ya = max(0, left - x0), max(0, top - y0)
        xb, yb = min(canvas.shape[1], right - x0), min(canvas.shape[0], bottom - y0)
        if xb > xa and yb > ya:
            canvas[ya:yb, xa:xb] = np.maximum(canvas[ya:yb, xa:xb], mask[:yb-ya, :xb-xa])
    return canvas, (x0, y0)


def _concept_features(rows: list[dict[str, Any]], width: int, height: int) -> tuple[tuple[float, float] | None, float | None, float]:
    if not rows:
        return None, None, 0.0
    # Use the largest mask for principal-axis and centroid; union masks are used for contact.
    row = max(rows, key=lambda item: int(item.get("area", 0)))
    center = row.get("centroid")
    centroid = tuple(map(float, center)) if center and np.all(np.isfinite(center)) else None
    angle = row.get("angle_deg")
    return centroid, (float(angle) if angle is not None and np.isfinite(angle) else None), float(row.get("area", 0))


def extract_physical_primitives(
    *,
    procedure: ProcedureSpec,
    track_files: dict[str, Path],
    frame_count: int,
    fps: float,
    width: int,
    height: int,
    thresholds: dict[str, Any] | None = None,
) -> list[Primitive]:
    """Turn SAM mask tracks into auditable contact/carry/rotation measurements.

    This stage deliberately does not invent placement zones. Zone-specific insertion
    and settled-placement primitives require a grounded or reviewer-drawn zone map.
    """
    if fps <= 0 or frame_count <= 0 or width <= 0 or height <= 0:
        return []
    cfg = thresholds or {}
    contact_cfg = cfg.get("contact", {})
    carry_cfg = cfg.get("carried", {})
    rotate_cfg = cfg.get("rotated", {})
    concepts: dict[str, dict[int, list[dict[str, Any]]]] = {}
    for key, path in track_files.items():
        if path.is_file():
            concepts[key] = _frame_records(load_track_artifact(path))
    if not concepts:
        return []

    primitives: list[Primitive] = []
    hand_key = next((o.key for o in procedure.objects if o.role == "actor" and o.key == "hand"), None)
    if hand_key and hand_key in concepts:
        for obj in procedure.objects:
            if obj.key == hand_key or obj.key not in concepts:
                continue
            contacts: list[float] = []
            centroids: list[tuple[float, float] | None] = []
            for frame in range(frame_count):
                hands = concepts[hand_key].get(frame, [])
                targets = concepts[obj.key].get(frame, [])
                centroids.append(_concept_features(targets, width, height)[0])
                if not hands or not targets:
                    contacts.append(0.0)
                    continue
                hand_mask, hand_origin = _compose_roi(hands, width, height)
                target_mask, target_origin = _compose_roi(targets, width, height)
                x0 = min(hand_origin[0], target_origin[0]); y0 = min(hand_origin[1], target_origin[1])
                x1 = max(hand_origin[0] + hand_mask.shape[1], target_origin[0] + target_mask.shape[1])
                y1 = max(hand_origin[1] + hand_mask.shape[0], target_origin[1] + target_mask.shape[0])
                hmask = np.zeros((y1-y0, x1-x0), dtype=np.uint8)
                tmask = np.zeros_like(hmask)
                hx, hy = hand_origin[0]-x0, hand_origin[1]-y0
                tx, ty = target_origin[0]-x0, target_origin[1]-y0
                hmask[hy:hy+hand_mask.shape[0], hx:hx+hand_mask.shape[1]] = hand_mask
                tmask[ty:ty+target_mask.shape[0], tx:tx+target_mask.shape[1]] = target_mask
                contacts.append(contact_ratio(hmask, tmask, int(contact_cfg.get("dilation_px", 8))))
            intervals = hysteresis_intervals(contacts, float(contact_cfg.get("start_ratio", .02)),
                float(contact_cfg.get("stop_ratio", .01)), float(contact_cfg.get("min_duration_s", .3)), fps)
            for event in intervals:
                primitives.append(Primitive(type="contact", objects=[hand_key, obj.key], start_frame=event.start_frame,
                    end_frame=event.end_frame, features={"peak_contact_ratio": round(event.peak, 4),
                    "duration_s": round(event.duration_s, 3)}, confidence=min(.95, .55 + event.peak)))
                start_center = next((centroids[i] for i in range(event.start_frame, min(event.end_frame + 1, frame_count)) if centroids[i]), None)
                end_center = next((centroids[i] for i in range(min(event.end_frame, frame_count - 1), event.start_frame - 1, -1) if centroids[i]), None)
                if start_center and end_center:
                    diagonal = max(float(np.hypot(width, height)) * .03, 1.0)
                    displacement = float(np.linalg.norm(np.asarray(end_center) - np.asarray(start_center)))
                    ratio = displacement / diagonal
                    if ratio >= float(carry_cfg.get("centroid_diagonal_ratio", .25)):
                        primitives.append(Primitive(type="carried", objects=[obj.key, hand_key],
                            start_frame=event.start_frame, end_frame=event.end_frame,
                            features={"displacement_px": round(displacement, 2), "diagonal_ratio": round(ratio, 3)},
                            confidence=min(.92, .55 + ratio * .25)))

    # Tool rotation is measured only while its mask is in contact with the step subject.
    # In this first integration it is global per step; target-zone localization is a
    # separate grounded-zone stage, so the primitive zone is intentionally left blank.
    for step in procedure.steps:
        if step.action not in {"tighten", "loosen"} or not step.tool or step.tool not in concepts or step.subject not in concepts:
            continue
        angles: list[float] = []
        contacts: list[float] = []
        for frame in range(frame_count):
            tool_rows = concepts[step.tool].get(frame, [])
            subject_rows = concepts[step.subject].get(frame, [])
            _, angle, _ = _concept_features(tool_rows, width, height)
            angles.append(float("nan") if angle is None else angle)
            if not tool_rows or not subject_rows:
                contacts.append(0.0)
                continue
            tool_mask, tool_origin = _compose_roi(tool_rows, width, height)
            subject_mask, subject_origin = _compose_roi(subject_rows, width, height)
            x0=min(tool_origin[0],subject_origin[0]); y0=min(tool_origin[1],subject_origin[1])
            x1=max(tool_origin[0]+tool_mask.shape[1],subject_origin[0]+subject_mask.shape[1])
            y1=max(tool_origin[1]+tool_mask.shape[0],subject_origin[1]+subject_mask.shape[0])
            tmask=np.zeros((y1-y0,x1-x0),dtype=np.uint8); smask=np.zeros_like(tmask)
            tx,ty=tool_origin[0]-x0,tool_origin[1]-y0; sx,sy=subject_origin[0]-x0,subject_origin[1]-y0
            tmask[ty:ty+tool_mask.shape[0],tx:tx+tool_mask.shape[1]]=tool_mask
            smask[sy:sy+subject_mask.shape[0],sx:sx+subject_mask.shape[1]]=subject_mask
            contacts.append(contact_ratio(tmask,smask,int(contact_cfg.get("dilation_px",8))))
        events=detect_rotation_events(angles,contacts,fps=fps,
            min_total_deg=float(rotate_cfg.get("min_total_degrees",90)),
            min_reversals=int(rotate_cfg.get("min_reversals",3)),
            contact_threshold=float(contact_cfg.get("start_ratio",.02)))
        for event in events:
            primitives.append(Primitive(type="rotated",objects=[step.tool,step.subject],zone="",
                start_frame=event["start_frame"],end_frame=event["end_frame"],
                features={"total_deg":round(event["total_deg"],2),"reversals":event["reversals"],
                          "duration_s":round(event["duration_s"],3)},
                confidence=.78 if event["total_deg"]>=float(rotate_cfg.get("min_total_degrees",90)) else .72))
    return sorted(primitives,key=lambda p:(p.start_frame,p.end_frame,p.type,p.objects))
