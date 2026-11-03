from __future__ import annotations
from dataclasses import dataclass
from ..perception.geometry import rotation_features

@dataclass
class Interval:
    start_frame: int
    end_frame: int
    peak: float
    duration_s: float

def hysteresis_intervals(values: list[float], start_threshold: float, stop_threshold: float,
                         min_duration_s: float, fps: float) -> list[Interval]:
    if fps <= 0: raise ValueError("fps must be positive")
    active=False; start=0; peak=0.0; out=[]
    for i,value in enumerate(values):
        if not active and value >= start_threshold:
            active=True; start=i; peak=value
        elif active:
            peak=max(peak,value)
            if value < stop_threshold:
                duration=(i-start)/fps
                if duration >= min_duration_s: out.append(Interval(start,i,peak,duration))
                active=False
    if active:
        duration=(len(values)-1-start)/fps
        if duration >= min_duration_s: out.append(Interval(start,len(values)-1,peak,duration))
    return out

def detect_rotation_events(angles: list[float], contact: list[float], *, fps: float,
                           min_total_deg: float = 90, min_reversals: int = 3,
                           contact_threshold: float = .02) -> list[dict]:
    if len(angles) != len(contact): raise ValueError("angles and contact must have equal length")
    events=[]; active=[]
    for i,c in enumerate(contact):
        if c >= contact_threshold: active.append(i)
        elif active:
            events.extend(_rotation_window(angles,active,fps,min_total_deg,min_reversals)); active=[]
    if active: events.extend(_rotation_window(angles,active,fps,min_total_deg,min_reversals))
    return events

def _rotation_window(angles, indices, fps, min_total, min_reversals):
    if not indices: return []
    values=[angles[i] for i in indices]
    stats=rotation_features(values)
    duration=(indices[-1]-indices[0])/fps
    if stats["total_deg"] >= min_total or stats["reversals"] >= min_reversals:
        return [{"start_frame":indices[0],"end_frame":indices[-1],"duration_s":duration,**stats}]
    return []
