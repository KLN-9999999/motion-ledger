from __future__ import annotations
import asyncio, json, os, re, uuid, html
from contextlib import aclosing, asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse, HTMLResponse, Response
from pydantic import BaseModel, Field

from .config import settings
from .db import Database
from .schemas.procedure import ProcedureSpec, ProcedureCompileRequest
from .schemas.report import AuditReport, RunMeta, VideoMeta, StepObservation, Primitive
from .media.ingest import ingest_video, MediaError, read_frame
from .media.render import render_sam_overlay
from .review.ledger import append_entry, verify_chain, utc_now_iso
from .pipeline.runner import run_live_audit
from .pipeline.events import hub
from .pipeline.demo import seed_demo
from .engine.compare import compare

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Seed the canonical, editable flange procedure when the database is empty.
    if not db.one("SELECT id FROM procedures WHERE id=? ORDER BY version DESC LIMIT 1", ("flange-assembly",)):
        sample_path=Path(__file__).resolve().parents[3]/"samples"/"procedures"/"flange-assembly.json"
        if not sample_path.exists(): sample_path=Path.cwd().parents[1]/"samples"/"procedures"/"flange-assembly.json"
        if sample_path.exists():
            spec=ProcedureSpec.model_validate_json(sample_path.read_text(encoding="utf-8"))
            spec.validate_references()
            db.insert_json("procedures",{"id":spec.id,"version":spec.version,"title":spec.title,
                "body_json":spec.model_dump(mode="json"),"created_at":utc_now_iso()})
    yield

app = FastAPI(title="MotionLedger API", version="0.1.0", description="Evidence-first assembly process auditing", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
db = Database(settings.db_path)
background_tasks: set[asyncio.Task] = set()

class AuditCreate(BaseModel):
    video_id: str
    procedure_id: str = "flange-assembly"
    procedure_version: int | None = None

class OverrideCreate(BaseModel):
    kind: Literal["confirm_observation","dismiss_finding","confirm_finding","relabel","retime","add_observation","edit_zone","edit_procedure"]
    instance_id: str | None = None
    payload: dict = Field(default_factory=dict)

class RerunCreate(BaseModel):
    mode: Literal["recompare","reverify","resegment"] = "recompare"
    object_key: str | None = None
    concept: str | None = None


def _json(row: dict | None, key: str) -> Any:
    return json.loads(row[key]) if row and row.get(key) else None

def _procedure_from_db(procedure_id: str, version: int | None = None) -> tuple[dict, ProcedureSpec]:
    if version is None:
        row=db.one("SELECT * FROM procedures WHERE id=? ORDER BY version DESC LIMIT 1",(procedure_id,))
    else:
        row=db.one("SELECT * FROM procedures WHERE id=? AND version=?",(procedure_id,version))
    if not row: raise HTTPException(404,"Procedure not found")
    body=json.loads(row["body_json"])
    return row, ProcedureSpec.model_validate(body)

def _background(coro):
    task=asyncio.create_task(coro)
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)
    return task

def _next_version(audit_id: str) -> int:
    row=db.one("SELECT MAX(version) AS v FROM runs WHERE audit_id=?",(audit_id,))
    return int((row or {}).get("v") or 0)+1

def _new_run(audit_id: str, mode: str, parent_id: str | None = None) -> dict:
    run_id=f"run_{uuid.uuid4().hex[:12]}"
    version=_next_version(audit_id)
    db.insert_json("runs",{"id":run_id,"audit_id":audit_id,"version":version,"parent_id":parent_id,"mode":mode,
        "status":"queued","stage":"queued","created_at":utc_now_iso(),"completed_at":None,"cost_usd":0,
        "report_json":None,"error":None})
    return db.one("SELECT * FROM runs WHERE id=?",(run_id,))

def _start_live_run(audit_id: str, run: dict):
    return _background(run_live_audit(db,run["id"],audit_id))

def _apply_finding_review(report: AuditReport, overrides: list[dict]) -> AuditReport:
    for override in overrides:
        if override["kind"] not in {"confirm_finding","dismiss_finding"}: continue
        payload=json.loads(override["payload_json"])
        target_id=payload.get("finding_id")
        target_type=payload.get("finding_type")
        target_instances=set(payload.get("instances",[]))
        for finding in report.findings:
            match=(finding.id==target_id) if target_id else ((not target_type or finding.type==target_type) and (not target_instances or bool(target_instances.intersection(finding.instances))))
            if match: finding.status="confirmed" if override["kind"]=="confirm_finding" else "dismissed"
    report.status="CLEAN" if not any(f.status!="dismissed" for f in report.findings) else "REVIEW"
    return report

