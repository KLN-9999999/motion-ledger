from motionledger.db import Database
from motionledger.review.ledger import append_entry, verify_chain

def test_hash_chain_verifies_and_detects_tampering(tmp_path):
    db=Database(tmp_path/"ledger.db")
    # Minimal table is already created by Database.
    db.insert_json("videos",{"id":"v","original_name":"v.mp4","original_path":"/tmp/v","proxy_path":"/tmp/v","sha256":"x","duration_s":1,"fps":15,"frames":15,"width":10,"height":10,"file_id":None,"status":"ready","created_at":"now"})
    db.insert_json("procedures",{"id":"p","version":1,"title":"P","body_json":{},"created_at":"now"})
    db.insert_json("audits",{"id":"a","video_id":"v","procedure_id":"p","procedure_version":1,"created_at":"now"})
    append_entry(db,"a","audit.created",{"x":1})
    append_entry(db,"a","run.created",{"x":2})
    result=verify_chain(db,"a")
    assert result["valid"] is True and result["entries"]==2
    db.execute("UPDATE ledger_entries SET payload_json=? WHERE audit_id=? AND seq=0",('{"x":99}',"a"))
    result=verify_chain(db,"a")
    assert result["valid"] is False and result["broken_at"]==0


def test_ledger_sequence_is_monotonic_under_concurrent_appends(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    db = Database(tmp_path / "concurrent-ledger.db")
    db.insert_json("videos", {"id":"v","original_name":"v.mp4","original_path":"/tmp/v","proxy_path":"/tmp/v",
        "sha256":"x","duration_s":1,"fps":15,"frames":15,"width":10,"height":10,"file_id":None,"status":"ready","created_at":"now"})
    db.insert_json("procedures", {"id":"p","version":1,"title":"P","body_json":{},"created_at":"now"})
    db.insert_json("audits", {"id":"a","video_id":"v","procedure_id":"p","procedure_version":1,"created_at":"now"})
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda n: append_entry(db, "a", "review.event", {"n": n}), range(24)))
    result = verify_chain(db, "a")
    assert result["valid"] is True
    assert [item["seq"] for item in result["items"]] == list(range(24))
