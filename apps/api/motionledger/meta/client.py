from __future__ import annotations
import os, json, asyncio, time, hashlib
from pathlib import Path
from contextlib import aclosing
from typing import Any
try:
    from openai import AsyncOpenAI
except ImportError:
    AsyncOpenAI = None
from ..config import settings

class MetaUnavailable(RuntimeError): pass


async def close_text_lanes(stream):
    """Synthesize the output_text.done event missing from some SAM streams.

    The SAM parser expects this close marker to finalize each text content lane.
    The exact adapted events are persisted so a future decoder can replay them.
    """
    closed: set[tuple[Any, Any, Any]] = set()
    async for event in stream:
        data = event.model_dump() if hasattr(event, "model_dump") else dict(event)
        lane = (data.get("item_id"), data.get("output_index"), data.get("content_index"))
        if data.get("type") == "response.output_text.done":
            if lane not in closed:
                closed.add(lane)
                yield data
            continue
        yield data
        part = data.get("part")
        if (data.get("type") == "response.content_part.done" and lane not in closed
                and isinstance(part, dict) and part.get("type") == "output_text"):
            closed.add(lane)
            yield {"type":"response.output_text.done", "item_id":lane[0],
                   "output_index":lane[1], "content_index":lane[2],
                   "text":part.get("text", "")}

def get_client() -> AsyncOpenAI:
    if AsyncOpenAI is None:
        raise MetaUnavailable("Install backend dependencies with pip install -e .[dev] to enable Meta model calls.")
    if not settings.model_api_key:
        raise MetaUnavailable("MODEL_API_KEY is not set. Add it to apps/api/.env to enable live Meta model analysis.")
    return AsyncOpenAI(base_url=settings.meta_base_url, api_key=settings.model_api_key, max_retries=4, timeout=600)

def canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()

async def upload_video(client: AsyncOpenAI, path: Path) -> str:
    uploaded = await client.files.create(file=(path.name, path.read_bytes(), "video/mp4"), purpose="user_data",
                                         expires_after={"anchor": "created_at", "seconds": settings.files_ttl_seconds})
    return uploaded.id

async def _wait_response(client: AsyncOpenAI, response: Any, timeout_s: int = 480) -> Any:
    deadline = time.monotonic() + timeout_s
    missing_retries = 0
    while getattr(response, "status", None) in {"queued", "in_progress"}:
        if time.monotonic() > deadline: raise TimeoutError("Meta response polling timed out")
        await asyncio.sleep(2)
        try:
            response = await client.responses.retrieve(response.id)
            missing_retries = 0
        except Exception as exc:
            if getattr(exc, "status_code", None) == 404 and missing_retries < 2:
                missing_retries += 1
                await asyncio.sleep(1)
                continue
            raise
    if getattr(response, "status", None) != "completed":
        raise RuntimeError(f"Meta response ended with status {getattr(response, 'status', 'unknown')}")
    return response

async def compile_procedure(text: str, title: str | None = None) -> dict:
    client = get_client()
    prompt = f"Title hint: {title or 'Infer from the work instruction'}\n\nWork instruction:\n{text}"
    resp = await client.responses.create(model=settings.spark_model, reasoning={"effort": "high"},
        instructions=PROCEDURE_COMPILER_INSTRUCTIONS, input=prompt,
        text={"format": {"type": "json_schema", "name": "ProcedureSpec", "schema": PROCEDURE_SCHEMA}},
        max_output_tokens=16000)
    return json.loads(resp.output_text)

async def run_recon(client: AsyncOpenAI, file_id: str, procedure: dict, duration_s: float, fps: float) -> tuple[dict, str, dict]:
    resp = await client.responses.create(model=settings.spark_model, background=True,
        reasoning={"effort": "medium"}, instructions=RECON_INSTRUCTIONS,
        input=[{"type":"message","role":"user","content":[
            {"type":"input_text","text":f"Procedure:\n{json.dumps(procedure)}\n\nThe video is {duration_s:.1f}s long at {fps} fps."},
            {"type":"input_video","file_id":file_id}]}],
        text={"format":{"type":"json_schema","name":"ReconReport","schema":RECON_SCHEMA}},
        max_output_tokens=12000)
    resp = await _wait_response(client, resp)
    usage = resp.usage.model_dump() if getattr(resp, "usage", None) else {}
    return json.loads(resp.output_text), resp.id, usage