@app.get("/health")
async def health():
    return {"ok":True,"service":"motionledger-api","model_api_configured":bool(settings.model_api_key),
            "offline":settings.offline,"demo_mode":settings.demo_mode}

@app.get("/api/procedures")
async def list_procedures():
    rows=db.all("SELECT p.* FROM procedures p JOIN (SELECT id,MAX(version) version FROM procedures GROUP BY id) latest ON p.id=latest.id AND p.version=latest.version ORDER BY p.created_at DESC")
    return [{"id":r["id"],"version":r["version"],"title":r["title"],"created_at":r["created_at"]} for r in rows]

@app.post("/api/procedures", status_code=201)
async def create_procedure(spec: ProcedureSpec):
    spec.validate_references()
    existing=db.one("SELECT MAX(version) AS v FROM procedures WHERE id=?",(spec.id,))
    if existing and existing.get("v") is not None: spec.version=int(existing["v"])+1
    db.insert_json("procedures",{"id":spec.id,"version":spec.version,"title":spec.title,
        "body_json":spec.model_dump(mode="json"),"created_at":utc_now_iso()})
    return spec.model_dump(mode="json")

@app.post("/api/procedures/compile")
async def compile_procedure(payload: ProcedureCompileRequest):
    try:
        from .meta.client import compile_procedure as do_compile
        raw=await do_compile(payload.text,payload.title)
        spec=ProcedureSpec.model_validate(raw)
        spec.id=re.sub(r"[^a-z0-9-]+","-",(payload.title or spec.title).lower()).strip("-")[:48] or f"procedure-{uuid.uuid4().hex[:8]}"
        spec.version=1
        spec.validate_references()
        return {"procedure":spec.model_dump(mode="json"),"requires_review":True}
    except Exception as exc:
        raise HTTPException(503,f"Procedure compilation failed: {exc}") from exc

@app.get("/api/procedures/{procedure_id}")
async def get_procedure(procedure_id: str, version: int | None = None):
    row,spec=_procedure_from_db(procedure_id,version)
    return spec.model_dump(mode="json")

@app.put("/api/procedures/{procedure_id}")
async def update_procedure(procedure_id: str, spec: ProcedureSpec):
    if spec.id != procedure_id: raise HTTPException(400,"Procedure id cannot be changed")
    spec.validate_references()
    latest=db.one("SELECT MAX(version) AS v FROM procedures WHERE id=?",(procedure_id,))
    spec.version=int(latest["v"] or 0)+1
    db.insert_json("procedures",{"id":spec.id,"version":spec.version,"title":spec.title,
        "body_json":spec.model_dump(mode="json"),"created_at":utc_now_iso()})
    return spec.model_dump(mode="json")

@app.post("/api/videos", status_code=201)
async def upload_video(file: UploadFile = File(...)):
    name=Path(file.filename or "upload.mp4").name
    suffix=Path(name).suffix.lower()
    if suffix not in {".mp4",".mov",".m4v"}: raise HTTPException(415,"Upload an MP4 or MOV file (H.264/H.265/MPEG-4 video)")
    video_id=f"vid_{uuid.uuid4().hex[:12]}"
    target=settings.data_dir/"uploads"/f"{video_id}{suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    total=0
    try:
        with target.open("wb") as out:
            while chunk:=await file.read(1024*1024):
                total+=len(chunk)
                if total>settings.max_upload_bytes:
                    target.unlink(missing_ok=True); raise HTTPException(413,"Video exceeds the 1 GiB upload limit")
                out.write(chunk)
        if total==0: raise HTTPException(400,"Uploaded file is empty")
        processed=ingest_video(target,video_id)
    except MediaError as exc:
        target.unlink(missing_ok=True); raise HTTPException(422,str(exc)) from exc
    except HTTPException: raise
    except Exception as exc:
        target.unlink(missing_ok=True); raise HTTPException(500,f"Video ingest failed: {exc}") from exc
    db.insert_json("videos",{"id":video_id,"original_name":name,"original_path":str(target),"proxy_path":str(processed["proxy_path"]),
        "sha256":processed["sha256"],"duration_s":processed["duration_s"],"fps":processed["fps"],"frames":processed["frames"],
        "width":processed["width"],"height":processed["height"],"file_id":None,"status":"ready","created_at":utc_now_iso()})
    return {"id":video_id,"name":name,"duration_s":processed["duration_s"],"fps":processed["fps"],
            "frames":processed["frames"],"width":processed["width"],"height":processed["height"],"status":"ready"}

