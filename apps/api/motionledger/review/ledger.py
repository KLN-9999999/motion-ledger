from __future__ import annotations
import hashlib, json, uuid
from datetime import datetime, timezone
from typing import Any
from ..db import Database

ZERO_HASH = "0" * 64

def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

def sha256_hex(value: str | bytes) -> str:
    if isinstance(value, str): value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def append_entry(db: Database, audit_id: str, kind: str, payload: dict) -> dict:
    # Acquire the SQLite write reservation before reading the previous link. This
    # keeps sequence allocation + append atomic when two reviewers act together.
    payload_json = canonical_json(payload)
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        previous = conn.execute(
            "SELECT seq, hash FROM ledger_entries WHERE audit_id=? ORDER BY seq DESC LIMIT 1",
            (audit_id,),
        ).fetchone()
        body = {
            "seq": (previous["seq"] + 1) if previous else 0,
            "audit_id": audit_id,
            "kind": kind,
            "payload_sha256": sha256_hex(payload_json),
            "prev_hash": previous["hash"] if previous else ZERO_HASH,
            "at": utc_now_iso(),
        }
        digest = sha256_hex(canonical_json(body))
        entry = {"id": str(uuid.uuid4()), **body, "hash": digest, "payload": payload}
        conn.execute(
            "INSERT INTO ledger_entries "
            "(id,audit_id,seq,kind,payload_json,payload_sha256,prev_hash,hash,at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (entry["id"], audit_id, body["seq"], kind, payload_json, body["payload_sha256"],
             body["prev_hash"], digest, body["at"]),
        )
    return entry

def verify_chain(db: Database, audit_id: str) -> dict:
    entries = db.all("SELECT * FROM ledger_entries WHERE audit_id=? ORDER BY seq", (audit_id,))
    expected_prev = ZERO_HASH
    broken_at = None
    for idx, row in enumerate(entries):
        payload = json.loads(row["payload_json"])
        payload_hash = sha256_hex(canonical_json(payload))
        body = {"seq": row["seq"], "audit_id": audit_id, "kind": row["kind"],
                "payload_sha256": row["payload_sha256"], "prev_hash": row["prev_hash"], "at": row["at"]}
        calculated = sha256_hex(canonical_json(body))
        if row["seq"] != idx or row["prev_hash"] != expected_prev or payload_hash != row["payload_sha256"] or calculated != row["hash"]:
            broken_at = row["seq"]; break
        expected_prev = row["hash"]
    return {"valid": broken_at is None, "entries": len(entries), "broken_at": broken_at,
            "head": entries[-1]["hash"] if entries else ZERO_HASH,
            "items": [{**r, "payload": json.loads(r.pop("payload_json"))} for r in entries]}
