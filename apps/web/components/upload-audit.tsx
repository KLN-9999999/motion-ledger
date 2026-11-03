"use client";
import { useEffect, useState, type FormEvent } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { FileVideo, ShieldCheck, Upload, ArrowLeft, LoaderCircle } from "lucide-react";
import { Shell } from "./shell";
import { api } from "@/lib/api";

type Procedure={id:string;version:number;title:string};
type Video={id:string;name:string;duration_s:number;frames:number;width:number;height:number};
export default function UploadAudit(){
 const router=useRouter();const [procedures,setProcedures]=useState<Procedure[]>([]);const [procedure,setProcedure]=useState('flange-assembly');const [file,setFile]=useState<File|null>(null);const [consent,setConsent]=useState(false);const [busy,setBusy]=useState(false);const [message,setMessage]=useState('');const [error,setError]=useState('');
 useEffect(()=>{api<Procedure[]>('/api/procedures').then(setProcedures).catch(e=>setError(e.message));},[]);
 async function submit(e:FormEvent){e.preventDefault();if(!file||!consent)return;setBusy(true);setError('');setMessage('Normalizing video…');try{
   const form=new FormData();form.append('file',file);const video=await api<Video>('/api/videos',{method:'POST',body:form});setMessage('Creating audit and starting Meta analysis…');
   const run=await api<{audit_id:string}>('/api/audits',{method:'POST',body:JSON.stringify({video_id:video.id,procedure_id:procedure})});router.push(`/audits/${run.audit_id}`);
  }catch(e){setError((e as Error).message);setMessage('');}finally{setBusy(false)}}
 return <Shell><main className="main"><div style={{marginBottom:22}}><Link href="/" className="btn small"><ArrowLeft size={13}/>Workspace</Link></div><div className="eyebrow">Create a new audit</div><h1>Bring a process into focus.</h1><p className="subhead">Upload a top-down recording, select its expected procedure, and let MotionLedger prepare an evidence-linked review.</p>
   <form className="panel form-panel" onSubmit={submit} style={{marginTop:24}}>
    <div className="field"><label>1 · Assembly procedure</label><select value={procedure} onChange={e=>setProcedure(e.target.value)}>{procedures.map(p=><option key={p.id} value={p.id}>{p.title} · v{p.version}</option>)}</select><span className="helper">The selected procedure is pinned to this audit. Later edits create a new procedure version.</span></div>
    <div className="field"><label>2 · Assembly video</label><div className="upload-drop"><FileVideo size={28} color="#438a98" style={{marginBottom:10}}/><div style={{fontWeight:700,fontSize:13,marginBottom:5}}>Choose your station recording</div><div className="helper" style={{marginBottom:14}}>MP4 or MOV · up to 5 minutes · up to 1 GiB · top-down view recommended</div><input type="file" accept="video/mp4,video/quicktime,.mp4,.mov,.m4v" onChange={e=>setFile(e.target.files?.[0]||null)}/>{file&&<div className="toast" style={{marginBottom:0,marginTop:12}}>{file.name} · {(file.size/1024/1024).toFixed(1)} MB</div>}</div></div>
    <div className="notice" style={{margin:'18px 0'}}><strong>Recording and data notice.</strong> The video is normalized and stored in this local workspace. Starting a live audit sends the normalized clip to Meta Model API using the server-side API key. Upload only footage you are authorized to process and have given the appropriate notice for. Meta Model API Standard mode is used for real footage.</div>
    <label style={{display:'flex',alignItems:'flex-start',gap:10,fontSize:12,color:'#566779',lineHeight:1.5,marginBottom:18}}><input type="checkbox" checked={consent} onChange={e=>setConsent(e.target.checked)} style={{marginTop:2}}/>I confirm that I am authorized to upload this recording and that the people in the recording have been appropriately informed.</label>
    {error&&<div className="toast error">{error}</div>}{message&&<div className="toast">{message}</div>}
    <button className="btn primary" type="submit" disabled={!file||!consent||busy}>{busy?<><span className="spinner"/>Processing</>:<><Upload size={14}/>Upload and analyze</>}</button>
   </form>
   <div className="notice" style={{maxWidth:760,marginTop:14}}><ShieldCheck size={14} style={{verticalAlign:'middle',marginRight:6}}/>A machine finding is always a possible discrepancy. “Unverifiable” means the critical moment could not be seen; it is not a defect.</div>
 </main></Shell>;
}