@app.get("/api/videos")
async def list_videos():
    rows=db.all("SELECT id,original_name,duration_s,fps,frames,width,height,status,created_at FROM videos ORDER BY created_at DESC")
    return rows

@app.get("/api/videos/{video_id}")
async def get_video(video_id: str):
    row=db.one("SELECT id,original_name,duration_s,fps,frames,width,height,status,created_at FROM videos WHERE id=?",(video_id,))
    if not row: raise HTTPException(404,"Video not found")
    return row

@app.get("/api/videos/{video_id}/proxy.mp4")
async def get_proxy(video_id: str):
    row=db.one("SELECT proxy_path FROM videos WHERE id=?",(video_id,))
    if not row: raise HTTPException(404,"Video not found")
    path=Path(row["proxy_path"])
    if not path.is_file(): raise HTTPException(404,"Normalized video is missing")
    return FileResponse(path,media_type="video/mp4",filename=f"{video_id}.mp4",content_disposition_type="inline")

@app.get("/api/videos/{video_id}/frames/{frame}.jpg")
async def get_frame(video_id: str, frame: int):
    row=db.one("SELECT proxy_path,frames FROM videos WHERE id=?",(video_id,))
    if not row: raise HTTPException(404,"Video not found")
    if frame<0 or frame>=row["frames"]: raise HTTPException(416,"Frame index is outside this video")
    try: data=read_frame(Path(row["proxy_path"]),frame)
    except MediaError as exc: raise HTTPException(404,str(exc)) from exc
    return Response(data,media_type="image/jpeg",headers={"Cache-Control":"public, max-age=3600"})

@app.post("/api/audits", status_code=202)
async def create_audit(payload: AuditCreate):
    video=db.one("SELECT * FROM videos WHERE id=?",(payload.video_id,))
    if not video: raise HTTPException(404,"Video not found")
    procrow,sop=_procedure_from_db(payload.procedure_id,payload.procedure_version)
    audit_id=f"aud_{uuid.uuid4().hex[:10]}"
    db.insert_json("audits",{"id":audit_id,"video_id":payload.video_id,"procedure_id":sop.id,
        "procedure_version":sop.version,"created_at":utc_now_iso()})
    append_entry(db,audit_id,"audit.created",{"audit_id":audit_id,"video_id":payload.video_id,"procedure_id":sop.id,"procedure_version":sop.version})
    run=_new_run(audit_id,"LIVE_META_MODEL")
    _start_live_run(audit_id,run)
    return {"audit_id":audit_id,"run_id":run["id"],"status":"queued","mode":"LIVE_META_MODEL"}

@app.post("/api/demo/seed", status_code=201)
async def create_demo():
    _,sop=_procedure_from_db("flange-assembly")
    result=seed_demo(db,sop)
    return result

@app.get("/api/audits")
async def list_audits():
    rows=db.all("""SELECT a.id,a.video_id,a.procedure_id,a.procedure_version,a.created_at,
      v.original_name,v.duration_s,r.id AS run_id,r.version AS run_version,r.status AS run_status,r.stage,r.cost_usd
      FROM audits a JOIN videos v ON v.id=a.video_id LEFT JOIN runs r ON r.id=(SELECT id FROM runs WHERE audit_id=a.id ORDER BY version DESC LIMIT 1)
      ORDER BY a.created_at DESC""")
    output=[]
    for row in rows:
        report=db.one("SELECT report_json FROM runs WHERE id=?",(row["run_id"],)) if row.get("run_id") else None
        parsed=json.loads(report["report_json"]) if report and report["report_json"] else {}
        row["finding_count"]=len(parsed.get("findings",[])); row["report_status"]=parsed.get("status","PENDING")
        output.append(row)
    return output

