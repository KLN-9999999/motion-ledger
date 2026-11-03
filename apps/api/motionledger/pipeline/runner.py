from __future__ import annotations
import asyncio, json, time, uuid, re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from ..config import settings
from ..db import Database
from ..schemas.procedure import ProcedureSpec
from ..schemas.report import StepObservation, RunMeta, VideoMeta, Primitive
from ..signals.physical import extract_physical_primitives
from ..engine.compare import compare
from ..meta.client import get_client, upload_video, run_recon, sam_track, verify_video, canonical_hash
from ..review.ledger import append_entry, utc_now_iso
from .events import hub
from .cache import ArtifactCache

class PipelineError(RuntimeError): pass

async def run_live_audit(db: Database, run_id: str, audit_id: str) -> None:
    runrow = db.one("SELECT * FROM runs WHERE id=?", (run_id,))
    audit = db.one("SELECT * FROM audits WHERE id=?", (audit_id,))
    video = db.one("SELECT * FROM videos WHERE id=?", (audit["video_id"],))
    procedure_row = db.one("SELECT * FROM procedures WHERE id=? AND version=?", (audit["procedure_id"], audit["procedure_version"]))
    sop = ProcedureSpec.model_validate_json(procedure_row["body_json"])
    cache = ArtifactCache(settings.data_dir / "cache")
    timings: dict[str, int] = {}
    total_cost = 0.0
    response_ids: list[str] = []
    cache_hits: list[str] = []
    client = None
    # Always acquire a fresh remote file only when a cache miss requires a model call.
    remote_file_id = None

    async def ensure_client():
        nonlocal client
        if client is None:
            if settings.offline:
                raise PipelineError("Offline mode cache miss: this stage has no saved artifact. Disable MOTIONLEDGER_OFFLINE to run Meta models.")
            client = get_client()
        return client

    async def ensure_remote_video(force_upload: bool = False) -> str:
        nonlocal remote_file_id
        if remote_file_id and not force_upload:
            return remote_file_id
        model_client = await ensure_client()
        hub.publish(run_id, "stage.progress", stage="meta_upload", message="Uploading normalized video to Meta Files API")
        started = time.monotonic()
        remote_file_id = await upload_video(model_client, Path(video["proxy_path"]))
        db.update("videos", "id", video["id"], {"file_id": remote_file_id, "status": "ready"})
        timings["meta_upload_ms"] = int((time.monotonic() - started) * 1000)
        return remote_file_id

    try:
        procedure_json = sop.model_dump(mode="json")
        procedure_hash = canonical_hash(procedure_json)

        # Recon cache: keyed by immutable proxy hash, procedure content and model.
        hub.publish(run_id, "stage.started", stage="recon", message="Muse Spark is mapping the scene")
        db.update("runs", "id", run_id, {"stage": "recon"})
        recon_key = cache.key("recon", {"video_sha256": video["sha256"], "procedure_sha256": procedure_hash,
                                         "model": settings.spark_model})
        cached_recon = cache.read_json(recon_key)
        if cached_recon and isinstance(cached_recon.get("recon"), dict):
            recon, recon_id = cached_recon["recon"], cached_recon.get("response_id")
            if recon_id: response_ids.append(recon_id)
            cache_hits.append("recon")
            hub.publish(run_id, "stage.completed", stage="recon", cached=True,
                        activities=len(recon.get("activities", [])), issues=len(recon.get("camera_issues", [])))
        else:
            started = time.monotonic()
            model_client = await ensure_client()
            file_id = await ensure_remote_video()
            recon, recon_id, recon_usage = await run_recon(model_client, file_id, procedure_json, video["duration_s"], video["fps"])
            response_ids.append(recon_id)
            timings["recon_ms"] = int((time.monotonic() - started) * 1000)
            recon_cost = _spark_cost(recon_usage)
            total_cost += recon_cost
            cache.write_json(recon_key, {"recon": recon, "response_id": recon_id, "usage": recon_usage})
            _log_model_call(db, run_id, settings.spark_model, "/v1/responses", {"procedure": sop.id, "file_id": file_id, "stage": "recon"}, recon_id, recon_usage, timings["recon_ms"], recon_cost)
            hub.publish(run_id, "stage.completed", stage="recon", cached=False,
                        activities=len(recon.get("activities", [])), issues=len(recon.get("camera_issues", [])))

        # SAM concept cache: raw adapted JSONL and decoded NPZ are both required for a hit.
        hub.publish(run_id, "stage.started", stage="sam_tracking", message="SAM 3.1 is tracking procedure objects")
        db.update("runs", "id", run_id, {"stage": "sam_tracking"})
        semaphore = asyncio.Semaphore(settings.sam_concurrency)
        sam_results: dict[str, dict[str, Any]] = {}

        async def one_track(obj):
            nonlocal total_cost
            async with semaphore:
                concept = obj["concept"]
                object_key = obj["key"]
                safe_key = re.sub(r"[^a-zA-Z0-9_-]", "_", object_key)[:40]
                log_path = settings.data_dir / "logs" / run_id / f"sam-{safe_key}.jsonl"
                track_path = settings.data_dir / "tracks" / run_id / f"sam-{safe_key}.npz"
                cache_key = cache.key("sam_video", {"video_sha256": video["sha256"], "model": settings.sam_model,
                    "object_key": object_key, "concept": concept,
                    "metadata": {"mask_encoding": "one_bit", "include_confidence": "true"}})
                summary = cache.read_json(cache_key, "summary.json")
                names = ("events.jsonl", "tracks.npz")
                if summary and cache.has_files(cache_key, names):
                    cache.copy_file(cache_key, "events.jsonl", log_path)
                    cache.copy_file(cache_key, "tracks.npz", track_path)
                    result = {**summary, "log_path": str(log_path), "track_path": str(track_path), "cached": True}
                    if result.get("response_id"): response_ids.append(result["response_id"])
                    cache_hits.append(f"sam:{object_key}")
                else:
                    model_client = await ensure_client()
                    file_id = await ensure_remote_video()
                    started = time.monotonic()
                    result = await sam_track(model_client, file_id, concept, log_path, track_path)
                    elapsed = int((time.monotonic() - started) * 1000)
                    timings[f"sam_{object_key}_ms"] = elapsed
                    cost = max(50, int(video["frames"])) * 0.20 / 1000
                    total_cost += cost
                    cache.store_file(cache_key, "events.jsonl", log_path)
                    cache.store_file(cache_key, "tracks.npz", track_path)
                    summary_to_cache = {k: result[k] for k in ("events", "response_id", "records", "objects") if k in result}
                    cache.write_json(cache_key, summary_to_cache, "summary.json")
                    if result.get("response_id"): response_ids.append(result["response_id"])
                    _log_model_call(db, run_id, settings.sam_model, "/v1/responses", {"file_id": file_id, "concept": concept},
                                    result.get("response_id"), {"frames_estimated": video["frames"], "events": result["events"]}, elapsed, cost)
                    result["cached"] = False
                sam_results[object_key] = {"events": result["events"], "response_id": result.get("response_id"),
                    "records": result.get("records", 0), "objects": result.get("objects", []),
                    "cached": bool(result.get("cached")), "log_path": f"/api/runs/{run_id}/sam/{safe_key}.jsonl"}
                hub.publish(run_id, "stage.progress", stage="sam_tracking", object=object_key,
                            events=result["events"], cached=bool(result.get("cached")))

        await asyncio.gather(*(one_track(obj.model_dump()) for obj in sop.objects))
        track_summary_path = settings.data_dir / "tracks" / run_id / "summary.json"
        track_summary_path.parent.mkdir(parents=True, exist_ok=True)
        track_summary_path.write_text(json.dumps(sam_results, indent=2), encoding="utf-8")
        hub.publish(run_id, "stage.completed", stage="sam_tracking", objects=len(sam_results),
                    mask_records=sum(item.get("records", 0) for item in sam_results.values()),
                    cached=sum(bool(item.get("cached")) for item in sam_results.values()))

        # Deterministic physical signals over decoded SAM masks.
        hub.publish(run_id, "stage.started", stage="physical_signals", message="Measuring mask contact, carrying and tool rotation")
        db.update("runs", "id", run_id, {"stage": "physical_signals"})
        project_root = Path(__file__).resolve().parents[4]
        threshold_path = project_root / "config" / "signals.yaml"
        with threshold_path.open("r", encoding="utf-8") as fh:
            import yaml
            signal_thresholds = yaml.safe_load(fh) or {}
        track_files = {obj.key: settings.data_dir / "tracks" / run_id / f"sam-{re.sub(r'[^a-zA-Z0-9_-]', '_', obj.key)[:40]}.npz"
                       for obj in sop.objects}
        physical_primitives = await asyncio.to_thread(
            extract_physical_primitives, procedure=sop, track_files=track_files,
            frame_count=int(video["frames"]), fps=float(video["fps"]),
            width=int(video["width"]), height=int(video["height"]), thresholds=signal_thresholds)
        hub.publish(run_id, "stage.completed", stage="physical_signals", primitives=len(physical_primitives),
                    types=sorted({p.type for p in physical_primitives}))

        # Re-verify intentionally bypasses only the Muse Spark verdict cache; SAM and
        # reconnaissance remain reusable. All other runs consume a matching verdict cache.
        hub.publish(run_id, "stage.started", stage="step_verification", message="Muse Spark is verifying the procedure against the video")
        db.update("runs", "id", run_id, {"stage": "step_verification"})
        verification_key = cache.key("verification", {"video_sha256": video["sha256"], "procedure_sha256": procedure_hash,
            "model": settings.spark_model, "schema": "motionledger-verification-v1"})
        cached_verification = None if runrow["mode"] == "reverify" else cache.read_json(verification_key)
        started = time.monotonic()
        if cached_verification and isinstance(cached_verification.get("verdicts"), dict):
            verdicts = cached_verification["verdicts"]
            verification_id = cached_verification.get("response_id")
            usage = cached_verification.get("usage", {})
            if verification_id: response_ids.append(verification_id)
            cache_hits.append("verification")
            hub.publish(run_id, "stage.completed", stage="step_verification", cached=True,
                        observations=sum(len(item.get("instances", [])) for item in verdicts.get("steps", [])))
        else:
            model_client = await ensure_client()
            file_id = await ensure_remote_video(force_upload=(runrow["mode"] == "reverify" and not remote_file_id))
            verdicts, verification_id, usage = await verify_video(model_client, file_id, procedure_json, video["frames"], video["fps"])
            response_ids.append(verification_id)
            timings["verification_ms"] = int((time.monotonic() - started) * 1000)
            cache.write_json(verification_key, {"verdicts": verdicts, "response_id": verification_id, "usage": usage})
            cost = _spark_cost(usage)
            total_cost += cost
            _log_model_call(db, run_id, settings.spark_model, "/v1/responses", {"file_id": file_id, "procedure": sop.id, "stage": "verify"},
                            verification_id, usage, timings["verification_ms"], cost)
            hub.publish(run_id, "stage.completed", stage="step_verification", cached=False,
                        observations=sum(len(item.get("instances", [])) for item in verdicts.get("steps", [])))

        observations = _to_observations(sop, verdicts, recon, video["frames"], video["fps"])
        warnings = [
            "Live Meta model analysis: SAM 3.1 raw event logs and decoded per-frame mask/geometry artifacts are retained for auditability.",
            "This initial build derives contact, carrying and tool-rotation measurements. Automatic zone grounding/projection, zone-specific placement/insertion, calibrated signal/model fusion and set-of-mark evidence packets remain follow-on integration work.",
            "Treat every machine result as a possible discrepancy until reviewed by a qualified person."
        ]
        if settings.offline:
            warnings.append("Offline replay: every reused stage came from the local content-addressed artifact cache; a cache miss would stop the run without a network call.")
        if cache_hits:
            warnings.append("Reused cached stages: " + ", ".join(sorted(cache_hits)))
        if recon.get("camera_issues"):
            warnings.append("Camera issues: " + "; ".join(recon["camera_issues"]))
        if recon.get("unlisted_objects"):
            warnings.append("Unlisted objects: " + "; ".join(recon["unlisted_objects"]))
        run_meta = RunMeta(version=runrow["version"], parent=runrow["parent_id"],
            models={"perception": settings.sam_model, "reasoning": settings.spark_model},
            cost_usd=round(total_cost, 4), mode="LIVE_META_MODEL", created_at=runrow["created_at"])
        video_meta = VideoMeta(duration_s=video["duration_s"], fps=video["fps"], frames=video["frames"],
                               width=video["width"], height=video["height"])

        hub.publish(run_id, "stage.started", stage="comparison", message="Deterministic engine is reconciling the results")
        db.update("runs", "id", run_id, {"stage": "comparison"})
        report = compare(sop=sop, observations=observations, primitives=physical_primitives, video=video_meta,
                         audit_id=audit_id, run=run_meta, video_id=video["id"], warnings=warnings)
        append_entry(db, audit_id, "run.created", {"run_id": run_id, "version": runrow["version"], "parent": runrow["parent_id"],
                    "procedure_id": sop.id, "procedure_version": sop.version, "mode": "LIVE_META_MODEL", "models": run_meta.models,
                    "cache_hits": sorted(cache_hits)})
        append_entry(db, audit_id, "model.calls.completed", {"run_id": run_id, "response_ids": response_ids,
                    "timings_ms": timings, "cost_usd": round(total_cost, 4), "cache_hits": sorted(cache_hits)})
        head = db.one("SELECT hash FROM ledger_entries WHERE audit_id=? ORDER BY seq DESC LIMIT 1", (audit_id,))["hash"]
        report.ledger_head = head
        db.update("runs", "id", run_id, {"status": "completed", "stage": "completed", "completed_at": utc_now_iso(),
            "cost_usd": round(total_cost, 4), "report_json": report.model_dump(mode="json")})
        append_entry(db, audit_id, "run.completed", {"run_id": run_id, "finding_count": len(report.findings), "report_status": report.status})
        head = db.one("SELECT hash FROM ledger_entries WHERE audit_id=? ORDER BY seq DESC LIMIT 1", (audit_id,))["hash"]
        report.ledger_head = head
        db.update("runs", "id", run_id, {"report_json": report.model_dump(mode="json")})
        hub.publish(run_id, "run.completed", status=report.status, findings=len(report.findings),
                    cost_usd=round(total_cost, 4), sam_jobs=sam_results, cache_hits=sorted(cache_hits))
    except Exception as exc:
        db.update("runs", "id", run_id, {"status": "failed", "stage": "failed", "completed_at": utc_now_iso(), "error": str(exc)})
        try:
            append_entry(db, audit_id, "run.failed", {"run_id": run_id, "error": str(exc)[:1000]})
        except Exception:
            pass
        hub.publish(run_id, "run.failed", error=str(exc))
    finally:
        if client is not None:
            await client.close()

