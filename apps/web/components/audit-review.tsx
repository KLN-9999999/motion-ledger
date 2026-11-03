"use client";
import { useCallback, useEffect, useMemo, useRef, useState, type MouseEvent } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import { ArrowLeft, Check, ChevronRight, CircleAlert, Download, FileDown, Fingerprint, Layers, RefreshCw, ScanLine, ShieldCheck, X } from "lucide-react";
import { Shell } from "./shell";
import { api, timecode } from "@/lib/api";
import type { Finding, Primitive, Report } from "@/lib/types";

type AuditPayload={audit:{id:string;video_id:string;procedure_id:string;procedure_version:number};video:{id:string;original_name:string;duration_s:number;fps:number;frames:number;width:number;height:number};runs:any[];latest_run:any};

export default function AuditReview(){
 const params=useParams<{id:string}>();const auditId=params.id;const [audit,setAudit]=useState<AuditPayload|null>(null);const [report,setReport]=useState<Report|null>(null);const [run,setRun]=useState<any>(null);const [stage,setStage]=useState('Loading audit');const [error,setError]=useState('');const [busy,setBusy]=useState(false);const [selected,setSelected]=useState<string|null>(null);const [ledger,setLedger]=useState<any>(null);const [diff,setDiff]=useState<any>(null);const [segmentMode,setSegmentMode]=useState(false);const [toast,setToast]=useState('');const [trackSummary,setTrackSummary]=useState<any>(null);const [overlayEnabled,setOverlayEnabled]=useState(true);const [overlayFrame,setOverlayFrame]=useState(0);const [overlayUrl,setOverlayUrl]=useState('');const videoRef=useRef<HTMLVideoElement>(null);
 const refresh=useCallback(async()=>{
   const a=await api<AuditPayload>(`/api/audits/${auditId}`);setAudit(a);setRun(a.latest_run);
   if(a.latest_run?.id){try{const r=await api<Report>(`/api/runs/${a.latest_run.id}/report`);setReport(r);setError('');}catch(e){if(a.latest_run.status==='failed')setError(a.latest_run.error||'Analysis failed');}}
 },[auditId]);
 useEffect(()=>{refresh().catch(e=>setError((e as Error).message));},[refresh]);
 useEffect(()=>{if(!run?.id)return;api<any>(`/api/runs/${run.id}/tracks`).then(setTrackSummary).catch(()=>setTrackSummary(null));},[run?.id,run?.status]);
 const hasMasks=Object.keys(trackSummary?.sam_jobs||{}).length>0;
 const updateOverlayFrame=useCallback(()=>{const v=videoRef.current;if(!v||!report)return;setOverlayFrame(Math.max(0,Math.min(report.video.frames-1,Math.floor(v.currentTime*report.video.fps))));},[report]);
 useEffect(()=>{if(!overlayEnabled||!hasMasks||!run?.id||!report||report.run.mode==='SIMULATION'){setOverlayUrl('');return;}setOverlayUrl(`/api/runs/${run.id}/overlays/${overlayFrame}.png`);},[overlayEnabled,hasMasks,run?.id,report,overlayFrame]);
 useEffect(()=>{
   if(!run?.id||run.status==='completed'||run.status==='failed')return;
   const timer=setInterval(()=>refresh().catch(()=>{}),1800);return()=>clearInterval(timer);
 },[run?.id,run?.status,refresh]);
 useEffect(()=>{
   if(!run?.id||run.status==='completed'||run.status==='failed')return;
   const es=new EventSource(`/api/runs/${run.id}/events`);
   es.addEventListener('stage.started',(e:any)=>{const d=JSON.parse(e.data);setStage(d.message||d.stage||'Analyzing');});
   es.addEventListener('stage.progress',(e:any)=>{const d=JSON.parse(e.data);setStage(`${d.stage||'Processing'}${d.object?` · ${d.object}`:''}`);});
   es.addEventListener('run.completed',()=>{setStage('Analysis complete');refresh().catch(()=>{});es.close();});
   es.addEventListener('run.failed',(e:any)=>{const d=JSON.parse(e.data);setError(d.error||'Analysis failed');es.close();});
   return()=>es.close();
 },[run?.id,run?.status,refresh]);
 const findings=report?.findings||[];
 const selectedFinding=findings.find(f=>f.id===selected)||findings.find(f=>f.status==='open')||findings[0];
 const observations=report?.step_observations||[];
 const stepRows=useMemo(()=>{
   const map=new Map<string,{rows:any[];title:string}>();
   for(const i of report?.instances||[]){const key=i.step_id;const v=map.get(key)||{rows:[],title:i.title};v.rows.push(i);map.set(key,v)}
   return [...map.entries()];
 },[report]);
 async function rerun(mode:'recompare'|'reverify'='recompare'){
   setBusy(true);setToast('');setError('');try{const r=await api<{run_id:string;version:number}>(`/api/audits/${auditId}/runs`,{method:'POST',body:JSON.stringify({mode})});setStage(`Queued run v${r.version}`);setReport(null);await refresh();}catch(e){setError((e as Error).message)}finally{setBusy(false)}
 }
 async function reviewFinding(f:Finding,kind:'confirm_finding'|'dismiss_finding'){
   setBusy(true);try{
     await api(`/api/audits/${auditId}/overrides`,{method:'POST',body:JSON.stringify({kind,payload:{finding_id:f.id,finding_type:f.type,instances:f.instances,reason:kind==='dismiss_finding'?'Reviewer dismissed':'Reviewer confirmed'}})});
     setToast(kind==='confirm_finding'?'Finding confirmed and recorded in the ledger.':'Finding dismissed and recorded in the ledger.');
     await rerun('recompare');
   }catch(e){setError((e as Error).message)}finally{setBusy(false)}
 }
 async function addObservation(instanceId:string){
   const startText=window.prompt('Start time in seconds (for example 21.4):');if(startText===null)return;
   const endText=window.prompt('End time in seconds:');if(endText===null)return;
   const start=Math.max(0,Math.round(Number(startText)*(report?.video.fps||15)));const end=Math.max(start,Math.round(Number(endText)*(report?.video.fps||15)));
   if(!Number.isFinite(start)||!Number.isFinite(end)){setError('Enter valid numeric times.');return;}
   try{await api(`/api/audits/${auditId}/overrides`,{method:'POST',body:JSON.stringify({kind:'add_observation',instance_id:instanceId,payload:{start_frame:start,end_frame:end,note:'Added by reviewer'}})});await rerun('recompare');}catch(e){setError((e as Error).message)}
 }
 async function showLedger(){try{setLedger(await api(`/api/audits/${auditId}/ledger`));}catch(e){setError((e as Error).message)}}
 async function showDiff(){if(!audit?.runs||audit.runs.length<2){setToast('Create a second run to compare versions.');return;}const sorted=[...audit.runs].sort((a,b)=>a.version-b.version);try{setDiff(await api(`/api/runs/${sorted[sorted.length-2].id}/diff/${sorted[sorted.length-1].id}`));}catch(e){setError((e as Error).message)}}
 function seek(seconds:number){if(videoRef.current){videoRef.current.currentTime=Math.max(0,seconds);videoRef.current.pause();}}
 async function segmentClick(e:MouseEvent<HTMLVideoElement>){
   if(!segmentMode||!report||!audit)return;
   const rect=e.currentTarget.getBoundingClientRect();const scale=Math.min(rect.width/report.video.width,rect.height/report.video.height);const ox=(rect.width-report.video.width*scale)/2;const oy=(rect.height-report.video.height*scale)/2;
   const x=Math.max(0,Math.min(report.video.width-1,Math.floor((e.clientX-rect.left-ox)/scale)));const y=Math.max(0,Math.min(report.video.height-1,Math.floor((e.clientY-rect.top-oy)/scale)));
   const frame=Math.round((videoRef.current?.currentTime||0)*report.video.fps);
   setBusy(true);try{const r=await api<{accepted:boolean;note:string}>(`/api/audits/${auditId}/segment`,{method:'POST',body:JSON.stringify({frame,positive:[[x,y]],negative:[],object_id:101})});setToast(r.note);setSegmentMode(false);}catch(err){setError((err as Error).message)}finally{setBusy(false)}
 }
 if(!audit)return <Shell><main className="main"><div className="panel">{error||'Loading audit…'}</div></main></Shell>;
 const isSimulation=report?.run.mode==='SIMULATION';
 return <Shell><main className="main">
  <div style={{marginBottom:15}}><Link href="/" className="btn small"><ArrowLeft size={13}/>All audits</Link></div>
  <div className="review-head"><div><div className="eyebrow">Audit review · {audit.audit.id}</div><h1 style={{fontSize:27,marginTop:4}}>{report?.procedure.title||'Pipe flange gasket assembly'}</h1><div className="review-meta"><span>{audit.video.original_name}</span><span>{Math.round(audit.video.duration_s)} sec · {audit.video.fps} fps</span><span>Run v{run?.version||1}</span><span>${(report?.run.cost_usd||run?.cost_usd||0).toFixed(4)} model cost</span></div></div>
   <div className="review-actions"><button className="btn" onClick={()=>rerun('recompare')} disabled={busy||!report}><RefreshCw size={13}/>Recompare</button><button className="btn" onClick={()=>rerun('reverify')} disabled={busy}><ScanLine size={13}/>Re-verify</button><a className="btn" href={`/api/audits/${auditId}/export.csv`}><Download size={13}/>CSV</a><a className="btn" href={`/api/runs/${run?.id}/report.html`} target="_blank"><FileDown size={13}/>Report</a></div>
  </div>
  {error&&<div className="toast error">{error}</div>}{toast&&<div className="toast">{toast}</div>}
  {(!report)&&<div className="panel" style={{padding:30,marginBottom:16}}><div style={{display:'flex',gap:13,alignItems:'center'}}><span className="spinner" style={{width:22,height:22}}/><div><h2 style={{margin:'0 0 4px'}}>Analysis in progress</h2><div className="muted" style={{fontSize:12}}>{stage||run?.stage||'Waiting for pipeline'}</div></div></div><div className="timeline-track" style={{marginTop:18,height:6}}><div className="timeline-bar" style={{left:0,width:'40%',height:6,top:0}}/></div><div className="helper" style={{marginTop:10}}>This page refreshes as stages complete. Live analysis requires a valid Meta Model API key in the backend environment.</div></div>}
  {report&&<>
   {isSimulation&&<div className="notice" style={{marginBottom:14}}><strong>Simulation only.</strong> This is a synthetic training clip and an illustrative report to demonstrate the review workflow. Findings below were not inferred from real production footage.</div>}
   <div className="review-grid">
    <div>
     <div className="panel"><div className="section-row"><h2>Evidence player</h2><div style={{display:'flex',gap:7,flexWrap:'wrap'}}>{hasMasks&&<button className={`btn small ${overlayEnabled?'teal':''}`} onClick={()=>setOverlayEnabled(!overlayEnabled)}><Layers size={12}/>{overlayEnabled?'Hide masks':'Show masks'}</button>}<button className={`btn small ${segmentMode?'teal':''}`} onClick={()=>setSegmentMode(!segmentMode)} disabled={isSimulation}><ScanLine size={12}/>{segmentMode?'Click a point…':'Click-to-segment'}</button></div></div>
      <div className="player-wrap"><video ref={videoRef} controls playsInline preload="metadata" onClick={segmentClick} onTimeUpdate={updateOverlayFrame} onSeeked={updateOverlayFrame} style={{cursor:segmentMode?'crosshair':'default'}} src={`/api/videos/${audit.video.id}/proxy.mp4`}/>{overlayUrl&&<img className="video-overlay" src={overlayUrl} alt="SAM segmentation overlays" onError={e=>{e.currentTarget.style.display='none'}}/>}</div>
      <div className="player-controls"><span>FRAME</span><span>{videoRef.current?Math.round((videoRef.current.currentTime||0)*report.video.fps):'—'} / {report.video.frames}</span><input type="range" min={0} max={report.video.duration_s} step={1/report.video.fps} onChange={e=>seek(Number(e.target.value))}/><span>{timecode(videoRef.current?.currentTime||0)}</span></div>
      {segmentMode&&<div className="toast">Click the object in the video to send a positive point prompt to SAM. Hold Shift for a negative point in the next iteration.</div>}
      <div className="timeline"><div className="section-row"><h3 style={{margin:0}}>Procedure timeline</h3><span className="helper">Select a bar to seek</span></div>
       {stepRows.map(([stepId,group])=><div className="timeline-row" key={stepId}><div className="timeline-label">{stepId} · {group.title}</div><div className="timeline-track">{group.rows.map((item:any)=>{const f=item.frames||[];if(!f.length)return null;const left=(Math.max(0,f[0])/report.video.frames)*100;const width=Math.max(1,((Math.max(f[0],f[1])-f[0])/report.video.frames)*100);return <button key={item.id} title={`${item.id} · ${item.status}`} className={`timeline-bar ${item.status==='missing'?'missing':item.status==='unverifiable'?'unverifiable':item.status==='blocked'?'blocked':''}`} style={{left:`${left}%`,width:`${width}%`}} onClick={()=>seek(f[0]/report.video.fps)}/>})}</div></div>)}
       <div className="timeline-legend"><span><i className="legend-dot" style={{background:'#4ba4a3'}}/>Observed</span><span><i className="legend-dot" style={{background:'#e6a2a0'}}/>Possible omission</span><span><i className="legend-dot" style={{background:'#efc77e'}}/>Unverifiable</span><span><i className="legend-dot" style={{background:'#b8c4d0'}}/>Blocked</span></div>
      </div>
     </div>
     <div className="panel" style={{marginTop:14}}><div className="section-row"><h2>Step results</h2><span className="helper">Expected vs. observed</span></div><div className="steps-list">{report.instances.map((item:any)=><div key={item.id} className="step-row"><div><div className="step-name">{item.id} · {item.title}</div><div className="step-note">{item.rationale}</div></div><div style={{display:'flex',gap:8,alignItems:'center'}}><span className="step-status">{item.status.replace('_',' ')}</span>{item.status!=='observed'&&<button className="btn small" onClick={()=>addObservation(item.id)}>Add evidence</button>}</div></div>)}</div></div>
    </div>
    <div>
     <div className="panel"><div className="section-row"><h2>Possible discrepancies</h2><span className={`pill ${report.status==='CLEAN'?'clean':'review'}`}>{report.status}</span></div>
      {findings.length===0?<div className="empty" style={{padding:22}}><CircleAlert size={22} color="#5e9d7b"/><h3 style={{marginTop:8}}>No open discrepancies</h3><p style={{marginBottom:0}}>The available evidence supports the required steps.</p></div>:<div className="finding-list">{findings.map(f=><div className={`finding-card ${selectedFinding?.id===f.id?'selected':''}`} key={f.id} onClick={()=>{setSelected(f.id);seek(f.key_moment_s)}}>
       <div className="finding-tags"><span className={`pill ${f.severity==='high'?'high':f.severity==='medium'?'medium':f.severity==='check'?'check':'info'}`}>{f.severity}</span><span className="pill info">{f.type}</span>{f.status!=='open'&&<span className="pill clean">{f.status}</span>}</div>
       <h3>{f.explanation}</h3><p>{f.instances.join(', ')} · {timecode(f.time_range_s[0])}–{timecode(f.time_range_s[1])} · confidence {(f.confidence*100).toFixed(0)}%</p>
       {selectedFinding?.id===f.id&&<div className="finding-evidence"><div className="helper">Evidence · {f.evidence.procedure_clause}</div><ul>{f.evidence.measurements.map((m,i)=><li key={i}>{m}</li>)}</ul><div className="evidence-frames">{f.evidence.frames.slice(0,3).map((frame,i)=><div key={i}><img src={frame.image||''} alt={`Evidence frame ${frame.frame}`} onClick={e=>{e.stopPropagation();seek(frame.t)}}/><small>F{String(frame.frame).padStart(5,'0')} · {timecode(frame.t)}</small></div>)}</div><p style={{marginTop:9,color:'#66778a'}}>{f.evidence.rationale}</p>
        {f.status==='open'&&!isSimulation&&<div style={{display:'flex',gap:7,marginTop:10}}><button className="btn small teal" onClick={e=>{e.stopPropagation();reviewFinding(f,'confirm_finding')}} disabled={busy}><Check size={12}/>Confirm</button><button className="btn small danger" onClick={e=>{e.stopPropagation();reviewFinding(f,'dismiss_finding')}} disabled={busy}><X size={12}/>Dismiss</button></div>}</div>}
      </div>)}</div>}
     </div>
     <div className="panel" style={{marginTop:14}}><div className="section-row"><h2>Measured physical signals</h2><span className="helper">SAM-derived geometry</span></div>
      {(report.primitives||[]).length===0?<div className="helper">No threshold-crossing contact, carry or rotation signals were measured in this run.</div>:<div className="steps-list">{(report.primitives||[]).map((p:Primitive,i:number)=><div className="step-row" key={`${p.type}-${p.start_frame}-${i}`}><div><div className="step-name">{p.type.toUpperCase()} · {p.objects.join(' + ')}</div><div className="step-note">{timecode(p.start_frame/report.video.fps)}–{timecode(p.end_frame/report.video.fps)} · {Object.entries(p.features||{}).map(([k,v])=>`${k.replaceAll('_',' ')} ${typeof v==='number'?Number(v.toFixed(2)):String(v)}`).join(' · ')}</div></div><span className="step-status">{Math.round(p.confidence*100)}%</span></div>)}</div>}
      <div className="helper" style={{marginTop:8}}>Contact and carrying are mask-overlap measurements; tool rotation is measured while the tool overlaps its step subject. Zone-specific insertion and placement remain unmeasured until a grounded or reviewer-drawn zone map is available.</div>
     </div>
     <div className="panel" style={{marginTop:14}}><div className="section-row"><h2>Audit integrity</h2><button className="btn small" onClick={showLedger}><Fingerprint size={12}/>Verify ledger</button></div><div className="ledger-box">{report.ledger_head||'Ledger head not yet available'}</div>{ledger&&<div className="toast" style={{marginBottom:0,marginTop:10}}><strong>{ledger.valid?'Chain verified':'Chain integrity error'}</strong> · {ledger.entries} entries{ledger.broken_at!==null&&ledger.broken_at!==undefined?` · broken at sequence ${ledger.broken_at}`:''}</div>}</div>
     <div className="panel" style={{marginTop:14}}><div className="section-row"><h2>Run versions</h2><button className="btn small" onClick={showDiff}><ChevronRight size={12}/>Version diff</button></div>{audit.runs.map((r:any)=><div className="step-row" key={r.id}><div><div className="step-name">Run v{r.version}</div><div className="step-note">{r.mode} · {r.status}</div></div><span className="step-status">${Number(r.cost_usd||0).toFixed(4)}</span></div>)}{diff&&<div className="toast">Diff · {diff.added.length} added · {diff.resolved.length} resolved · {diff.unchanged.length} unchanged</div>}</div>
    </div>
   </div>
   {report.warnings.length>0&&<div className="notice" style={{marginTop:16}}><strong>Analysis notes</strong><ul style={{margin:'7px 0 0',paddingLeft:18}}>{report.warnings.map((w,i)=><li key={i}>{w}</li>)}</ul></div>}
  </>}
 </main></Shell>;
}
