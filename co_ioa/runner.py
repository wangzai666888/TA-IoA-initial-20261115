"""One experiment entry used by CLI, API and batch runner."""
import json
import random
import hashlib
import importlib.metadata
import platform
from pathlib import Path
from .event_loop import Simulation


def run_experiment(scenario, output, *, backend="ioa", planner=None, method="co_ioa", protocol=None,
                   native_env_file=None, native_model=None, native_embedding_model=None,
                   native_embedding_dimension=None, native_max_turns=20,
                   native_wall_wait_seconds=1200):
    from .methods import require_runnable
    from .comparison_protocol import load_protocol, validate_scenario, model_settings, digest
    spec = require_runnable(method)
    frozen = load_protocol(protocol) if protocol is not None else None
    if frozen is not None:
        validate_scenario(scenario, frozen)
        declared = frozen["methods"].get(method, {})
        if (method == "co_ioa_no_forecast"
                and declared.get("policy_version") != spec.policy_version):
            raise ValueError("ablation policy differs from protocol; use experiments/comparison_v2/protocol.json")
    previous_result = Path(output) / "result.json"
    if previous_result.is_file():
        previous = json.loads(previous_result.read_text())
        previous_method = previous.get("method", "co_ioa")
        if previous_method != method:
            raise ValueError("output already belongs to another method; choose a separate directory")
        if (spec.policy_version != "unchanged"
                and previous.get("method_policy_version") != spec.policy_version):
            raise ValueError("output already belongs to another policy version; choose a separate directory")
    scenario = json.loads(json.dumps(scenario))
    if planner is not None:
        scenario["planner"] = planner
    if frozen is not None and scenario.get("planner") == "llm" and method != "ioa_original":
        from .comparison_protocol import validate_model_settings
        validate_model_settings(frozen)
    formal_budget = frozen.get("formal_budget") if frozen else None
    early_failure = bool(frozen and frozen.get('early_failure_rule') == 'physical_deadline_v1')
    if formal_budget is not None:
        from .comparison_protocol import validate_model_settings, validate_files, validate_formal_budget
        from .formal_experiments import validate_formal_sources
        if scenario.get("planner") != "llm" or backend != "ioa":
            raise ValueError("formal comparison requires --backend ioa --planner llm")
        validate_model_settings(frozen)
        validate_formal_budget(frozen)
        mismatches = validate_files(frozen)
        mismatches += ["formal source mismatch: " + name for name in validate_formal_sources(frozen)]
        if mismatches:
            raise ValueError("formal comparison inputs changed: " + "; ".join(mismatches))
        if method == 'ioa_original':
            if native_model not in (None, frozen['co_model_configuration']['model']):
                raise ValueError('formal native model differs from frozen model')
            if native_embedding_model is not None or native_embedding_dimension is not None:
                raise ValueError('formal native embedding overrides are not allowed')
            if native_max_turns != frozen['formal_design']['native_max_turns']:
                raise ValueError('formal native max turns differs from frozen value')
            if native_wall_wait_seconds != frozen['formal_design']['native_wall_wait_seconds']:
                raise ValueError('formal native wall wait differs from frozen value')
    if backend not in {"ioa", "offline"}:
        raise ValueError("unknown experiment backend")
    if method == 'ioa_original':
        if backend != 'ioa' or scenario.get('planner', 'llm') != 'llm':
            raise ValueError('ioa_original requires --backend ioa --planner llm')
        from .native_runner import run_native_experiment
        return run_native_experiment(scenario, output, env_file=native_env_file,
            model=native_model, embedding_model=native_embedding_model,
            embedding_dimension=native_embedding_dimension,
            max_turns=native_max_turns, wall_wait_seconds=native_wall_wait_seconds,
            formal_budget=formal_budget, protocol_sha256=digest(frozen) if frozen else None,
            formal_native_config=frozen.get('native_model_configuration') if frozen else None,
            early_failure=early_failure)
    random.seed(scenario.get("seed", 0))
    sim = Simulation(scenario, method=method, formal_budget=formal_budget,
                     early_failure=early_failure)
    if backend == "ioa":
        from .ioa_infrastructure import IoAInfrastructure
        with IoAInfrastructure(sim) as infrastructure:
            result = sim.run()
            result["infrastructure"] = infrastructure.snapshot()
    else:
        result = sim.run()
        result["infrastructure"] = {"backend": "offline"}
    from .external_evidence import evaluate_co
    result["external_judgement"] = evaluate_co(sim, allow_early_failure=early_failure)
    if result['external_judgement']['terminal_certificate'] is not None:
        # Preserve the strategy's diagnostic conclusion separately. The run's
        # outcome is the shared external judge's proven physical result.
        result['controller_outcome_at_stop'] = result['outcome']
        result['controller_reason_at_stop'] = result['reason']
        result['outcome'] = 'failed'
        result['reason'] = 'shared external judge certified impossible on-time completion'
        result['ended_at'] = None
        result['evaluation_valid'] = True
    result.update(method=method, strategy=spec.strategy, uses_communication_forecast=spec.uses_forecast,
                  method_policy_version=spec.policy_version,
                  comparison_ready=bool(formal_budget),
                  evaluation_scope="shared external judge; CO controller outcome is diagnostic")
    sim.last_result = result
    sim.write(Path(output))
    root = Path(__file__).resolve().parents[1]
    sources = [*root.glob("common_environment/*.py"), *Path(__file__).parent.glob("*.py"), *root.glob("im_client/*.py"),
               *root.glob("im_client/communication/*.py"), *root.glob("im_server/*.py"), *root.glob("common/*.py")]
    manifest = {"python": platform.python_version(), "platform": platform.platform(),
        "backend": backend, "method": method, "strategy": spec.strategy,
        "uses_communication_forecast": spec.uses_forecast, "method_policy_version": spec.policy_version,
        "comparison_ready": bool(formal_budget),
        "protocol_sha256": digest(frozen) if frozen else None,
        "formal_budget": formal_budget,
        "early_failure_rule": 'physical_deadline_v1' if early_failure else None,
        "environment_sha256": digest({k: v for k, v in scenario.items() if k not in {"seed", "planner"}}),
        "model_settings": model_settings(), "planner": result["planner"], "seed": scenario.get("seed", 0),
        "scenario_sha256": hashlib.sha256(json.dumps(scenario, sort_keys=True).encode()).hexdigest(),
        "source_sha256": {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(sources)},
        "packages": {name: importlib.metadata.version(name) for name in ["ortools", "fastapi", "httpx", "websockets"]}}
    (Path(output)/"manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    return result
