from __future__ import annotations
from typing import Literal, Any
from pydantic import BaseModel, Field

InstanceStatus = Literal["observed", "missing", "incomplete", "unverifiable", "blocked", "skipped_optional"]
FindingType = Literal["OMISSION", "ORDER_VIOLATION", "PATTERN_VIOLATION", "INCOMPLETE", "REPEATED", "EXTRA_ACTION", "POSTCONDITION_FAILED", "UNVERIFIABLE"]

class EvidenceFrame(BaseModel):
    frame: int = Field(ge=0)
    t: float = Field(ge=0)
    role: str
    image: str | None = None
    clean_image: str | None = None

class Primitive(BaseModel):
    type: str
    objects: list[str] = Field(default_factory=list)
    zone: str = ""
    start_frame: int = 0
    end_frame: int = 0
    features: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(default=0.5, ge=0, le=1)

class StepObservation(BaseModel):
    id: str
    step_id: str
    instance_key: str = ""
    status: Literal["observed", "partially_performed", "not_performed", "cannot_determine"]
    start_frame: int = -1
    end_frame: int = -1
    confidence: float = Field(default=0, ge=0, le=1)
    cited_frames: list[int] = Field(default_factory=list)
    rationale: str = ""
    source: Literal["rules", "muse", "reviewer", "demo"] = "rules"
    missing_evidence: str = ""
    duration_s: float | None = None
    expected_window: tuple[int, int] | None = None

class FindingEvidence(BaseModel):
    frames: list[EvidenceFrame] = Field(min_length=1)
    measurements: list[str] = Field(default_factory=list)
    procedure_clause: str = ""
    rationale: str = ""
    response_id: str | None = None
    clip: str | None = None

class Finding(BaseModel):
    id: str
    type: FindingType
    instances: list[str] = Field(default_factory=list)
    severity: Literal["high", "medium", "low", "info", "check"]
    confidence: float = Field(ge=0, le=1)
    time_range_s: tuple[float, float]
    key_moment_s: float
    explanation: str
    evidence: FindingEvidence
    status: Literal["open", "confirmed", "dismissed"] = "open"

class RunMeta(BaseModel):
    version: int
    parent: str | None = None
    models: dict[str, str] = Field(default_factory=dict)
    cost_usd: float = 0
    mode: str = "LIVE"
    created_at: str

class VideoMeta(BaseModel):
    duration_s: float
    fps: float
    frames: int
    width: int
    height: int

class AuditReport(BaseModel):
    audit_id: str
    run: RunMeta
    video: VideoMeta
    procedure: dict[str, Any]
    status: Literal["CLEAN", "REVIEW"]
    instances: list[dict[str, Any]]
    findings: list[Finding]
    primitives: list[Primitive] = Field(default_factory=list)
    step_observations: list[StepObservation] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    ledger_head: str = ""
