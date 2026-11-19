"""Read-only frozen inputs and non-secret configuration fingerprints."""
import hashlib
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROTOCOL = ROOT / "experiments/comparison_formal_v5/protocol.json"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def environment_content(scenario):
    # Seed is paired separately; planner implementation is not environment truth.
    return {k: v for k, v in scenario.items() if k not in {"seed", "planner"}}


def model_settings(environment=None):
    env = os.environ if environment is None else environment
    timeout = float(env.get("CO_IOA_MODEL_TIMEOUT", "120"))
    return {"model": env.get("CO_IOA_MODEL", ""), "provider_base": env.get("CO_IOA_API_BASE", "").rstrip("/"),
            "temperature": float(env.get("CO_IOA_TEMPERATURE", "0")),
            "read_timeout_seconds": timeout,
            "max_read_timeout_seconds": float(env.get("CO_IOA_MODEL_MAX_TIMEOUT", str(timeout * 2))),
            "transport_retries": int(env.get("CO_IOA_MODEL_RETRIES", "2")),
            "retry_backoff_seconds": float(env.get("CO_IOA_MODEL_RETRY_BACKOFF", "1")),
            "max_plan_corrections": 2,
            "output_token_limit": "provider_default", "total_run_call_limit": None}


def load_protocol(path=None):
    protocol = json.loads(Path(path or DEFAULT_PROTOCOL).read_text())
    if protocol.get("schema_version") != 1:
        raise ValueError("unsupported comparison protocol version")
    if protocol.get("protocol_id") not in {"co_ioa_13_stage123_v1", "co_ioa_13_ablation_v2", "co_ioa_13_formal_v1", "co_ioa_13_formal_v2", "co_ioa_13_formal_v3", "co_ioa_13_formal_v4", "co_ioa_13_formal_v5"}:
        raise ValueError("unknown comparison protocol")
    if (protocol.get('protocol_id') in {'co_ioa_13_formal_v3', 'co_ioa_13_formal_v4', 'co_ioa_13_formal_v5'}
            and protocol.get('early_failure_rule') != 'physical_deadline_v1'):
        raise ValueError('formal v3/v4/v5 requires the frozen shared early-failure rule')
    return protocol


def validate_scenario(scenario, protocol):
    name = scenario.get("name")
    expected = protocol["scenarios"].get(name)
    if expected is None or digest(environment_content(scenario)) != expected["environment_sha256"]:
        raise ValueError("scenario differs from frozen comparison environment: " + str(name))


def validate_model_settings(protocol):
    if model_settings() != protocol["co_model_configuration"]:
        raise ValueError("LLM configuration differs from frozen CO model configuration; use the frozen env/settings")


def validate_formal_budget(protocol):
    profile = protocol.get('formal_budget')
    if profile is None:
        return
    settings = protocol['co_model_configuration']
    for field in ('read_timeout_seconds', 'max_read_timeout_seconds',
                  'transport_retries', 'retry_backoff_seconds'):
        if profile.get(field) != settings.get(field):
            raise ValueError('formal transport budget differs from frozen model settings: ' + field)
    for field in ('max_output_tokens_per_request', 'max_model_requests_per_run_including_retries',
                  'max_model_wall_seconds_per_run'):
        if not isinstance(profile.get(field), (int, float)) or profile[field] <= 0:
            raise ValueError('invalid formal model budget: ' + field)


def validate_files(protocol, upstream_root=None):
    mismatches = []
    files = sorted((ROOT / "co_ioa/scenarios").glob("*.json"))
    if {p.stem for p in files} != set(protocol["scenarios"]):
        mismatches.append("scenario file set changed")
    for p in files:
        expected = protocol["scenarios"].get(p.stem, {})
        if hashlib.sha256(p.read_bytes()).hexdigest() != expected.get("file_sha256"):
            mismatches.append("scenario bytes changed: " + p.name)
    upstream = Path(upstream_root or protocol["upstream"]["root"])
    for name, expected in protocol["upstream"]["source_sha256"].items():
        path = upstream / name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            mismatches.append("upstream baseline changed: " + name)
    return mismatches