async def sam_track(client: AsyncOpenAI, file_id: str, concept: str, log_path: Path,
                    track_path: Path) -> dict:
    """Stream SAM, preserve adapted wire events, parse masks and save track geometry."""
    from meta_sam_parser import parse_responses_stream, video_segmentation_format
    from ..perception.sam import decode_video_records, save_track_artifact

    log_path.parent.mkdir(parents=True, exist_ok=True)
    stream = await client.responses.create(model=settings.sam_model, stream=True,
        input=[{"type":"message","role":"user","content":[
            {"type":"input_text","text":concept},{"type":"input_video","file_id":file_id}]}],
        metadata={"mask_encoding":"one_bit","include_confidence":"true"})
    state: dict[str, Any] = {"events":0, "response_id":None}

    async def tee_events(source):
        with log_path.open("w", encoding="utf-8") as fh:
            async for data in source:
                fh.write(json.dumps(data, separators=(",",":"), default=str) + "\n")
                state["events"] += 1
                response = data.get("response") or {}
                if response.get("id"):
                    state["response_id"] = response["id"]
                yield data

    parsed = parse_responses_stream(tee_events(close_text_lanes(stream)), video_segmentation_format())
    result = await parsed.final_result()
    if getattr(result.outcome, "status", None) != "completed":
        raise RuntimeError(f"SAM tracking did not complete for concept '{concept}': {result.outcome}")
    decoded = decode_video_records(result.records)
    summary = save_track_artifact(decoded, track_path)
    return {"events":state["events"], "response_id":state["response_id"],
            "log_path":str(log_path), "track_path":str(track_path), **summary}

async def verify_video(client: AsyncOpenAI, file_id: str, procedure: dict, frame_count: int, fps: float) -> tuple[dict, str, dict]:
    instructions = VERIFIER_INSTRUCTIONS.replace("{{PROCEDURE}}", json.dumps(procedure, ensure_ascii=False))
    schema = VERIFICATION_SCHEMA
    resp = await client.responses.create(model=settings.spark_model, background=True,
        reasoning={"effort":"high","summary":"auto"}, instructions=instructions,
        input=[{"type":"message","role":"user","content":[
            {"type":"input_text","text":f"Video has {frame_count} frames at {fps} fps. Verify every required and optional step. Cite zero-based frame indices only."},
            {"type":"input_video","file_id":file_id}]}],
        text={"format":{"type":"json_schema","name":"MotionLedgerVerification","schema":schema}},
        max_output_tokens=16000, store=True)
    resp = await _wait_response(client, resp)
    parsed = json.loads(resp.output_text)
    return parsed, resp.id, (resp.usage.model_dump() if getattr(resp, "usage", None) else {})

PROCEDURE_COMPILER_INSTRUCTIONS = """You convert manufacturing work instructions into a camera-checkable procedure. Use one physical action per step. Actions: place, remove, insert, tighten, loosen, connect, press, apply, inspect, other. Include all parts, tools, fixtures and a hand object. Concepts are 1-4 word concrete noun phrases, no verbs, lists, negation or position words. Express order only with requires. Use same_instance for per-zone predecessor actions and all for global prerequisites. Use repeat for counted actions and pattern for prescribed sequences. Never invent unsupported steps; record ambiguity in open_questions. Return only schema-conforming JSON."""
RECON_INSTRUCTIONS = """You are inspecting an assembly video before a detailed audit. Do not judge compliance. Say whether each procedure object is visible; suggest up to 3 better 1-4 word noun phrases where needed. List distinct physical activities in order with approximate start/end seconds, involved objects and likely step ids. Mark visibility occluded when hands, bodies or frame edges hide the action. List unlisted objects and camera issues. Describe only what is visible."""
VERIFIER_INSTRUCTIONS = """You verify an assembly procedure from a video. Procedure JSON: {{PROCEDURE}}. For every listed step instance decide performed, partially_performed, not_performed, or cannot_determine. Use cannot_determine whenever decisive action is occluded, blurred, or outside frame; do not call unseen work omitted when visibility is poor. Give start/end frame (-1 when unknown), decisive frame numbers from this video, confidence 0-1, rationale at most two sentences, and missing_evidence. Never infer a torque value from appearance. Model verdicts are evidence candidates, not final findings. Return only schema-conforming JSON."""

