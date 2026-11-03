from __future__ import annotations
import json, argparse
from pathlib import Path

def main():
    parser=argparse.ArgumentParser(description="Validate labelled MotionLedger evaluation manifests")
    parser.add_argument("--manifest",default=str(Path(__file__).with_name("manifest.json")))
    parser.add_argument("--out",default=str(Path(__file__).with_name("results.md")))
    args=parser.parse_args()
    data=json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    lines=["# MotionLedger evaluation results","","This file is a scaffold. No metrics are reported until labelled footage and predictions are available.","",
           f"Evaluation clips specified: **{len(data['clips'])}**","", "| Clip | Scenario | Expected finding labels |", "|---|---|---|"]
    for clip in data["clips"]:
        lines.append(f"| {clip['id']} | {clip['scenario']} | {', '.join(clip['expected_findings']) or 'None'} |")
    lines += ["","## Required metrics","","- Per-type precision, recall and F1 at temporal IoU ≥ 0.3.","- False-accusation rate; target <5%.","- Unverifiable rate on clean clips; target <10%.","- Median observed-step timestamp error; target <0.5 s.","- Reviewer time saved versus raw-video scrubbing.",""]
    Path(args.out).write_text("\n".join(lines),encoding="utf-8")
    print(f"Wrote {args.out}; this is a manifest summary, not a measured evaluation.")
if __name__=="__main__":main()