@app.get("/api/audits/{audit_id}")
async def get_audit(audit_id: str):
    audit=db.one("SELECT * FROM audits WHERE id=?",(audit_id,))
    if not audit: raise HTTPException(404,"Audit not found")
    video=db.one("SELECT id,original_name,duration_s,fps,frames,width,height,status FROM videos WHERE id=?",(audit["video_id"],))
    runs=db.all("SELECT id,version,parent_id,mode,status,stage,created_at,completed_at,cost_usd,error FROM runs WHERE audit_id=? ORDER BY version",(audit_id,))
    latest=runs[-1] if runs else None
    return {"audit":audit,"video":video,"runs":runs,"latest_run":latest}

@app.get("/api/runs/{run_id}")
async def get_run(run_id: str):
    run=db.one("SELECT * FROM runs WHERE id=?",(run_id,))
    if not run: raise HTTPException(404,"Run not found")
    return {k:run[k] for k in run if k!="report_json"}

@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str):
    if not db.one("SELECT id FROM runs WHERE id=?",(run_id,)): raise HTTPException(404,"Run not found")
    async def stream():
        async for item in hub.subscribe(run_id):
            yield f"event: {item['event']}\ndata: {json.dumps(item['data'])}\n\n"
    return StreamingResponse(stream(),media_type="text/event-stream",headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

@app.get("/api/runs/{run_id}/report")
async def get_report(run_id: str):
    row=db.one("SELECT report_json FROM runs WHERE id=?",(run_id,))
    if not row: raise HTTPException(404,"Run not found")
    if not row["report_json"]: raise HTTPException(409,"Report is not ready")
    return json.loads(row["report_json"])

@app.get("/api/runs/{run_id}/report.html", response_class=HTMLResponse)
async def report_html(run_id: str):
    row=db.one("SELECT r.*,a.id AS aid,v.original_name FROM runs r JOIN audits a ON a.id=r.audit_id JOIN videos v ON v.id=a.video_id WHERE r.id=?",(run_id,))
    if not row: raise HTTPException(404,"Run not found")
    if not row["report_json"]: raise HTTPException(409,"Report is not ready")
    report=json.loads(row["report_json"])
    cards="".join(f"<article><small>{html.escape(f['severity'].upper())} · {html.escape(f['type'])}</small><h2>{html.escape(f['explanation'])}</h2><p>{f['time_range_s'][0]:.2f}s – {f['time_range_s'][1]:.2f}s · confidence {f['confidence']:.2f}</p><ul>"+"".join(f"<li>{html.escape(m)}</li>" for m in f['evidence']['measurements'])+"</ul></article>" for f in report["findings"])
    page=f"""<!doctype html><html><head><meta charset='utf-8'><title>MotionLedger report</title><style>body{{font:15px Arial;max-width:960px;margin:40px auto;color:#172033}}article{{border:1px solid #ccd4df;border-radius:12px;padding:20px;margin:16px 0}}small{{font-weight:bold;color:#52647b}}@media print{{button{{display:none}}}}</style></head><body><button onclick='window.print()'>Print / Save PDF</button><h1>MotionLedger audit report</h1><p>{html.escape(report['procedure']['title'])} · {html.escape(row['original_name'])}</p><p>Status: <b>{report['status']}</b> · Run v{report['run']['version']} · Cost ${report['run']['cost_usd']:.4f}</p><p>Ledger head: <code>{html.escape(report.get('ledger_head',''))}</code></p>{cards}<hr><p>Possible discrepancies require qualified human review. Inability to see an action is reported as UNVERIFIABLE, not as a defect.</p></body></html>"""
    return HTMLResponse(page)

@app.get("/api/runs/{run_id}/overlays/{frame}.png")
async def sam_overlay_frame(run_id: str, frame: int):
    run = db.one("SELECT * FROM runs WHERE id=?", (run_id,))
    if not run: raise HTTPException(404, "Run not found")
    audit = db.one("SELECT * FROM audits WHERE id=?", (run["audit_id"],))
    video = db.one("SELECT * FROM videos WHERE id=?", (audit["video_id"],))
    if frame < 0 or frame >= int(video["frames"]): raise HTTPException(416, "Frame index is outside this video")
    _, sop = _procedure_from_db(audit["procedure_id"], audit["procedure_version"])
    sources = []
    for obj in sop.objects:
        safe_key = re.sub(r"[^a-zA-Z0-9_-]", "_", obj.key)[:40]
        path = settings.data_dir / "tracks" / run_id / f"sam-{safe_key}.npz"
        if path.is_file(): sources.append({"key": obj.key, "role": obj.role, "path": str(path)})
    try:
        png = render_sam_overlay(Path(video["proxy_path"]), sources, frame)
    except Exception as exc:
        raise HTTPException(422, f"Could not render SAM overlay: {exc}") from exc
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})

