"""Experiment management API and optional built-frontend serving."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import secrets
from threading import Lock
from typing import Literal
from uuid import uuid4
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from .run import load_scenario
from .runner import run_experiment


class RunRequest(BaseModel):
    method: Literal["co_ioa", "ioa_original", "co_ioa_no_forecast"] = "co_ioa"
    scenario: str = "delayed_contact"
    backend: Literal["ioa", "offline"] = "ioa"
    planner: Literal["rule", "llm"] = "rule"
    seed: int = Field(default=0, ge=0)


class RunManager:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = Lock()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="co-ioa-experiment")
        self.jobs = {}
        for path in self.root.glob("*/job.json"):
            row = json.loads(path.read_text(encoding="utf-8"))
            if row["status"] in {"queued", "running"}:
                row.update(status="interrupted", error="previous API process stopped before completion")
            self.jobs[row["run_id"]] = row

    def save(self, row):
        path = self.root / row["run_id"]
        path.mkdir(exist_ok=True)
        (path / "job.json").write_text(json.dumps(row, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")

    def submit(self, request):
        run_id = uuid4().hex
        row = {"run_id": run_id, **request.model_dump(), "status": "queued", "error": None}
        with self.lock:
            self.jobs[run_id] = row
            self.save(row)
        self.pool.submit(self.execute, run_id)
        return dict(row)

    def execute(self, run_id):
        with self.lock:
            row = self.jobs[run_id]
            row["status"] = "running"
            self.save(row)
        try:
            scenario = load_scenario(row["scenario"])
            scenario["seed"] = row["seed"]
            result = run_experiment(scenario, self.root/run_id, backend=row["backend"], planner=row["planner"], method=row.get("method", "co_ioa"))
            with self.lock:
                row.update(status="error" if result["outcome"] == "error" else "completed",
                    outcome=result["outcome"], ended_at=result["ended_at"],
                    error=result["reason"] if result["outcome"] == "error" else None)
        except Exception as exc:
            with self.lock:
                # Domain/configuration errors are safe; unexpected errors omit
                # raw network URLs/headers to keep credentials out of artifacts.
                row.update(status="error", error=str(exc) if isinstance(exc, ValueError) else type(exc).__name__)
        finally:
            with self.lock:
                self.save(row)

    def get(self, run_id):
        with self.lock:
            if run_id not in self.jobs:
                raise KeyError(run_id)
            return dict(self.jobs[run_id])

    def close(self):
        self.pool.shutdown(wait=True, cancel_futures=False)


def create_app(output=None, frontend=None):
    manager = RunManager(output or os.getenv("CO_IOA_RUNS_DIR", "runs/api"))

    @asynccontextmanager
    async def lifespan(app):
        yield
        manager.close()

    app = FastAPI(title="CO-IoA experiments", lifespan=lifespan)
    app.state.manager = manager
    names = sorted(p.stem for p in (Path(__file__).parent/"scenarios").glob("*.json"))

    @app.get("/api/health")
    def health():
        return {"status": "ok", "mechanism": "CO-IoA", "model_configured": bool(os.getenv("CO_IOA_MODEL"))}

    @app.get("/api/scenarios")
    def scenarios():
        return [{"name": name, "objective": load_scenario(name)["objective"]} for name in names]

    @app.get("/api/runs")
    def runs():
        with manager.lock:
            return [dict(row) for row in reversed(list(manager.jobs.values()))]

    @app.post("/api/runs", status_code=202)
    def launch(body: RunRequest, request: Request):
        configured = os.getenv("CO_IOA_CONTROL_KEY", "")
        supplied = request.headers.get("authorization", "").removeprefix("Bearer ")
        if configured and not secrets.compare_digest(configured, supplied):
            raise HTTPException(403, "invalid experiment control key")
        if body.scenario not in names:
            raise HTTPException(404, "unknown scenario")
        from .methods import require_runnable, AdapterNotReadyError
        try:
            require_runnable(body.method)
        except AdapterNotReadyError as exc:
            raise HTTPException(409, str(exc)) from None
        return manager.submit(body)

    @app.get("/api/runs/{run_id}")
    def status(run_id: str):
        try:
            return manager.get(run_id)
        except KeyError:
            raise HTTPException(404, "unknown run") from None

    @app.get("/api/runs/{run_id}/{artifact}")
    def artifact(run_id: str, artifact: str):
        try:
            manager.get(run_id)
        except KeyError:
            raise HTTPException(404, "unknown run") from None
        allowed = {"result": "result.json", "events": "events.jsonl", "scenario": "scenario.json", "manifest": "manifest.json"}
        if artifact not in allowed:
            raise HTTPException(404, "unknown artifact")
        path = manager.root / run_id / allowed[artifact]
        if not path.is_file():
            raise HTTPException(409, "artifact not available yet")
        return FileResponse(path, media_type="application/x-ndjson" if artifact == "events" else "application/json")

    static = Path(frontend or os.getenv("CO_IOA_FRONTEND_DIR", "im_server/frontend/build"))
    if static.is_dir():
        app.mount("/", StaticFiles(directory=static, html=True), name="frontend")
    return app


def main():
    import argparse
    import uvicorn
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--output", type=Path, default=Path("runs/api"))
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()
    from .config import load_environment
    load_environment(args.env_file)
    uvicorn.run(create_app(args.output), host=args.host, port=args.port, access_log=False)


if __name__ == "__main__":
    main()