def _to_observations(sop: ProcedureSpec, result: dict, recon: dict, frames: int, fps: float) -> list[StepObservation]:
    activities = recon.get("activities",[])
    output=[]
    for step_result in result.get("steps",[]):
        step_id=step_result.get("step_id")
        step=next((s for s in sop.steps if s.id==step_id),None)
        if not step: continue
        for v in step_result.get("instances",[]):
            start=int(v.get("start_frame",-1)); end=int(v.get("end_frame",-1))
            cited=[]
            for f in v.get("decisive_frames",[]):
                try:
                    fi=int(f)
                    if 0 <= fi < frames: cited.append(fi)
                except (ValueError,TypeError): pass
            if start < 0 or start >= frames: start=-1
            if end < 0 or end >= frames: end=-1
            if start >= 0 and end >= 0 and end < start: start,end=end,start
            status=v.get("verdict","cannot_determine")
            if status not in {"performed","partially_performed","not_performed","cannot_determine"}: status="cannot_determine"
            obs_status = "observed" if status == "performed" else status
            confidence=float(v.get("confidence",0.0))
            if not 0 <= confidence <= 1: confidence=0
            missing=str(v.get("missing_evidence", ""))
            # Approximate expected window for omissions from prerequisite completion to clip end.
            expected=None
            if status == "not_performed":
                instance_key=v.get("instance_key","")
                same_pre=[]
                for prior in output:
                    if any(d.step==prior.step_id and (d.scope=="all" or prior.instance_key==instance_key) for d in step.requires):
                        if prior.end_frame >= 0: same_pre.append(prior.end_frame)
                start_expected=max(same_pre,default=0)
                related=[a for a in activities if step_id in a.get("likely_step_ids",[])]
                if related:
                    lo=min(max(0,int(a.get("start_s",0)*fps)) for a in related)
                    hi=max(min(frames-1,int(a.get("end_s",0)*fps)) for a in related)
                    expected=(lo,hi)
                    if any(a.get("visibility")=="occluded" for a in related):
                        status="cannot_determine"; obs_status="cannot_determine"; missing=missing or "Reconnaissance marked the candidate interval as occluded."
                else:
                    expected=(min(max(0,start_expected),frames-1),max(0,frames-1))
            if status == "partially_performed": obs_status="partially_performed"
            if status == "cannot_determine": obs_status="cannot_determine"
            output.append(StepObservation(id=f"muse-{step_id}-{v.get('instance_key','')}",step_id=step_id,
                instance_key=str(v.get("instance_key","")),status=obs_status,start_frame=start,end_frame=end,
                confidence=confidence,cited_frames=cited,rationale=str(v.get("rationale","")),source="muse",
                missing_evidence=missing,duration_s=(end-start)/fps if start>=0 and end>=start else None,
                expected_window=expected))
    return output

def _log_model_call(db: Database, run_id: str, model: str, endpoint: str, request: dict,
                    response_id: str | None, usage: dict, latency_ms: int, cost: float) -> None:
    db.insert_json("model_calls",{"id":str(uuid.uuid4()),"run_id":run_id,"model":model,"endpoint":endpoint,
        "request_sha256":canonical_hash(request),"response_id":response_id,"usage_json":usage,
        "latency_ms":latency_ms,"cost_usd":cost,"created_at":utc_now_iso()})


def _spark_cost(usage: dict[str, Any]) -> float:
    """Estimate Muse Spark token cost using uncached/cached input and output rates."""
    input_tokens = int(usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0)
    output_tokens = int(usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0)
    details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
    cached_tokens = min(input_tokens, int(details.get("cached_tokens", 0) or 0))
    uncached_tokens = input_tokens - cached_tokens
    return (uncached_tokens * 1.25 + cached_tokens * 0.15 + output_tokens * 4.25) / 1_000_000