@app.get("/api/runs/{run_id}/tracks")
async def tracks(run_id: str):
    run=db.one("SELECT id FROM runs WHERE id=?",(run_id,))
    if not run: raise HTTPException(404,"Run not found")
    calls=db.all("SELECT model,endpoint,usage_json,latency_ms,cost_usd,created_at FROM model_calls WHERE run_id=? ORDER BY created_at",(run_id,))
    track_path=settings.data_dir/"tracks"/run_id/"summary.json"
    sam_jobs=json.loads(track_path.read_text(encoding="utf-8")) if track_path.is_file() else {}
    return {"model_calls":[{**c,"usage":json.loads(c.pop("usage_json")) if c.get("usage_json") else {}} for c in calls],
            "sam_jobs":sam_jobs}

@app.get("/api/runs/{run_id}/sam/{object_key}.jsonl")
async def sam_log(run_id: str, object_key: str):
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,40}",object_key): raise HTTPException(400,"Invalid object key")
    if not db.one("SELECT id FROM runs WHERE id=?",(run_id,)): raise HTTPException(404,"Run not found")
    path=settings.data_dir/"logs"/run_id/f"sam-{object_key}.jsonl"
    if not path.is_file(): raise HTTPException(404,"SAM log not found")
    return FileResponse(path,media_type="application/x-ndjson")

@app.post("/api/audits/{audit_id}/overrides", status_code=201)
async def create_override(audit_id: str, payload: OverrideCreate):
    if not db.one("SELECT id FROM audits WHERE id=?",(audit_id,)): raise HTTPException(404,"Audit not found")
    latest=db.one("SELECT id FROM runs WHERE audit_id=? ORDER BY version DESC LIMIT 1",(audit_id,))
    if not latest: raise HTTPException(409,"Audit has no runs")
    oid=f"ovr_{uuid.uuid4().hex[:10]}"
    db.insert_json("overrides",{"id":oid,"audit_id":audit_id,"run_id":latest["id"],"kind":payload.kind,
        "instance_id":payload.instance_id,"payload_json":payload.payload,"created_at":utc_now_iso()})
    append_entry(db,audit_id,"review.override",{"override_id":oid,"kind":payload.kind,"instance_id":payload.instance_id,"payload":payload.payload})
    return {"id":oid,"saved":True,"message":"Correction recorded in the audit ledger. Run Recompare to apply it."}

@app.post("/api/audits/{audit_id}/runs", status_code=202)
async def rerun_audit(audit_id: str, payload: RerunCreate):
    audit=db.one("SELECT * FROM audits WHERE id=?",(audit_id,))
    if not audit: raise HTTPException(404,"Audit not found")
    previous=db.one("SELECT * FROM runs WHERE audit_id=? ORDER BY version DESC LIMIT 1",(audit_id,))
    if not previous or not previous["report_json"]: raise HTTPException(409,"Previous run has no report to compare")
    if payload.mode=="resegment":
        raise HTTPException(501,"Use POST /api/audits/{id}/segment to issue a SAM point prompt. A full resegment run is not yet wired into the run graph.")
    run=_new_run(audit_id,payload.mode,previous["id"])
    if payload.mode=="recompare":
        _background(_run_recompare(audit_id,run["id"],previous["id"]))
    elif payload.mode=="reverify":
        _start_live_run(audit_id,run)
    return {"run_id":run["id"],"version":run["version"],"parent_id":previous["id"],"status":"queued","mode":payload.mode}

