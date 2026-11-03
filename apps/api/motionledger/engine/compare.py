from __future__ import annotations
from collections import defaultdict
from typing import Any
import hashlib
from ..schemas.procedure import ProcedureSpec, StepSpec
from ..schemas.report import AuditReport, Finding, FindingEvidence, EvidenceFrame, StepObservation, Primitive, RunMeta, VideoMeta

SEVERITY = {"critical": "high", "major": "medium", "minor": "low"}


def _zones_for(step: StepSpec, sop: ProcedureSpec) -> list[str]:
    target = step.target_zone
    if not target:
        return [""]
    if target.endswith("*"):
        prefix = target[:-1]
        return [z.key for z in sop.zones if z.key.startswith(prefix)]
    return [target]


def expand_instances(sop: ProcedureSpec) -> list[dict[str, Any]]:
    result = []
    for step in sop.steps:
        if step.repeat:
            zones = _zones_for(step, sop)
            if step.repeat.per == "zone":
                zones = zones[:step.repeat.count]
                # If a spec has a fixed count and wildcard target, an underspecified zone set is a schema error.
                if len(zones) < step.repeat.count:
                    raise ValueError(f"{step.id} repeats {step.repeat.count} times but only {len(zones)} target zones match")
                for zone in zones:
                    result.append({"id": f"{step.id}@{zone}", "step_id": step.id, "instance_key": zone,
                                   "title": step.title, "optional": step.optional, "criticality": step.criticality})
            else:
                for index in range(step.repeat.count):
                    result.append({"id": f"{step.id}@{index+1}", "step_id": step.id, "instance_key": str(index+1),
                                   "title": step.title, "optional": step.optional, "criticality": step.criticality})
        else:
            result.append({"id": step.id, "step_id": step.id, "instance_key": "", "title": step.title,
                           "optional": step.optional, "criticality": step.criticality})
    return result


def _frame(frame: int, fps: float, role: str, video_id: str) -> EvidenceFrame:
    frame = max(0, int(frame))
    t = frame / max(fps, 0.001)
    return EvidenceFrame(frame=frame, t=round(t, 3), role=role,
                         image=f"/api/videos/{video_id}/frames/{frame}.jpg",
                         clean_image=f"/api/videos/{video_id}/frames/{frame}.jpg")


