export type Audit = {
  id: string; video_id: string; procedure_id: string; procedure_version: number; created_at: string;
  original_name: string; duration_s: number; run_id?: string; run_version?: number; run_status?: string;
  stage?: string; cost_usd?: number; finding_count?: number; report_status?: string;
};
export type Finding = {
  id: string; type: string; instances: string[]; severity: string; confidence: number;
  time_range_s: [number,number]; key_moment_s: number; explanation: string;
  status: "open"|"confirmed"|"dismissed";
  evidence: {frames: {frame:number;t:number;role:string;image?:string}[];measurements:string[];procedure_clause:string;rationale:string};
};
export type StepObservation = {
  id: string; step_id: string; instance_key: string; status: string;
  start_frame: number; end_frame: number; confidence: number; rationale: string;
};
export type Primitive = {type:string;objects:string[];zone:string;start_frame:number;end_frame:number;features:Record<string,unknown>;confidence:number};
export type Report = {
  audit_id: string; run: {version:number;parent?:string;models:Record<string,string>;cost_usd:number;mode:string;created_at:string};
  video: {duration_s:number;fps:number;frames:number;width:number;height:number};
  procedure: {id:string;version:number;title:string}; status: string; instances: any[];
  findings: Finding[]; step_observations: StepObservation[]; primitives: Primitive[]; warnings: string[]; ledger_head: string;
};