async def _run_recompare(audit_id: str, run_id: str, parent_id: str):
    try:
        run=db.one("SELECT * FROM runs WHERE id=?",(run_id,)); parent=db.one("SELECT * FROM runs WHERE id=?",(parent_id,))
        audit=db.one("SELECT * FROM audits WHERE id=?",(audit_id,)); video=db.one("SELECT * FROM videos WHERE id=?",(audit["video_id"],))
        _,sop=_procedure_from_db(audit["procedure_id"],audit["procedure_version"])
        prev=AuditReport.model_validate_json(parent["report_json"])
        override_rows=db.all("SELECT * FROM overrides WHERE audit_id=? ORDER BY created_at",(audit_id,))
        overrides=[{**row,"payload":json.loads(row["payload_json"] or "{}")} for row in override_rows]
        hub.publish(run_id,"stage.started",stage="comparison",message="Re-running deterministic comparison from cached evidence")
        db.update("runs","id",run_id,{"status":"running","stage":"comparison"})
        run_meta=RunMeta(version=run["version"],parent=parent_id,models=prev.run.models,cost_usd=prev.run.cost_usd,
                         mode=prev.run.mode,created_at=run["created_at"])
        report=compare(sop=sop,observations=prev.step_observations,primitives=prev.primitives,video=prev.video,
                       audit_id=audit_id,run=run_meta,video_id=video["id"],warnings=prev.warnings,overrides=overrides)
        report=_apply_finding_review(report,override_rows)
        append_entry(db,audit_id,"run.created",{"run_id":run_id,"version":run["version"],"parent":parent_id,"mode":"recompare"})
        append_entry(db,audit_id,"run.recompared",{"run_id":run_id,"finding_count":len(report.findings)})
        head=db.one("SELECT hash FROM ledger_entries WHERE audit_id=? ORDER BY seq DESC LIMIT 1",(audit_id,))["hash"]
        report.ledger_head=head
        db.update("runs","id",run_id,{"status":"completed","stage":"completed","completed_at":utc_now_iso(),
            "cost_usd":0,"report_json":report.model_dump(mode="json")})
        hub.publish(run_id,"run.completed",status=report.status,findings=len(report.findings),cost_usd=0)
    except Exception as exc:
        db.update("runs","id",run_id,{"status":"failed","stage":"failed","completed_at":utc_now_iso(),"error":str(exc)})
        hub.publish(run_id,"run.failed",error=str(exc))

@app.get("/api/runs/{run_a}/diff/{run_b}")
async def diff_runs(run_a: str, run_b: str):
    a=db.one("SELECT report_json FROM runs WHERE id=?",(run_a,)); b=db.one("SELECT report_json FROM runs WHERE id=?",(run_b,))
    if not a or not b: raise HTTPException(404,"Run not found")
    if not a["report_json"] or not b["report_json"]: raise HTTPException(409,"Both reports must be complete")
    ra=json.loads(a["report_json"]); rb=json.loads(b["report_json"])
    def key(f): return f"{f['type']}|{'|'.join(sorted(f.get('instances',[])))}"
    amap={key(f):f for f in ra.get("findings",[]) if f.get("status")!="dismissed"}
    bmap={key(f):f for f in rb.get("findings",[]) if f.get("status")!="dismissed"}
    return {"from":run_a,"to":run_b,"added":[bmap[k] for k in bmap.keys()-amap.keys()],
            "resolved":[amap[k] for k in amap.keys()-bmap.keys()],
            "unchanged":[bmap[k] for k in bmap.keys()&amap.keys()]}

@app.get("/api/audits/{audit_id}/ledger")
async def audit_ledger(audit_id: str):
    if not db.one("SELECT id FROM audits WHERE id=?",(audit_id,)): raise HTTPException(404,"Audit not found")
    return verify_chain(db,audit_id)

@app.get("/api/audits/{audit_id}/export.json")
async def export_audit(audit_id: str):
    run=db.one("SELECT id FROM runs WHERE audit_id=? AND report_json IS NOT NULL ORDER BY version DESC LIMIT 1",(audit_id,))
    if not run: raise HTTPException(409,"No completed report to export")
    report=db.one("SELECT report_json FROM runs WHERE id=?",(run["id"],))
    return json.loads(report["report_json"])

