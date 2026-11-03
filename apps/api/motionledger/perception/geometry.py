from __future__ import annotations
import math
import numpy as np

def mask_features(mask: np.ndarray) -> dict:
    """Return area, centroid and principal-axis angle for a binary mask."""
    binary = (np.asarray(mask) > 0).astype(np.uint8)
    ys, xs = np.nonzero(binary)
    if len(xs) == 0:
        return {"area": 0, "centroid": None, "angle_deg": None, "bbox": None}
    x0,x1=int(xs.min()),int(xs.max())+1; y0,y1=int(ys.min()),int(ys.max())+1
    x=float(xs.mean()); y=float(ys.mean())
    if len(xs) < 3:
        angle = 0.0
    else:
        dx=xs.astype(float)-x; dy=ys.astype(float)-y
        mu20=float(np.mean(dx*dx)); mu02=float(np.mean(dy*dy)); mu11=float(np.mean(dx*dy))
        angle=0.5*math.degrees(math.atan2(2*mu11,mu20-mu02))
    return {"area":int(len(xs)),"centroid":(x,y),"angle_deg":angle,"bbox":(x0,y0,x1,y1)}

def contact_ratio(hand_mask: np.ndarray, object_mask: np.ndarray, dilation_px: int = 8) -> float:
    import cv2
    hand=(np.asarray(hand_mask)>0).astype(np.uint8)
    obj=(np.asarray(object_mask)>0).astype(np.uint8)
    area=int(obj.sum())
    if area == 0: return 0.0
    kernel=cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(2*dilation_px+1,2*dilation_px+1))
    dilated=cv2.dilate(hand,kernel)
    return float(np.logical_and(dilated>0,obj>0).sum()/area)

def in_zone_ratio(object_mask: np.ndarray, zone_mask: np.ndarray) -> float:
    obj=np.asarray(object_mask)>0; area=int(obj.sum())
    return float(np.logical_and(obj,np.asarray(zone_mask)>0).sum()/area) if area else 0.0

def unwrap_principal_axis(angles_deg: list[float], max_gap: int = 5) -> np.ndarray:
    """Interpolate short missing spans and unwrap axes modulo 180 degrees."""
    values=np.asarray(angles_deg,dtype=float)
    if not len(values): return values
    idx=np.arange(len(values)); valid=np.isfinite(values)
    if not valid.any(): return np.zeros_like(values)
    # Only interpolate gaps up to max_gap; long gaps remain separated by a constant angle.
    filled=values.copy()
    missing=np.where(~valid)[0]
    for i in missing:
        left=np.where(valid & (idx<i))[0]; right=np.where(valid & (idx>i))[0]
        if len(left) and len(right):
            a,b=int(left[-1]),int(right[0])
            if b-a-1 <= max_gap: filled[i]=np.interp(i,[a,b],[values[a],values[b]])
            else: filled[i]=values[a]
        elif len(left): filled[i]=values[left[-1]]
        else: filled[i]=values[right[0]]
    rad=np.deg2rad(filled*2)
    return np.rad2deg(np.unwrap(rad))/2

def rotation_features(angles_deg: list[float], min_delta_deg: float = 1.0) -> dict:
    unwrapped=unwrap_principal_axis(angles_deg)
    if len(unwrapped)<2: return {"total_deg":0.0,"reversals":0,"net_deg":0.0}
    delta=np.diff(unwrapped)
    delta[np.abs(delta)<min_delta_deg]=0
    signs=np.sign(delta); signs=signs[signs!=0]
    reversals=int(np.sum(signs[1:]!=signs[:-1])) if len(signs)>1 else 0
    return {"total_deg":float(np.abs(delta).sum()),"reversals":reversals,"net_deg":float(delta.sum())}

def tracklet_stitch_cost(a: dict, b: dict, max_gap_frames: int, fps: float) -> float:
    gap=int(b["start_frame"])-int(a["end_frame"])
    if gap < 0 or gap > max_gap_frames: return float("inf")
    ca=np.asarray(a["last_centroid"],dtype=float); cb=np.asarray(b["first_centroid"],dtype=float)
    diagonal=max(float(a.get("diagonal",1)),1.0)
    distance=float(np.linalg.norm(ca-cb))/diagonal
    return distance + gap/max(fps,1e-6)*0.15
