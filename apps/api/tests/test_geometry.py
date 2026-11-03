import numpy as np
from motionledger.perception.geometry import mask_features, in_zone_ratio, rotation_features
from motionledger.signals.detectors import hysteresis_intervals

def test_mask_features():
    mask=np.zeros((20,20),dtype=np.uint8); mask[5:10,7:13]=1
    f=mask_features(mask)
    assert f["area"]==30
    assert f["centroid"]==(9.5,7.0)
    assert f["bbox"]==(7,5,13,10)

def test_zone_ratio():
    obj=np.zeros((10,10),dtype=np.uint8); obj[2:6,2:6]=1
    zone=np.zeros_like(obj); zone[2:6,2:4]=1
    assert in_zone_ratio(obj,zone)==.5

def test_rotation_principal_axis_unwraps():
    result=rotation_features([80, 88, -84, -70, -80, 75, 65])
    assert result["total_deg"] > 30
    assert result["reversals"] >= 1

def test_hysteresis_finds_stable_interval():
    signal=[0,0,.03,.04,.03,.009,0]
    found=hysteresis_intervals(signal,.02,.01,.1,10)
    assert len(found)==1
    assert found[0].start_frame==2
