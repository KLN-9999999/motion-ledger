from __future__ import annotations
import hashlib, json, subprocess
from pathlib import Path
import cv2
from ..config import settings

class MediaError(RuntimeError): pass

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""): h.update(block)
    return h.hexdigest()

def _run(args: list[str], timeout: int = 900) -> str:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise MediaError("ffmpeg and ffprobe must be installed and available on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaError("Video processing exceeded the time limit") from exc
    if result.returncode != 0:
        raise MediaError(result.stderr[-2000:] or "Media processing failed")
    return result.stdout

def probe_video(path: Path) -> dict:
    raw = _run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_streams", "-show_format", "-of", "json", str(path)])
    data = json.loads(raw)
    streams = data.get("streams") or []
    if not streams: raise MediaError("The uploaded file does not contain a video stream")
    stream = streams[0]
    duration = float(stream.get("duration") or data.get("format", {}).get("duration") or 0)
    width, height = int(stream["width"]), int(stream["height"])
    fps_text = stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "0/1"
    try:
        numerator, denominator = fps_text.split("/"); fps = float(numerator) / max(float(denominator), 1)
    except Exception: fps = 0
    if duration <= 0 or width <= 0 or height <= 0: raise MediaError("Could not read video duration or dimensions")
    if duration > settings.max_video_seconds: raise MediaError(f"Video is {duration:.1f}s; the configured limit is {settings.max_video_seconds}s")
    return {"duration_s": duration, "source_fps": fps, "width": width, "height": height,
            "has_audio": any(s.get("codec_type") == "audio" for s in data.get("streams", []))}

def ingest_video(source: Path, video_id: str) -> dict:
    meta = probe_video(source)
    longest = max(meta["width"], meta["height"])
    scale = min(1.0, settings.max_long_edge / longest)
    out_w = max(2, int(meta["width"] * scale) // 2 * 2)
    out_h = max(2, int(meta["height"] * scale) // 2 * 2)
    proxy = settings.data_dir / "proxies" / f"{video_id}.mp4"
    vf = f"fps={settings.analysis_fps},scale={out_w}:{out_h}:flags=lanczos"
    _run(["ffmpeg", "-y", "-i", str(source), "-vf", vf, "-fps_mode", "cfr",
          "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
          "-g", str(settings.analysis_fps), "-an", "-movflags", "+faststart", str(proxy)])
    probe = json.loads(_run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
                             "-show_entries", "stream=width,height,avg_frame_rate,nb_read_frames", "-of", "json", str(proxy)]))
    stream = probe["streams"][0]
    frames = int(stream.get("nb_read_frames") or round(meta["duration_s"] * settings.analysis_fps))
    return {"proxy_path": proxy, "sha256": sha256_file(proxy), "duration_s": frames / settings.analysis_fps,
            "fps": float(settings.analysis_fps), "frames": frames, "width": int(stream["width"]),
            "height": int(stream["height"]), "has_audio": meta["has_audio"]}

def read_frame(path: Path, frame_index: int) -> bytes:
    cap = cv2.VideoCapture(str(path))
    try:
        cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, frame_index))
        ok, frame = cap.read()
        if not ok: raise MediaError(f"Frame {frame_index} is outside the video")
        ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 88])
        if not ok: raise MediaError("Could not encode evidence frame")
        return encoded.tobytes()
    finally:
        cap.release()

def extract_jpeg_data_url(path: Path, frame_index: int, width: int | None = None) -> str:
    data = read_frame(path, frame_index)
    import base64
    return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")
