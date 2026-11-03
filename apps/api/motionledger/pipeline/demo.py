from __future__ import annotations
from pathlib import Path
import cv2, numpy as np, math
from ..config import settings
from ..schemas.procedure import ProcedureSpec
from ..schemas.report import StepObservation, RunMeta, VideoMeta
from ..engine.compare import compare
from ..review.ledger import append_entry, utc_now_iso
from ..db import Database

DEMO_VIDEO_ID = "vid_motionledger_demo"
DEMO_AUDIT_ID = "aud_motionledger_demo"

def make_demo_video(path: Path) -> dict:
    """Create an explicitly synthetic illustration used only for onboarding the UI."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fps, frames, w, h = 15, 720, 960, 600
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    if not writer.isOpened(): raise RuntimeError("OpenCV could not create the synthetic demo clip")
    cx, cy = 490, 305
    holes = [(cx, cy-130), (cx+130, cy), (cx, cy+130), (cx-130, cy)]
    try:
        for frame in range(frames):
            t = frame / fps
            img = np.zeros((h,w,3), dtype=np.uint8)
            img[:] = (24, 31, 43)
            # subtle grid
            for x in range(0,w,40): cv2.line(img,(x,0),(x,h),(31,40,53),1)
            for y in range(0,h,40): cv2.line(img,(0,y),(w,y),(31,40,53),1)
            cv2.putText(img,"MOTIONLEDGER  /  SYNTHETIC TRAINING CLIP",(34,42),cv2.FONT_HERSHEY_SIMPLEX,.7,(165,181,201),2,cv2.LINE_AA)
            cv2.putText(img,"ILLUSTRATIVE ONLY — NOT RECORDED FACTORY FOOTAGE",(34,570),cv2.FONT_HERSHEY_SIMPLEX,.52,(105,135,165),1,cv2.LINE_AA)
            # Fixture and gasket
            cv2.circle(img,(cx,cy),178,(93,108,127),-1,cv2.LINE_AA)
            cv2.circle(img,(cx,cy),154,(49,61,78),-1,cv2.LINE_AA)
            cv2.circle(img,(cx,cy),125,(20,28,39),5,cv2.LINE_AA)
            gasket_visible = frame >= 45
            if gasket_visible:
                cv2.circle(img,(cx,cy),110,(31,132,211),10,cv2.LINE_AA)
            cv2.putText(img,"FLANGE FIXTURE",(cx-95,cy+16),cv2.FONT_HERSHEY_SIMPLEX,.58,(135,151,171),1,cv2.LINE_AA)
            # Hole seats + bolts; third bolt remains visibly untightened.
            for idx,(x,y) in enumerate(holes, start=1):
                cv2.circle(img,(x,y),27,(16,22,32),-1,cv2.LINE_AA)
                inserted = frame >= (110 + idx*30)
                if inserted:
                    shade = (167,184,203) if idx != 3 else (107,126,149)
                    cv2.circle(img,(x,y),22,shade,-1,cv2.LINE_AA)
                    cv2.circle(img,(x,y),17,(54,67,83),2,cv2.LINE_AA)
                    cv2.line(img,(x-9,y),(x+9,y), (25,35,48),3,cv2.LINE_AA)
                    if idx == 3:
                        cv2.circle(img,(x,y),30,(30,82,235),2,cv2.LINE_AA)
                cv2.putText(img,f"H{idx}",(x-13,y+45),cv2.FONT_HERSHEY_SIMPLEX,.4,(174,190,207),1,cv2.LINE_AA)
            # Tool tray and wrench motion: holes 1 and 2 tightened; 3 absent; 4 masked.
            cv2.rectangle(img,(730,145),(910,430),(43,55,70),-1)
            cv2.rectangle(img,(730,145),(910,430),(82,99,119),2)
            cv2.putText(img,"TOOL TRAY",(765,178),cv2.FONT_HERSHEY_SIMPLEX,.55,(178,191,208),1,cv2.LINE_AA)
            wrench_active = 430 <= frame <= 610
            if wrench_active:
                idx = 1 if frame < 500 else 2
                x,y = holes[idx-1]
                angle = (frame % 18) * math.pi / 9
                ex,ey = int(x+115*math.cos(angle)), int(y+115*math.sin(angle))
                cv2.line(img,(x,y),(ex,ey),(105,196,231),13,cv2.LINE_AA)
                cv2.circle(img,(x,y),12,(105,196,231),-1,cv2.LINE_AA)
            else:
                cv2.line(img,(775,300),(865,250),(105,196,231),13,cv2.LINE_AA)
                cv2.circle(img,(775,300),16,(105,196,231),-1,cv2.LINE_AA)
            # Occlusion indicator on hole 4 interval.
            if 620 <= frame <= 690:
                cv2.ellipse(img,(cx-120,cy), (65,150), -15,0,360,(80,92,108),-1,cv2.LINE_AA)
                cv2.putText(img,"PARTIAL OCCLUSION",(cx-230,cy+190),cv2.FONT_HERSHEY_SIMPLEX,.5,(150,166,185),1)
            # Progress strip
            cv2.rectangle(img,(32,72),(w-32,80),(49,62,78),-1)
            cv2.rectangle(img,(32,72),(32+int((w-64)*frame/(frames-1)),80),(65,183,218),-1)
            writer.write(img)
    finally:
        writer.release()
    # Convert mp4v to H.264 for browser playback.
    h264 = path.with_name(path.stem + "_h264.mp4")
    import subprocess
    proc = subprocess.run(["ffmpeg","-y","-i",str(path),"-c:v","libx264","-pix_fmt","yuv420p","-movflags","+faststart",str(h264)],capture_output=True)
    if proc.returncode == 0:
        path.unlink(missing_ok=True); h264.replace(path)
    return {"duration_s": frames/fps, "fps": fps, "frames": frames, "width": w, "height": h}

def seed_demo(db: Database, sop: ProcedureSpec) -> dict:
    video_row = db.one("SELECT * FROM videos WHERE id=?", (DEMO_VIDEO_ID,))
    proxy = settings.data_dir / "proxies" / f"{DEMO_VIDEO_ID}.mp4"
    if not proxy.exists():
        meta = make_demo_video(proxy)
        db.insert_json("videos", {"id":DEMO_VIDEO_ID,"original_name":"motionledger-synthetic-demo.mp4",
            "original_path":str(proxy),"proxy_path":str(proxy),"sha256":"synthetic-illustration",
            **meta,"file_id":None,"status":"ready","created_at":utc_now_iso()})
    else:
        row = db.one("SELECT * FROM videos WHERE id=?", (DEMO_VIDEO_ID,))
        meta = {k:row[k] for k in ("duration_s","fps","frames","width","height")}
    audit = db.one("SELECT * FROM audits WHERE id=?", (DEMO_AUDIT_ID,))
    if not audit:
        db.insert_json("audits", {"id":DEMO_AUDIT_ID,"video_id":DEMO_VIDEO_ID,"procedure_id":sop.id,
            "procedure_version":sop.version,"created_at":utc_now_iso()})
    existing = db.one("SELECT * FROM runs WHERE audit_id=? ORDER BY version DESC LIMIT 1", (DEMO_AUDIT_ID,))
    if existing and existing["report_json"]:
        return {"audit_id":DEMO_AUDIT_ID,"run_id":existing["id"],"created":False}
    run_id = "run_motionledger_demo_v1"
    db.insert_json("runs", {"id":run_id,"audit_id":DEMO_AUDIT_ID,"version":1,"parent_id":None,"mode":"SIMULATION",
        "status":"running","stage":"simulating","created_at":utc_now_iso(),"completed_at":None,"cost_usd":0,
        "report_json":None,"error":None})
    observations = [
        StepObservation(id="demo-s1",step_id="S1",status="observed",start_frame=54,end_frame=93,confidence=.96,cited_frames=[54,93],rationale="Synthetic demo: gasket shown seated on flange.",source="demo",duration_s=2.6),
        *[StepObservation(id=f"demo-s2-{i}",step_id="S2",instance_key=f"hole_{i}",status="observed",start_frame=130+i*34,end_frame=160+i*34,confidence=.94,cited_frames=[130+i*34,160+i*34],rationale="Synthetic demo: bolt shown seated.",source="demo",duration_s=2.0) for i in range(1,5)],
        StepObservation(id="demo-s3-1",step_id="S3",instance_key="hole_1",status="observed",start_frame=435,end_frame=474,confidence=.91,cited_frames=[435,474],rationale="Synthetic demo: wrench strokes illustrated at hole 1.",source="demo",duration_s=2.6),
        StepObservation(id="demo-s3-2",step_id="S3",instance_key="hole_2",status="observed",start_frame=510,end_frame=550,confidence=.9,cited_frames=[510,550],rationale="Synthetic demo: wrench strokes illustrated at hole 2.",source="demo",duration_s=2.7),
        StepObservation(id="demo-s3-3",step_id="S3",instance_key="hole_3",status="not_performed",start_frame=-1,end_frame=-1,confidence=.87,cited_frames=[570,600,715],rationale="Illustrative simulated finding: no tightening event is assigned to hole 3.",source="demo",expected_window=(551,715)),
        StepObservation(id="demo-s3-4",step_id="S3",instance_key="hole_4",status="cannot_determine",start_frame=620,end_frame=690,confidence=.48,cited_frames=[620,660,690],rationale="Illustrative simulated finding: the decisive interval is obscured.",source="demo",missing_evidence="Synthetic occlusion overlay covers hole 4 from 41.3 to 46.0 seconds."),
        StepObservation(id="demo-s4",step_id="S4",status="observed",start_frame=680,end_frame=700,confidence=.78,cited_frames=[680,700],rationale="Synthetic demo: short final inspection pause.",source="demo",duration_s=1.3)
    ]
    video = VideoMeta(**meta)
    run = RunMeta(version=1,parent=None,models={"perception":"SIMULATED","reasoning":"SIMULATED"},cost_usd=0,mode="SIMULATION",created_at=utc_now_iso())
    report = compare(sop=sop,observations=observations,primitives=[],video=video,audit_id=DEMO_AUDIT_ID,
                     run=run,video_id=DEMO_VIDEO_ID,warnings=["SIMULATION ONLY: this is a synthetic illustration and its findings are not inferred from uploaded production footage."])
    append_entry(db,DEMO_AUDIT_ID,"run.created",{"run_id":run_id,"version":1,"mode":"SIMULATION"})
    head = db.one("SELECT hash FROM ledger_entries WHERE audit_id=? ORDER BY seq DESC LIMIT 1",(DEMO_AUDIT_ID,))["hash"]
    report.ledger_head = head
    db.update("runs","id",run_id,{"status":"completed","stage":"completed","completed_at":utc_now_iso(),"report_json":report.model_dump(mode="json")})
    append_entry(db,DEMO_AUDIT_ID,"run.completed",{"run_id":run_id,"finding_count":len(report.findings),"mode":"SIMULATION"})
    head = db.one("SELECT hash FROM ledger_entries WHERE audit_id=? ORDER BY seq DESC LIMIT 1",(DEMO_AUDIT_ID,))["hash"]
    report.ledger_head = head
    db.update("runs","id",run_id,{"report_json":report.model_dump(mode="json")})
    return {"audit_id":DEMO_AUDIT_ID,"run_id":run_id,"created":True}
