from motionledger.schemas.report import StepObservation, VideoMeta, RunMeta, Primitive
from motionledger.engine.compare import compare, expand_instances


def run_meta():
    return RunMeta(version=1,parent=None,models={"perception":"test","reasoning":"test"},cost_usd=0,mode="TEST",created_at="2026-11-02T00:00:00Z")

def full_observations():
    rows=[StepObservation(id="g",step_id="S1",status="observed",start_frame=30,end_frame=50,confidence=.9,duration_s=1.3)]
    for i in range(1,5):
        rows.append(StepObservation(id=f"i{i}",step_id="S2",instance_key=f"hole_{i}",status="observed",start_frame=100+i*20,end_frame=110+i*20,confidence=.9,duration_s=.8))
        rows.append(StepObservation(id=f"t{i}",step_id="S3",instance_key=f"hole_{i}",status="observed",start_frame={1:300,3:330,2:360,4:390}[i],end_frame={1:340,3:370,2:400,4:430}[i],confidence=.9,duration_s=2.6))
    return rows

def test_procedure_expands_repeated_steps(procedure):
    instances=expand_instances(procedure)
    assert len([x for x in instances if x["step_id"]=="S2"])==4
    assert {x["instance_key"] for x in instances if x["step_id"]=="S3"}=={"hole_1","hole_2","hole_3","hole_4"}

def test_clean_run_has_no_findings(procedure):
    report=compare(sop=procedure,observations=full_observations(),primitives=[],video=VideoMeta(duration_s=60,fps=15,frames=900,width=1280,height=720),
                   audit_id="a",run=run_meta(),video_id="v")
    assert report.status=="CLEAN"
    assert report.findings==[]

def test_missing_and_occluded_are_distinguished(procedure):
    obs=full_observations()
    obs=[o for o in obs if not (o.step_id=="S3" and o.instance_key in {"hole_3","hole_4"})]
    obs += [StepObservation(id="m",step_id="S3",instance_key="hole_3",status="not_performed",confidence=.9,expected_window=(500,899)),
            StepObservation(id="u",step_id="S3",instance_key="hole_4",status="cannot_determine",confidence=.4,start_frame=600,end_frame=650,missing_evidence="Hand covers hole 4")]
    report=compare(sop=procedure,observations=obs,primitives=[],video=VideoMeta(duration_s=60,fps=15,frames=900,width=1280,height=720),
                   audit_id="a",run=run_meta(),video_id="v")
    types={f.type for f in report.findings}
    assert "INCOMPLETE" in types
    assert "UNVERIFIABLE" in types
    unverifiable=next(f for f in report.findings if f.type=="UNVERIFIABLE")
    assert unverifiable.severity=="check"
    assert unverifiable.evidence.frames

def test_short_step_is_incomplete(procedure):
    obs=full_observations()
    target=next(o for o in obs if o.step_id=="S3" and o.instance_key=="hole_2")
    target.duration_s=.3
    report=compare(sop=procedure,observations=obs,primitives=[],video=VideoMeta(duration_s=60,fps=15,frames=900,width=1280,height=720),
                   audit_id="a",run=run_meta(),video_id="v")
    assert any(f.type=="INCOMPLETE" and "S3@hole_2" in f.instances for f in report.findings)

def test_order_violation_detected(procedure):
    obs=full_observations()
    s1=next(o for o in obs if o.step_id=="S1"); s1.start_frame=80; s1.end_frame=180
    s2=next(o for o in obs if o.step_id=="S2" and o.instance_key=="hole_1"); s2.start_frame=120
    report=compare(sop=procedure,observations=obs,primitives=[],video=VideoMeta(duration_s=60,fps=15,frames=900,width=1280,height=720),
                   audit_id="a",run=run_meta(),video_id="v")
    assert any(f.type=="ORDER_VIOLATION" for f in report.findings)

def test_pattern_violation_detected(procedure):
    obs=full_observations()
    # Actual order 1-2-3-4 conflicts with canonical cross order 1-3-2-4.
    for idx, zone in enumerate(("hole_1","hole_2","hole_3","hole_4")):
        item=next(o for o in obs if o.step_id=="S3" and o.instance_key==zone)
        item.start_frame=300+idx*60; item.end_frame=item.start_frame+30
    report=compare(sop=procedure,observations=obs,primitives=[],video=VideoMeta(duration_s=60,fps=15,frames=900,width=1280,height=720),
                   audit_id="a",run=run_meta(),video_id="v")
    assert any(f.type=="PATTERN_VIOLATION" for f in report.findings)

