from __future__ import annotations
from pathlib import Path
import cv2
import numpy as np

def draw_box_overlay(frame: np.ndarray, boxes: list[dict], labels: dict[int,str] | None = None) -> np.ndarray:
    out=frame.copy(); labels=labels or {}
    for item in boxes:
        x0,y0,x1,y1=[int(v) for v in item["box"]]
        oid=int(item.get("object_id",0)); color=(75,190,220)
        cv2.rectangle(out,(x0,y0),(x1,y1),color,2,cv2.LINE_AA)
        text=labels.get(oid,f"object {oid}")
        cv2.rectangle(out,(x0,max(0,y0-24)),(min(out.shape[1]-1,x0+max(72,len(text)*9)),y0),color,-1)
        cv2.putText(out,text,(x0+5,max(16,y0-7)),cv2.FONT_HERSHEY_SIMPLEX,.48,(14,25,35),1,cv2.LINE_AA)
    return out

def make_contact_sheet(frame_paths: list[Path], output: Path, columns: int = 3) -> Path:
    images=[cv2.imread(str(p)) for p in frame_paths]
    images=[im for im in images if im is not None]
    if not images: raise ValueError("No frames to compose")
    thumbs=[]
    for im in images:
        h,w=im.shape[:2]; target_w=480; target_h=max(1,int(h*target_w/w))
        thumbs.append(cv2.resize(im,(target_w,target_h)))
    rows=(len(thumbs)+columns-1)//columns
    tile_h=max(i.shape[0] for i in thumbs); sheet=np.zeros((rows*tile_h,columns*480,3),dtype=np.uint8)
    for n,im in enumerate(thumbs):
        y=(n//columns)*tile_h;x=(n%columns)*480;sheet[y:y+im.shape[0],x:x+im.shape[1]]=im
    output.parent.mkdir(parents=True,exist_ok=True);cv2.imwrite(str(output),sheet)
    return output


def render_sam_overlay(proxy_path: Path, track_sources: list[dict], frame_index: int) -> bytes:
    """Render a transparent RGBA segmentation layer for one proxy-video frame."""
    from ..perception.sam import load_track_frame
    from .ingest import read_frame

    base_bytes = read_frame(proxy_path, frame_index)
    frame = cv2.imdecode(np.frombuffer(base_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        raise ValueError("Could not decode the requested proxy frame")
    height, width = frame.shape[:2]
    overlay = np.zeros((height, width, 4), dtype=np.uint8)
    palette = {
        "actor": (45, 190, 235),    # BGR
        "part": (90, 190, 105),
        "tool": (225, 155, 55),
        "fixture": (185, 105, 185),
    }
    short = {"hand": "H", "gasket": "G", "bolt": "B", "wrench": "W", "flange": "F"}
    for source in track_sources:
        key, role, path = source["key"], source["role"], source["path"]
        color = palette.get(role, (180, 180, 180))
        label = short.get(key, key[:3].upper())
        for record in load_track_frame(Path(path), frame_index):
            left, top, right, bottom = map(int, record["box"])
            mask = (record["mask"] > 0).astype(np.uint8)
            h, w = mask.shape
            x0, y0 = max(0, left), max(0, top)
            x1, y1 = min(width, left + w), min(height, top + h)
            if x1 <= x0 or y1 <= y0:
                continue
            local = mask[y0-top:y1-top, x0-left:x1-left]
            if local.size == 0:
                continue
            region = overlay[y0:y1, x0:x1]
            selected = local > 0
            region[selected] = (color[0], color[1], color[2], 62)
            contours, _ = cv2.findContours(local, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            shifted = [contour + np.array([[[x0, y0]]], dtype=contour.dtype) for contour in contours]
            cv2.drawContours(overlay, shifted, -1, (*color, 235), 2, cv2.LINE_AA)
            cv2.putText(overlay, label, (x0 + 2, max(16, y0 - 5)), cv2.FONT_HERSHEY_SIMPLEX,
                        .48, (*color, 255), 1, cv2.LINE_AA)
    ok, encoded = cv2.imencode(".png", overlay, [cv2.IMWRITE_PNG_COMPRESSION, 3])
    if not ok:
        raise ValueError("Could not encode segmentation overlay")
    return encoded.tobytes()
