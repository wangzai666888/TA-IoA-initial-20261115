"""One experiment entry used by CLI, API and batch runner."""
import json
import random
import hashlib
import importlib.metadata
import platform
from pathlib import Path
from .event_loop import Simulation


def run_experiment(scenario, output, *, backend="ioa", planner=None):
    scenario = json.loads(json.dumps(scenario))
    if planner is not None:
        scenario["planner"] = planner
    if backend not in {"ioa", "offline"}:
        raise ValueError("unknown experiment backend")
    random.seed(scenario.get("seed", 0))
    sim = Simulation(scenario)
    if backend == "ioa":
        from .ioa_infrastructure import IoAInfrastructure
        with IoAInfrastructure(sim) as infrastructure:
            result = sim.run()
            result["infrastructure"] = infrastructure.snapshot()
    else:
        result = sim.run()
        result["infrastructure"] = {"backend": "offline"}
    sim.last_result = result
    sim.write(Path(output))
    root = Path(__file__).resolve().parents[1]
    sources = [*Path(__file__).parent.glob("*.py"), *root.glob("im_client/*.py"),
               *root.glob("im_client/communication/*.py"), *root.glob("im_server/*.py"), *root.glob("common/*.py")]
    manifest = {"python": platform.python_version(), "platform": platform.platform(),
        "backend": backend, "planner": result["planner"], "seed": scenario.get("seed", 0),
        "scenario_sha256": hashlib.sha256(json.dumps(scenario, sort_keys=True).encode()).hexdigest(),
        "source_sha256": {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(sources)},
        "packages": {name: importlib.metadata.version(name) for name in ["ortools", "fastapi", "httpx", "websockets"]}}
    (Path(output)/"manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    return result