PROCEDURE_SCHEMA = {"type":"object","properties":{
 "title":{"type":"string"},"camera":{"type":"string"},
 "objects":{"type":"array","items":{"type":"object","properties":{"key":{"type":"string"},"concept":{"type":"string"},"role":{"type":"string","enum":["actor","part","tool","fixture"]},"expected_count":{"type":"integer"}},"required":["key","concept","role","expected_count"],"additionalProperties":False}},
 "zones":{"type":"array","items":{"type":"object","properties":{"key":{"type":"string"},"label":{"type":"string"},"anchor":{"type":"string"},"source":{"type":"string","enum":["manual","grounding","sam"]}},"required":["key","label","anchor","source"],"additionalProperties":False}},
 "steps":{"type":"array","items":{"type":"object","properties":{"id":{"type":"string"},"title":{"type":"string"},"action":{"type":"string","enum":["place","remove","insert","tighten","loosen","connect","press","apply","inspect","other"]},"subject":{"type":"string"},"tool":{"type":"string"},"target_zone":{"type":"string"},"repeat":{"type":["object","null"],"properties":{"count":{"type":"integer"},"per":{"type":"string","enum":["zone","object"]},"pattern":{"type":"array","items":{"type":"string"}}},"required":["count","per","pattern"],"additionalProperties":False},"requires":{"type":"array","items":{"type":"object","properties":{"step":{"type":"string"},"scope":{"type":"string","enum":["all","same_instance"]}},"required":["step","scope"],"additionalProperties":False}},"min_duration_s":{"type":"number"},"optional":{"type":"boolean"},"criticality":{"type":"string","enum":["critical","major","minor"]},"evidence_hints":{"type":"object","properties":{"performed":{"type":"string"},"skipped":{"type":"string"}},"required":["performed","skipped"],"additionalProperties":False}},"required":["id","title","action","subject","tool","target_zone","repeat","requires","min_duration_s","optional","criticality","evidence_hints"],"additionalProperties":False}},
 "postconditions":{"type":"array","items":{"type":"object","properties":{"id":{"type":"string"},"description":{"type":"string"},"check":{"type":"string","enum":["count_in_zones","present_in_zone","absent_from_zone"]},"object":{"type":"string"},"zones":{"type":"string"},"equals":{"type":"integer"}},"required":["id","description","check","object","zones","equals"],"additionalProperties":False}},
 "vocabulary":{"type":"array","items":{"type":"string"}},"open_questions":{"type":"array","items":{"type":"string"}}},
 "required":["title","camera","objects","zones","steps","postconditions","vocabulary","open_questions"],"additionalProperties":False}
RECON_SCHEMA = {"type":"object","properties":{
 "objects":{"type":"array","items":{"type":"object","properties":{"object_key":{"type":"string"},"visible":{"type":"boolean"},"better_concepts":{"type":"array","items":{"type":"string"}},"notes":{"type":"string"}},"required":["object_key","visible","better_concepts","notes"],"additionalProperties":False}},
 "activities":{"type":"array","items":{"type":"object","properties":{"start_s":{"type":"number"},"end_s":{"type":"number"},"description":{"type":"string"},"object_keys":{"type":"array","items":{"type":"string"}},"likely_step_ids":{"type":"array","items":{"type":"string"}},"visibility":{"type":"string","enum":["clear","partial","occluded"]}},"required":["start_s","end_s","description","object_keys","likely_step_ids","visibility"],"additionalProperties":False}},
 "unlisted_objects":{"type":"array","items":{"type":"string"}},"camera_issues":{"type":"array","items":{"type":"string"}}},
 "required":["objects","activities","unlisted_objects","camera_issues"],"additionalProperties":False}
VERIFICATION_SCHEMA = {"type":"object","properties":{
 "steps":{"type":"array","items":{"type":"object","properties":{"step_id":{"type":"string"},"instances":{"type":"array","items":{"type":"object","properties":{"instance_key":{"type":"string"},"verdict":{"type":"string","enum":["performed","partially_performed","not_performed","cannot_determine"]},"start_frame":{"type":"integer"},"end_frame":{"type":"integer"},"decisive_frames":{"type":"array","items":{"type":"integer"}},"marks":{"type":"array","items":{"type":"string"}},"confidence":{"type":"number"},"rationale":{"type":"string"},"missing_evidence":{"type":"string"}},"required":["instance_key","verdict","start_frame","end_frame","decisive_frames","marks","confidence","rationale","missing_evidence"],"additionalProperties":False}},"measurement_conflicts":{"type":"array","items":{"type":"string"}}},"required":["step_id","instances","measurement_conflicts"],"additionalProperties":False}}},
 "required":["steps"],"additionalProperties":False}
