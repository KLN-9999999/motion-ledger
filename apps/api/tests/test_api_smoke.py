from fastapi.testclient import TestClient

from motionledger import main as api
from motionledger.db import Database


def test_demo_api_workflow_and_resegment_does_not_create_orphan_run(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(api, "db", Database(tmp_path / "motionledger-test.db"))
    monkeypatch.setattr(api.settings, "data_dir", data_dir)
    monkeypatch.setattr(api.settings, "model_api_key", "")

    with TestClient(api.app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["model_api_configured"] is False

        procedures = client.get("/api/procedures")
        assert procedures.status_code == 200
        assert any(p["id"] == "flange-assembly" for p in procedures.json())

        seeded = client.post("/api/demo/seed")
        assert seeded.status_code == 201
        ids = seeded.json()
        details = client.get(f"/api/audits/{ids['audit_id']}").json()
        assert details["latest_run"]["status"] == "completed"

        report_response = client.get(f"/api/runs/{ids['run_id']}/report")
        assert report_response.status_code == 200
        report = report_response.json()
        assert report["run"]["mode"] == "SIMULATION"
        assert any(f["type"] == "INCOMPLETE" for f in report["findings"])
        assert any(f["type"] == "UNVERIFIABLE" for f in report["findings"])
        assert any("SIMULATION ONLY" in warning for warning in report["warnings"])

        # Overlay route is frame-addressed and returns transparent PNG when the
        # demo has no real segmentation tracks attached.
        overlay = client.get(f"/api/runs/{ids['run_id']}/overlays/0.png")
        assert overlay.status_code == 200
        assert overlay.headers["content-type"].startswith("image/png")
        assert overlay.content.startswith(b"\x89PNG\r\n\x1a\n")

        # A real upload follows the ffprobe/ffmpeg normalization path and exposes
        # the normalized MP4 and addressable evidence frames.
        demo_video = data_dir / "proxies" / "vid_motionledger_demo.mp4"
        assert demo_video.is_file()
        with demo_video.open("rb") as stream:
            uploaded = client.post("/api/videos", files={"file": ("sample.mp4", stream, "video/mp4")})
        assert uploaded.status_code == 201, uploaded.text
        uploaded_video = uploaded.json()
        proxy = client.get(f"/api/videos/{uploaded_video['id']}/proxy.mp4")
        assert proxy.status_code == 200 and proxy.content[4:8] == b"ftyp"
        frame = client.get(f"/api/videos/{uploaded_video['id']}/frames/0.jpg")
        assert frame.status_code == 200 and frame.headers["content-type"].startswith("image/jpeg")

        ledger = client.get(f"/api/audits/{ids['audit_id']}/ledger").json()
        assert ledger["valid"] is True
        assert ledger["entries"] >= 2

        # A reviewer-added observation must be decoded from the stored JSON payload
        # and applied by the deterministic engine on the next run.
        override = client.post(f"/api/audits/{ids['audit_id']}/overrides", json={
            "kind":"add_observation", "instance_id":"S3@hole_3",
            "payload":{"start_frame":575,"end_frame":615,"note":"Reviewer checked the wrench stroke."}
        })
        assert override.status_code == 201
        rerun = client.post(f"/api/audits/{ids['audit_id']}/runs", json={"mode":"recompare"})
        assert rerun.status_code == 202
        rerun_id = rerun.json()["run_id"]
        for _ in range(40):
            run_state = client.get(f"/api/runs/{rerun_id}").json()
            if run_state["status"] in {"completed", "failed"}:
                break
            import time
            time.sleep(0.05)
        assert run_state["status"] == "completed"
        corrected = client.get(f"/api/runs/{rerun_id}/report").json()
        assert not any(f["type"] == "INCOMPLETE" and "S3@hole_3" in f["instances"]
                       for f in corrected["findings"])
        assert any(f["type"] == "UNVERIFIABLE" and "S3@hole_4" in f["instances"]
                   for f in corrected["findings"])

        # Re-segmentation is currently a dedicated point-prompt endpoint. Rejecting
        # an unsupported run-mode must not leave an orphaned queued run.
        count_before = len(client.get(f"/api/audits/{ids['audit_id']}").json()["runs"])
        rejected = client.post(f"/api/audits/{ids['audit_id']}/runs", json={"mode":"resegment"})
        assert rejected.status_code == 501
        count_after = len(client.get(f"/api/audits/{ids['audit_id']}").json()["runs"])
        assert count_after == count_before