@app.get("/api/audits/{audit_id}/export.csv")
async def export_audit_csv(audit_id: str):
    run=db.one("SELECT id FROM runs WHERE audit_id=? AND report_json IS NOT NULL ORDER BY version DESC LIMIT 1",(audit_id,))
    if not run: raise HTTPException(409,"No completed report to export")
    report=json.loads(db.one("SELECT report_json FROM runs WHERE id=?",(run["id"],))["report_json"])
    import csv, io
    out=io.StringIO(); writer=csv.writer(out)
    writer.writerow(["finding_id","type","severity","instances","status","confidence","start_s","end_s","explanation"])
    for f in report["findings"]:
        writer.writerow([f["id"],f["type"],f["severity"],";".join(f["instances"]),f["status"],f["confidence"],f["time_range_s"][0],f["time_range_s"][1],f["explanation"]])
    return Response(out.getvalue(),media_type="text/csv",headers={"Content-Disposition":f"attachment; filename=motionledger-{audit_id}.csv"})

@app.post("/api/audits/{audit_id}/segment")
async def click_to_segment(audit_id: str, payload: dict = Body(...)):
    audit=db.one("SELECT a.*,v.* FROM audits a JOIN videos v ON v.id=a.video_id WHERE a.id=?",(audit_id,))
    if not audit: raise HTTPException(404,"Audit not found")
    if not settings.model_api_key: raise HTTPException(503,"Set MODEL_API_KEY to use SAM click-to-segment")
    client=None
    try:
        from meta_sam_parser import FramePrompt, PromptObject, PromptPoint, build_object_prompt
        from .meta.client import get_client, close_text_lanes
        frame=int(payload["frame"]); object_id=int(payload.get("object_id",101))
        if frame < 0 or frame >= int(audit["frames"]):
            raise HTTPException(422,"Prompt frame is outside the normalized video")
        positive=payload.get("positive",[]); negative=payload.get("negative",[])
        if not isinstance(positive,list) or not positive:
            raise HTTPException(422,"At least one positive click point is required")
        if not isinstance(negative,list):
            raise HTTPException(422,"Negative points must be a list")
        def checked_point(point):
            if not isinstance(point,(list,tuple)) or len(point)!=2:
                raise HTTPException(422,"Each click point must contain x and y pixels")
            x,y=int(point[0]),int(point[1])
            if not (0 <= x < int(audit["width"]) and 0 <= y < int(audit["height"])):
                raise HTTPException(422,"Click point is outside the normalized video frame")
            return x,y
        points=[PromptPoint(*checked_point(p)) for p in positive]
        points += [PromptPoint(*checked_point(p),label="negative") for p in negative]
        prompt=build_object_prompt(size=(audit["width"],audit["height"]),objects=[PromptObject(id=object_id,prompts=[FramePrompt(frame=frame,points=points)])])
        client=get_client()
        file_id=audit.get("file_id")
        if not file_id:
            uploaded=await client.files.create(file=("proxy.mp4",Path(audit["proxy_path"]).read_bytes(),"video/mp4"),purpose="user_data",
                expires_after={"anchor":"created_at","seconds":settings.files_ttl_seconds})
            file_id=uploaded.id
            db.update("videos","id",audit["video_id"],{"file_id":file_id})
        stream=await client.responses.create(model=settings.sam_model,stream=True,input=[{"type":"message","role":"user","content":[{"type":"input_text","text":prompt},{"type":"input_video","file_id":file_id}]}],metadata={"propagation_direction":"both","include_confidence":"true"})
        log=settings.data_dir/"logs"/f"segment-{uuid.uuid4().hex[:8]}.jsonl"; log.parent.mkdir(parents=True,exist_ok=True)
        n=0
        with log.open("w",encoding="utf-8") as fh:
            async with stream, aclosing(close_text_lanes(stream)) as adapted:
                async for data in adapted:
                    fh.write(json.dumps(data,default=str)+"\n"); n+=1
        append_entry(db,audit_id,"review.segment_requested",{"frame":frame,"object_id":object_id,"positive":payload.get("positive",[]),"negative":payload.get("negative",[])})
        return {"accepted":True,"events":n,"log_path":str(log),"note":"Raw SAM events recorded; decoded masks still need to be linked to the track cache."}
    except HTTPException:
        raise
    except ImportError as exc: raise HTTPException(503,"Install meta-sam-parser>=0.1.0 to use click-to-segment") from exc
    except Exception as exc: raise HTTPException(502,f"SAM re-segmentation failed: {exc}") from exc
    finally:
        if client is not None: await client.close()