def test_postcondition_is_checked_when_measurement_exists(procedure):
    report=compare(sop=procedure,observations=full_observations(),primitives=[],end_state={"P1":3},
                   video=VideoMeta(duration_s=60,fps=15,frames=900,width=1280,height=720),audit_id="a",run=run_meta(),video_id="v")
    assert any(f.type=="POSTCONDITION_FAILED" for f in report.findings)

def test_finding_ids_are_stable(procedure):
    obs=full_observations(); obs=[o for o in obs if not (o.step_id=="S3" and o.instance_key=="hole_3")]
    first=compare(sop=procedure,observations=obs,primitives=[],video=VideoMeta(duration_s=60,fps=15,frames=900,width=1280,height=720),audit_id="a",run=run_meta(),video_id="v")
    second=compare(sop=procedure,observations=obs,primitives=[],video=VideoMeta(duration_s=60,fps=15,frames=900,width=1280,height=720),audit_id="a",run=run_meta(),video_id="v")
    assert [x.id for x in first.findings]==[x.id for x in second.findings]


def test_prerequisites_are_resolved_even_when_steps_are_listed_out_of_order(procedure):
    # Deliberately put S2 before S1 in the declaration order. The dependency graph,
    # not list order, must determine readiness and prevent cascading false findings.
    reordered = procedure.model_copy(deep=True)
    reordered.steps = [reordered.steps[1], reordered.steps[0], *reordered.steps[2:]]
    rows = [o for o in full_observations() if o.step_id not in {"S1", "S2"}]
    report = compare(sop=reordered, observations=rows, primitives=[],
                     video=VideoMeta(duration_s=60, fps=15, frames=900, width=1280, height=720),
                     audit_id="a", run=run_meta(), video_id="v")
    s2_states = {x["id"]: x["status"] for x in report.instances if x["step_id"] == "S2"}
    assert set(s2_states.values()) == {"blocked"}
    assert not any(f.type in {"OMISSION", "INCOMPLETE"} and any(i.startswith("S2@") for i in f.instances)
                   for f in report.findings)


def test_reviewer_add_observation_override_is_applied(procedure):
    rows = [o for o in full_observations() if not (o.step_id == "S3" and o.instance_key == "hole_3")]
    report = compare(sop=procedure, observations=rows, primitives=[],
                     video=VideoMeta(duration_s=60, fps=15, frames=900, width=1280, height=720),
                     audit_id="a", run=run_meta(), video_id="v",
                     overrides=[{"kind":"add_observation","instance_id":"S3@hole_3",
                                 "payload":{"start_frame":580,"end_frame":620,"note":"Reviewer found the wrench stroke."}}])
    instance = next(x for x in report.instances if x["id"] == "S3@hole_3")
    assert instance["status"] == "observed"
    assert not any(f.type == "INCOMPLETE" and "S3@hole_3" in f.instances for f in report.findings)


def test_omission_evidence_covers_expected_window(procedure):
    rows = [o for o in full_observations() if not (o.step_id == "S3" and o.instance_key == "hole_3")]
    rows.append(StepObservation(id="m",step_id="S3",instance_key="hole_3",status="not_performed",
                                confidence=.9,expected_window=(450,750),rationale="Wrench remained in tray."))
    report = compare(sop=procedure, observations=rows, primitives=[],
                     video=VideoMeta(duration_s=60, fps=15, frames=900, width=1280, height=720),
                     audit_id="a", run=run_meta(), video_id="v")
    finding = next(f for f in report.findings if f.type == "INCOMPLETE" and "S3@hole_3" in f.instances)
    assert finding.time_range_s == (30.0, 50.0)
    assert len(finding.evidence.frames) >= 2
    assert any("Wrench remained" in item for item in finding.evidence.measurements)


def test_missing_and_too_short_instances_are_merged(procedure):
    rows = [o for o in full_observations() if not (o.step_id == "S3" and o.instance_key in {"hole_2", "hole_3"})]
    rows.append(StepObservation(id="short",step_id="S3",instance_key="hole_2",status="observed",
                                start_frame=500,end_frame=505,confidence=.9,duration_s=.3))
    rows.append(StepObservation(id="missing",step_id="S3",instance_key="hole_3",status="not_performed",
                                confidence=.9,expected_window=(510,700)))
    report = compare(sop=procedure,observations=rows,primitives=[],
                     video=VideoMeta(duration_s=60,fps=15,frames=900,width=1280,height=720),
                     audit_id="a",run=run_meta(),video_id="v")
    findings = [f for f in report.findings if f.type == "INCOMPLETE" and ({"S3@hole_2","S3@hole_3"} & set(f.instances))]
    assert len(findings) == 1
    assert set(findings[0].instances) == {"S3@hole_2", "S3@hole_3"}
