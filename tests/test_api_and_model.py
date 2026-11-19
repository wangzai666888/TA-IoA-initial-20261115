import json
import time
from copy import deepcopy
from threading import Thread
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import httpx
import pytest
from fastapi.testclient import TestClient
from co_ioa.api import create_app
from co_ioa.model_api import ModelAPI
from co_ioa.runner import run_experiment
from co_ioa.scenarios import suite


def test_model_api_config_errors_are_explicit():
    with pytest.raises(ValueError, match="requires"):
        ModelAPI([], environment={})
    with pytest.raises(ValueError, match="API_KEY"):
        ModelAPI([], environment={"CO_IOA_API_BASE": "http://localhost/v1", "CO_IOA_MODEL": "test"})


def test_model_api_request_schema_and_secret_free_audit():
    seen = []
    def respond(request):
        seen.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"requirements":[]}'}}], "usage": {"total_tokens": 10}})
    audit = []
    model = ModelAPI(audit, environment={"CO_IOA_API_BASE": "https://provider.example/v1", "CO_IOA_MODEL": "model-x", "CO_IOA_API_KEY": "private-key"}, transport=httpx.MockTransport(respond))
    assert model("schema", {"objective": "inspect"}, "fix") == '{"requirements":[]}'
    assert seen[0].headers["authorization"] == "Bearer private-key"
    body = json.loads(seen[0].content)
    assert body["model"] == "model-x" and body["response_format"]["type"] == "json_object"
    assert audit[0]["usage"]["total_tokens"] == 10
    assert "private-key" not in json.dumps(audit)


def test_model_api_error_does_not_log_provider_body():
    transport = httpx.MockTransport(lambda request: httpx.Response(401, text="private-key debug body"))
    model = ModelAPI([], environment={"CO_IOA_API_BASE": "http://localhost/v1", "CO_IOA_MODEL": "x", "CO_IOA_API_KEY": "private-key"}, transport=transport)
    with pytest.raises(ValueError, match="HTTP 401") as exc:
        model("", {}, "")
    assert "private-key" not in str(exc.value)


@pytest.mark.parametrize("backend", ["offline", "ioa"])
def test_llm_end_to_end_uses_http_and_existing_matcher(tmp_path, monkeypatch, backend):
    scenario = suite()["delayed_contact"]
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(body)
            response = {"choices": [{"message": {"content": json.dumps({"requirements": scenario["requirements"], "change_summary": "initial specification"})}}], "usage": {"total_tokens": 12}}
            content = json.dumps(response).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(content))); self.end_headers(); self.wfile.write(content)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    monkeypatch.setenv("CO_IOA_API_BASE", f"http://127.0.0.1:{server.server_port}/v1")
    monkeypatch.setenv("CO_IOA_MODEL", "local-test-model")
    monkeypatch.setenv("CO_IOA_API_KEY", "test-only-key")
    monkeypatch.setenv("CO_IOA_MODEL_TRUST_ENV", "false")
    try:
        result = run_experiment(scenario, tmp_path, backend=backend, planner="llm")
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)
    assert result["outcome"] == "success" and result["ended_at"] == 660
    assert result["planner"] == "LLMPlanner"
    assert len(calls) == len(result["model_calls"]) == 1
    assert "capability templates" in calls[0]["messages"][0]["content"]
    for path in tmp_path.iterdir():
        assert "test-only-key" not in path.read_text(encoding="utf-8")


def wait_for_job(client, run_id):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        row = client.get(f"/api/runs/{run_id}").json()
        if row["status"] in {"completed", "error"}: return row
        time.sleep(0.02)
    raise AssertionError("experiment did not finish")


def test_experiment_api_artifacts_and_path_validation(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        assert client.get("/api/health").json()["mechanism"] == "CO-IoA"
        assert len(client.get("/api/scenarios").json()) == 13
        assert client.post("/api/runs", json={"scenario": "../LICENSE"}).status_code == 404
        response = client.post("/api/runs", json={"scenario": "delayed_contact", "backend": "offline"})
        assert response.status_code == 202
        rid = response.json()["run_id"]
        assert wait_for_job(client, rid)["status"] == "completed"
        assert client.get(f"/api/runs/{rid}/result").json()["ended_at"] == 660
        assert client.get(f"/api/runs/{rid}/manifest").json()["source_sha256"]
        assert "assignment_committed" in client.get(f"/api/runs/{rid}/events").text
        assert client.get("/api/runs/missing/result").status_code == 404
        assert client.get(f"/api/runs/{rid}/LICENSE").status_code == 404


def test_experiment_control_key(tmp_path, monkeypatch):
    monkeypatch.setenv("CO_IOA_CONTROL_KEY", "control-secret")
    with TestClient(create_app(tmp_path)) as client:
        assert client.post("/api/runs", json={}).status_code == 403
        response = client.post("/api/runs", json={"backend": "offline"}, headers={"Authorization": "Bearer control-secret"})
        assert response.status_code == 202
        assert wait_for_job(client, response.json()["run_id"])["status"] == "completed"


def test_llm_job_without_configuration_is_error_not_rule_fallback(tmp_path, monkeypatch):
    monkeypatch.delenv("CO_IOA_MODEL", raising=False)
    with TestClient(create_app(tmp_path)) as client:
        row = client.post("/api/runs", json={"planner": "llm", "backend": "offline"}).json()
        final = wait_for_job(client, row["run_id"])
        assert final["status"] == "error" and "requires" in final["error"]
