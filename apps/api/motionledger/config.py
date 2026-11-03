from __future__ import annotations
import os
from pathlib import Path
from pydantic import BaseModel
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[3] / ".env", override=False)
    load_dotenv(Path.cwd() / ".env", override=False)
except ImportError:
    pass

class Settings(BaseModel):
    app_name: str = "MotionLedger"
    meta_base_url: str = os.getenv("META_BASE_URL", "https://api.meta.ai/v1")
    model_api_key: str = os.getenv("MODEL_API_KEY", "")
    spark_model: str = os.getenv("SPARK_MODEL", "muse-spark-1.3")
    sam_model: str = os.getenv("SAM_MODEL", "sam-3.1")
    voice_model: str = os.getenv("VOICE_MODEL", "muse-voice-transcribe-1.0")
    analysis_fps: int = int(os.getenv("ANALYSIS_FPS", "15"))
    max_long_edge: int = int(os.getenv("MAX_LONG_EDGE", "1280"))
    max_video_seconds: int = int(os.getenv("MAX_VIDEO_SECONDS", "300"))
    max_upload_bytes: int = int(os.getenv("MAX_UPLOAD_BYTES", str(1024**3)))
    sam_concurrency: int = int(os.getenv("SAM_CONCURRENCY", "4"))
    spark_concurrency: int = int(os.getenv("SPARK_CONCURRENCY", "6"))
    rpm_limit: int = int(os.getenv("RPM_LIMIT", "50"))
    files_ttl_seconds: int = int(os.getenv("FILES_TTL_SECONDS", "604800"))
    offline: bool = os.getenv("MOTIONLEDGER_OFFLINE", "0") == "1"
    demo_mode: bool = os.getenv("MOTIONLEDGER_DEMO_MODE", "0") == "1"
    data_dir: Path = Path(os.getenv("DATA_DIR", "./data")).resolve()
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./data/motionledger.db")

    @property
    def db_path(self) -> Path:
        if self.database_url.startswith("sqlite:///"):
            p = Path(self.database_url.removeprefix("sqlite:///"))
            return p if p.is_absolute() else (Path.cwd() / p).resolve()
        return self.data_dir / "motionledger.db"

settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
for folder in ("uploads", "proxies", "frames", "evidence", "logs", "cache", "clips"):
    (settings.data_dir / folder).mkdir(parents=True, exist_ok=True)
