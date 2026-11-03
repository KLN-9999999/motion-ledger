"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import { Activity, ArrowUpRight, CircleCheck, ClipboardList, FileVideo, Plus, ShieldCheck, Workflow } from "lucide-react";
import { Shell } from "./shell";
import { api } from "@/lib/api";
import type { Audit } from "@/lib/types";

export default function Dashboard() {
  const [audits,setAudits]=useState<Audit[]>([]); const [busy,setBusy]=useState(false); const [error,setError]=useState("");
  async function refresh(){try{setAudits(await api<Audit[]>("/api/audits"));setError("");}catch(e){setError((e as Error).message)}}
  useEffect(()=>{refresh();},[]);
  async function seed(){setBusy(true);setError("");try{const r=await api<{audit_id:string}>('/api/demo/seed',{method:'POST'});window.location.href=`/audits/${r.audit_id}`;}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
  const openFindings=audits.reduce((a,x)=>a+(x.finding_count||0),0);
  const completed=audits.filter(a=>a.run_status==='completed').length;
  const needsReview=audits.filter(a=>a.report_status==='REVIEW').length;
  return <Shell><main className="main">
    <div className="page-head"><div><div className="eyebrow">Manufacturing · audit workspace</div><h1>See the work behind the work.</h1><p className="subhead">Compare a real assembly run with its procedure. Every possible discrepancy is tied to a timestamp, source frame, and the evidence used to reach it.</p></div><div style={{display:'flex',gap:9,flexWrap:'wrap'}}><button className="btn" onClick={seed} disabled={busy}><Activity size={14}/>{busy?'Preparing demo…':'Explore sample audit'}</button><Link className="btn primary" href="/audits/new"><Plus size={15}/>New audit</Link></div></div>
    {error&&<div className="toast error">{error}</div>}
    <div className="stats">
      <div className="stat-card"><div className="stat-label">Total audits</div><div className="stat-value">{audits.length}</div><div className="stat-note">Across saved procedures</div></div>
      <div className="stat-card"><div className="stat-label">Completed runs</div><div className="stat-value">{completed}</div><div className="stat-note">Reports ready for review</div></div>
      <div className="stat-card"><div className="stat-label">Need review</div><div className="stat-value">{needsReview}</div><div className="stat-note">Possible discrepancies or checks</div></div>
      <div className="stat-card"><div className="stat-label">Findings generated</div><div className="stat-value">{openFindings}</div><div className="stat-note">Machine suggestions, not verdicts</div></div>
    </div>
    <div className="section-row"><h2>Recent audits</h2><button className="btn small" onClick={refresh}>Refresh list</button></div>
    {audits.length===0?<div className="empty"><div className="glyph"><Workflow size={28}/></div><h3>Your audit workspace is ready</h3><p>Start with a synthetic walkthrough or upload a top-down assembly video.</p><div style={{display:'flex',justifyContent:'center',gap:9,flexWrap:'wrap'}}><button className="btn" onClick={seed} disabled={busy}>Explore sample audit</button><Link className="btn primary" href="/audits/new"><FileVideo size={14}/>Upload video</Link></div></div>:
      <div className="audit-grid">{audits.map(a=><Link className="audit-card" key={a.id} href={`/audits/${a.id}`}>
        <div className="audit-top"><div><div className="audit-title">{a.procedure_id==='flange-assembly'?'Pipe flange gasket assembly':a.procedure_id}</div><div className="audit-meta">{a.original_name} · {Math.round(a.duration_s||0)} sec</div></div><span className={`pill ${a.report_status==='CLEAN'?'clean':a.report_status==='REVIEW'?'review':'pending'}`}>{a.report_status||a.run_status||'PENDING'}</span></div>
        <div className="audit-bottom"><span>Run v{a.run_version||'—'} · {a.stage||'queued'}</span><span>{a.finding_count||0} findings <ArrowUpRight size={12} style={{verticalAlign:'middle'}}/></span></div>
      </Link>)}</div>}
    <div className="two-col">
      <div className="panel"><div className="eyebrow">The system's rule</div><h2 style={{marginTop:8}}>Models perceive. Code decides.</h2><p className="muted" style={{fontSize:12,lineHeight:1.7,marginBottom:0}}>SAM 3.1 tracks objects. Muse Spark interprets the marked evidence. A deterministic comparison engine reconciles the observations with the procedure graph. A reviewer owns the final decision.</p></div>
      <div className="panel"><div className="eyebrow">Built for trustworthy review</div><h2 style={{marginTop:8}}>Not worker scoring.</h2><p className="muted" style={{fontSize:12,lineHeight:1.7,marginBottom:0}}>MotionLedger audits process conformance, not individual productivity. Occlusion becomes “unverifiable”, and only a person can confirm nonconformance.</p></div>
    </div>
  </main></Shell>;
}
