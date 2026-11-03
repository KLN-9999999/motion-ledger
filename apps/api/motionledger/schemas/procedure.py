from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, Field, ConfigDict

Action = Literal["place", "remove", "insert", "tighten", "loosen", "connect", "press", "apply", "inspect", "other"]

class ObjectSpec(BaseModel):
    key: str
    concept: str = Field(min_length=1, description="1-4 word concrete noun phrase for SAM")
    role: Literal["actor", "part", "tool", "fixture"]
    expected_count: int = Field(default=1, ge=1)

class ZoneSpec(BaseModel):
    key: str
    label: str
    anchor: str = ""
    source: Literal["manual", "grounding", "sam"] = "manual"
    # normalized x/y/r or x/y/width/height geometry when manually annotated
    geometry: dict | None = None

class Dependency(BaseModel):
    step: str
    scope: Literal["all", "same_instance"] = "all"

class Repeat(BaseModel):
    count: int = Field(ge=1)
    per: Literal["zone", "object"] = "zone"
    pattern: list[str] = Field(default_factory=list)

class EvidenceHints(BaseModel):
    performed: str = ""
    skipped: str = ""

class StepSpec(BaseModel):
    id: str
    title: str
    action: Action
    subject: str
    tool: str = ""
    target_zone: str = ""
    repeat: Repeat | None = None
    requires: list[Dependency] = Field(default_factory=list)
    min_duration_s: float = Field(default=0, ge=0)
    optional: bool = False
    criticality: Literal["critical", "major", "minor"] = "major"
    evidence_hints: EvidenceHints = Field(default_factory=EvidenceHints)

class Postcondition(BaseModel):
    id: str
    description: str
    check: Literal["count_in_zones", "present_in_zone", "absent_from_zone"]
    object: str
    zones: str
    equals: int = 1

class ProcedureSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = "flange-assembly"
    version: int = Field(default=1, ge=1)
    title: str
    camera: str = "Fixed, top-down, whole fixture and tool tray in frame"
    objects: list[ObjectSpec]
    zones: list[ZoneSpec]
    steps: list[StepSpec]
    postconditions: list[Postcondition] = Field(default_factory=list)
    vocabulary: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)

    def validate_references(self) -> None:
        object_keys = {obj.key for obj in self.objects}
        zone_keys = {zone.key for zone in self.zones}
        step_ids = {step.id for step in self.steps}
        if len(step_ids) != len(self.steps):
            raise ValueError("Step ids must be unique")
        if len(object_keys) != len(self.objects) or len(zone_keys) != len(self.zones):
            raise ValueError("Object and zone keys must be unique")
        for step in self.steps:
            if step.subject not in object_keys:
                raise ValueError(f"{step.id}: unknown subject {step.subject}")
            if step.tool and step.tool not in object_keys:
                raise ValueError(f"{step.id}: unknown tool {step.tool}")
            if step.target_zone and not (step.target_zone in zone_keys or step.target_zone.endswith("*")):
                raise ValueError(f"{step.id}: unknown target zone {step.target_zone}")
            for dep in step.requires:
                if dep.step not in step_ids:
                    raise ValueError(f"{step.id}: unknown prerequisite {dep.step}")
        # Detect cycles with DFS.
        graph = {step.id: [d.step for d in step.requires] for step in self.steps}
        visiting: set[str] = set(); visited: set[str] = set()
        def visit(node: str) -> None:
            if node in visiting:
                raise ValueError("Procedure dependency graph contains a cycle")
            if node in visited:
                return
            visiting.add(node)
            for parent in graph[node]:
                visit(parent)
            visiting.remove(node); visited.add(node)
        for node in graph:
            visit(node)

class ProcedureCreate(BaseModel):
    procedure: ProcedureSpec

class ProcedureCompileRequest(BaseModel):
    text: str = Field(min_length=10)
    title: str | None = None