def compare(
    *, sop: ProcedureSpec, observations: list[StepObservation], primitives: list[Primitive],
    video: VideoMeta, audit_id: str, run: RunMeta, video_id: str,
    end_state: dict[str, Any] | None = None, overrides: list[dict[str, Any]] | None = None,
    warnings: list[str] | None = None,
) -> AuditReport:
    """Pure comparison engine: no file I/O, DB access or model calls."""
    sop.validate_references()
    instances = expand_instances(sop)
    step_by_id = {s.id: s for s in sop.steps}
    obs_by_id: dict[str, StepObservation] = {}
    for obs in observations:
        obs_by_id[f"{obs.step_id}@{obs.instance_key}" if obs.instance_key else obs.step_id] = obs

    override_map: dict[str, dict] = {}
    for override in (overrides or []):
        if override.get("kind") in {"confirm_observation", "dismiss_finding", "add_observation", "retime", "relabel"}:
            key = override.get("instance_id")
            if key:
                override_map[key] = override

    # Resolve dependencies topologically, even when a user-authored procedure lists
    # dependent steps before their prerequisites.
    ordered_step_ids: list[str] = []
    visited_steps: set[str] = set()
    def visit_step(step_id: str) -> None:
        if step_id in visited_steps:
            return
        for dependency in step_by_id[step_id].requires:
            visit_step(dependency.step)
        visited_steps.add(step_id)
        ordered_step_ids.append(step_id)
    for candidate in sop.steps:
        visit_step(candidate.id)
    rank = {step_id: index for index, step_id in enumerate(ordered_step_ids)}
    ordered_instances = sorted(instances, key=lambda item: rank[item["step_id"]])

    # A dependency can block an instance, preventing one missed upstream action
    # from cascading into several downstream accusations.
    statuses: dict[str, dict[str, Any]] = {}
    for inst in ordered_instances:
        iid, step = inst["id"], step_by_id[inst["step_id"]]
        obs = obs_by_id.get(iid)
        override = override_map.get(iid)
        if override and override.get("kind") == "confirm_observation":
            data = override.get("payload", {})
            obs = StepObservation(id=f"review-{iid}", step_id=step.id, instance_key=inst["instance_key"],
                                  status="observed", start_frame=int(data.get("start_frame", 0)),
                                  end_frame=int(data.get("end_frame", data.get("start_frame", 0))),
                                  confidence=1.0, cited_frames=[int(data.get("start_frame", 0))],
                                  rationale=data.get("note", "Confirmed by reviewer"), source="reviewer")
        elif override and override.get("kind") == "add_observation":
            data = override.get("payload", {})
            obs = StepObservation(id=f"review-{iid}", step_id=step.id, instance_key=inst["instance_key"],
                                  status="observed", start_frame=int(data.get("start_frame", 0)),
                                  end_frame=int(data.get("end_frame", data.get("start_frame", 0))),
                                  confidence=1.0, cited_frames=[int(data.get("start_frame", 0))],
                                  rationale=data.get("note", "Added by reviewer"), source="reviewer")
        elif override and override.get("kind") == "retime" and obs:
            data = override.get("payload", {})
            obs = obs.model_copy(update={"start_frame": int(data.get("start_frame", obs.start_frame)),
                                         "end_frame": int(data.get("end_frame", obs.end_frame)),
                                         "source": "reviewer"})

        prereq_states = []
        for dep in step.requires:
            prereq_step = step_by_id[dep.step]
            prereq_ids = [x["id"] for x in instances if x["step_id"] == dep.step]
            if dep.scope == "same_instance" and inst["instance_key"]:
                candidate = f"{dep.step}@{inst['instance_key']}"
                prereq_ids = [candidate] if candidate in statuses else prereq_ids
            prereq_states.extend(statuses.get(pid, {}).get("status") for pid in prereq_ids)
        blocked = any(s in {"missing", "incomplete", "unverifiable", "blocked"} for s in prereq_states)
        if obs and obs.status in {"observed", "partially_performed"} and obs.confidence >= 0.60:
            duration = obs.duration_s if obs.duration_s is not None else max(0, obs.end_frame - obs.start_frame) / max(video.fps, .001)
            state = "incomplete" if (obs.status == "partially_performed" or duration + 1e-9 < step.min_duration_s) else "observed"
        elif obs and (obs.status == "cannot_determine" or obs.confidence < 0.60):
            state = "unverifiable"
        elif blocked:
            state = "blocked"
        elif step.optional:
            state = "skipped_optional"
        else:
            occlusion = (obs.expected_window[1] - obs.expected_window[0]) if (obs and obs.expected_window) else 0
            state = "unverifiable" if obs and obs.missing_evidence else "missing"
        statuses[iid] = {"status": state, "observation": obs, "instance": inst, "step": step}

    findings: list[Finding] = []
    def add_finding(kind: str, ids: list[str], severity: str, confidence: float,
                    frames: list[int], explanation: str, measurements: list[str],
                    rationale: str = "", procedure_clause: str = "") -> None:
        # Every finding has at least one source-video frame reference. Omission evidence
        # uses an expected-window frame rather than fabricated object evidence.
        valid_frames = [max(0, min(video.frames - 1, int(f))) for f in frames] if video.frames else [0]
        if not valid_frames: valid_frames = [0]
        uniq = sorted(set(valid_frames))[:6]
        evidence_frames = [_frame(f, video.fps, "decisive" if i == 0 else "context", video_id) for i, f in enumerate(uniq)]
        evidence_ranges: list[tuple[int, int]] = []
        for instance_id in ids:
            observation = statuses.get(instance_id, {}).get("observation")
            if not observation:
                continue
            if observation.start_frame >= 0 and observation.end_frame >= 0:
                evidence_ranges.append((observation.start_frame, observation.end_frame))
            elif observation.expected_window:
                evidence_ranges.append(tuple(map(int, observation.expected_window)))
        start = min((lo for lo, _ in evidence_ranges), default=uniq[0])
        end = max((hi for _, hi in evidence_ranges), default=uniq[-1])
        t0, t1 = sorted((start / max(video.fps, .001), end / max(video.fps, .001)))
        key_frame = uniq[0]
        findings.append(Finding(
            id=f"F-{hashlib.sha256((kind + "|" + "|".join(sorted(ids)) + "|" + explanation).encode()).hexdigest()[:8].upper()}", type=kind, instances=ids, severity=severity,
            confidence=round(max(0, min(1, confidence)), 3), time_range_s=(round(t0, 3), round(max(t1, t0), 3)),
            key_moment_s=round(key_frame / max(video.fps, .001), 3), explanation=explanation,
            evidence=FindingEvidence(frames=evidence_frames, measurements=measurements,
                                     procedure_clause=procedure_clause, rationale=rationale)))

    # Per-step discrepancy counts.
    for step in sop.steps:
        step_instances = [x for x in instances if x["step_id"] == step.id]
        rows = [statuses[x["id"]] for x in step_instances]
        active = [(x, row) for x, row in zip(step_instances, rows) if row["status"] != "blocked"]
        missing = [(x, row) for x, row in active if row["status"] == "missing"]
        incomplete = [(x, row) for x, row in active if row["status"] == "incomplete"]
        unknown = [(x, row) for x, row in active if row["status"] == "unverifiable"]
        observed = [(x, row) for x, row in active if row["status"] == "observed"]
        sev = SEVERITY[step.criticality]
        if missing or incomplete:
            affected = missing + incomplete
            kind = "OMISSION" if len(missing) == len(active) and not incomplete else "INCOMPLETE"
            frames: list[int] = []
            measurements = []
            for instance, row in affected:
                observation = row["observation"]
                if observation and observation.expected_window:
                    lo, hi = observation.expected_window
                    frames.extend([lo, (lo + hi) // 2, hi])
                    measurements.append(f"{instance['id']} expected window: {lo / max(video.fps, .001):.2f}–{hi / max(video.fps, .001):.2f} s")
                elif observation and observation.start_frame >= 0:
                    frames.append(observation.start_frame)
                else:
                    frames.append(max(0, video.frames - 1))
                if observation and observation.rationale:
                    measurements.append(f"{instance['id']} rationale: {observation.rationale}")
                if observation and observation.missing_evidence:
                    measurements.append(f"{instance['id']} visibility note: {observation.missing_evidence}")
            labels = [instance["id"] for instance, _ in affected]
            if missing:
                measurements.append(f"Missing instances: {', '.join(x['id'] for x, _ in missing)}")
            if incomplete:
                measurements.append(f"Incomplete instances: {', '.join(x['id'] for x, _ in incomplete)}")
                measurements.append(f"Minimum duration: {step.min_duration_s:.1f} s")
            add_finding(kind, labels, sev,
                        min((row["observation"].confidence if row["observation"] else 0.6) for _, row in affected),
                        frames, f"{len(missing)} required instance(s) of '{step.title}' lack supporting evidence and {len(incomplete)} are incomplete.",
                        measurements, procedure_clause=step.title)
        if unknown:
            frames = [row["observation"].start_frame for _, row in unknown if row["observation"] and row["observation"].start_frame >= 0]
            add_finding("UNVERIFIABLE", [x["id"] for x, _ in unknown], "check", 0.5,
                        frames or [0], f"The decisive moment for {', '.join(x['id'] for x, _ in unknown)} could not be seen clearly. This is a needs-check item, not a defect.",
                        [row["observation"].missing_evidence for _, row in unknown if row["observation"] and row["observation"].missing_evidence] or ["Visibility is insufficient to decide."],
                        procedure_clause=step.title)
        # Repeated evidence beyond the specified count is tracked on observations.
        extra = [p for p in primitives if p.type.lower() in {step.action, f"{step.action}ed", f"{step.action}d"} and p.confidence >= 0.7 and p.zone in _zones_for(step, sop)]
        if step.repeat and len(extra) > step.repeat.count:
            add_finding("REPEATED", [step.id], "info", min(p.confidence for p in extra[step.repeat.count:]),
                        [p.start_frame for p in extra[step.repeat.count:]], f"More than {step.repeat.count} instances of '{step.title}' were measured.",
                        [f"Measured instances: {len(extra)}; allowed: {step.repeat.count}"], procedure_clause=step.title)
        # Order/pattern use performed instances only.
        if step.repeat and step.repeat.pattern:
            observed_rows = [(x, row) for x, row in observed if row["observation"] and row["observation"].start_frame >= 0]
            actual = [x["instance_key"] for x, row in sorted(observed_rows, key=lambda pair: pair[1]["observation"].start_frame)]
            expected = step.repeat.pattern
            if len(actual) >= 2 and [x for x in actual if x in expected] != [x for x in expected if x in actual]:
                frames = [row["observation"].start_frame for _, row in observed_rows]
                add_finding("PATTERN_VIOLATION", [x["id"] for x, _ in observed_rows], sev,
                            min(row["observation"].confidence for _, row in observed_rows), frames,
                            f"The observed order ({' → '.join(actual)}) differs from the specified pattern ({' → '.join(expected)}).",
                            [f"Expected: {' → '.join(expected)}", f"Observed: {' → '.join(actual)}"], procedure_clause=step.title)

    # Dependency-order checks. All-scope requires the full predecessor to finish first.
    for step in sop.steps:
        current = [x for x in instances if x["step_id"] == step.id]
        for dep in step.requires:
            parents = [x for x in instances if x["step_id"] == dep.step]
            pairs = []
            if dep.scope == "same_instance":
                pmap = {x["instance_key"]: x for x in parents}
                pairs = [(pmap[x["instance_key"]], x) for x in current if x["instance_key"] in pmap]
            else:
                pairs = [(p, c) for p in parents for c in current]
            for parent, child in pairs:
                po = statuses[parent["id"]]["observation"]; co = statuses[child["id"]]["observation"]
                if po and co and po.status == "observed" and co.status == "observed" and po.end_frame >= 0 and co.start_frame >= 0 and co.start_frame < po.end_frame - round(.5 * video.fps):
                    add_finding("ORDER_VIOLATION", [parent["id"], child["id"]], "high",
                                min(po.confidence, co.confidence), [co.start_frame, po.end_frame],
                                f"'{step_by_id[dep.step].title}' had not finished when '{step.title}' began.",
                                [f"Prerequisite ended at {po.end_frame / video.fps:.2f} s", f"Dependent began at {co.start_frame / video.fps:.2f} s"],
                                procedure_clause=f"{step_by_id[dep.step].title} before {step.title}")

    # Postconditions are only judged when the caller provides measured end-state data.
    if end_state:
        for pc in sop.postconditions:
            actual = end_state.get(pc.id)
            if actual is None: continue
            passed = (actual == pc.equals) if pc.check == "count_in_zones" else ((bool(actual) == bool(pc.equals)) if pc.check == "present_in_zone" else (not bool(actual)))
            if not passed:
                add_finding("POSTCONDITION_FAILED", [pc.id], "high", 0.9, [max(0, video.frames - 1)],
                            f"End state does not satisfy: {pc.description}.", [f"Expected: {pc.equals}", f"Measured: {actual}"],
                            procedure_clause=pc.description)

    # Unclaimed, high-confidence primitives are surfaced for reviewer interpretation.
    claimed = {(s.action, x["instance_key"]) for s in sop.steps for x in instances if x["step_id"] == s.id}
    for primitive in primitives:
        if primitive.confidence >= .7 and primitive.type not in {s.action for s in sop.steps} and primitive.type not in {"contact", "carried", "placed", "inserted", "rotated", "idle"}:
            add_finding("EXTRA_ACTION", [f"primitive:{primitive.type}"], "info", primitive.confidence,
                        [primitive.start_frame], f"Unmapped activity '{primitive.type}' was detected.",
                        [str(primitive.features)], procedure_clause="No matching procedure step")

    # Apply finding dismissals/confirmations by exact finding ID, then merge duplicate cards lightly.
    finding_overrides = {o.get("instance_id"): o for o in (overrides or []) if o.get("kind") == "dismiss_finding"}
    for finding in findings:
        if finding.id in finding_overrides:
            finding.status = "dismissed"

    public_instances = []
    # Preserve the author's declared order in the report and review timeline, while
    # keeping prerequisite-first ordering internal to status resolution.
    for instance in instances:
        iid = instance["id"]
        row = statuses[iid]
        obs = row["observation"]
        public_instances.append({"id": iid, "step_id": row["step"].id, "title": row["step"].title,
                                 "status": row["status"], "frames": [obs.start_frame, obs.end_frame] if obs else [],
                                 "confidence": obs.confidence if obs else 0,
                                 "rationale": obs.rationale if obs else "No verified observation."})
    active_findings = [f for f in findings if f.status != "dismissed"]
    report_status = "CLEAN" if not active_findings else "REVIEW"
    return AuditReport(audit_id=audit_id, run=run, video=video,
                       procedure={"id": sop.id, "version": sop.version, "title": sop.title},
                       status=report_status, instances=public_instances, findings=findings,
                       primitives=primitives, step_observations=observations, warnings=warnings or [])
