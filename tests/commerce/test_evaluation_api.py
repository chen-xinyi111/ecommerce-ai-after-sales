import hashlib
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from commerce import evaluation_api
from commerce.models import Principal


def test_report_access_role_file_scope_and_tamper_detection(tmp_path, monkeypatch):
    monkeypatch.setattr(evaluation_api, "REPORT_ROOT", tmp_path)
    app = FastAPI()
    app.include_router(evaluation_api.router)
    actor = Principal(id="USR-001", username="user001", display_name="user", role="user")
    app.dependency_overrides[evaluation_api.auth.current_actor] = lambda: actor
    with TestClient(app) as client:
        for path in ("summary", "report", "runs/20261010-000000/manifest.json"):
            assert client.get("/api/v1/evaluation/" + path).status_code == 403
        actor = Principal(id="STAFF-001", username="staff001", display_name="staff", role="staff")
        assert client.get("/api/v1/evaluation/report").status_code == 503
        (tmp_path / "评测报告.html").write_text("<h1>saved report</h1>", encoding="utf-8")
        raw = tmp_path / "runs/20261010-000000/manifest.json"
        raw.parent.mkdir(parents=True)
        raw.write_text('{"verified":true}', encoding="utf-8")
        (tmp_path / "summary.json").write_text(
            json.dumps(
                dict(
                    run_id="20261010-000000",
                    raw_files_sha256={
                        "manifest.json": hashlib.sha256(raw.read_bytes()).hexdigest()
                    },
                )
            ),
            encoding="utf-8",
        )
        assert client.get("/api/v1/evaluation/report").status_code == 200
        assert (
            client.get("/api/v1/evaluation/runs/20261010-000000/manifest.json").status_code == 200
        )
        assert client.get("/api/v1/evaluation/runs/20261010-000000/secrets.json").status_code == 404
        assert (
            client.get("/api/v1/evaluation/runs/19990101-000000/manifest.json").status_code == 404
        )
        raw.write_text('{"verified":false}', encoding="utf-8")
        assert (
            client.get("/api/v1/evaluation/runs/20261010-000000/manifest.json").status_code == 409
        )
