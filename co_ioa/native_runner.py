"""Isolated Docker entry for the unchanged upstream IoA collaboration policy.

Each run gets a fresh original Server, Milvus, client processes and simulation
gateway. The only host outputs are experiment evidence and non-secret metadata.
"""
import json
import hashlib
import os
from pathlib import Path
import secrets
import subprocess
import tempfile

from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = Path('/root/IoA_upstream')
STACK = ROOT / 'scripts/native_stack.yaml'
IMAGE = 'ioa-upstream-smoke:local'


def _provider(path, *, model=None, embedding_model=None, embedding_dimension=None):
    if path is None or not Path(path).is_file():
        raise ValueError('native IoA requires an existing --env-file with model credentials')
    values = dotenv_values(path)
    key = values.get('CO_IOA_API_KEY') or values.get('OPENAI_API_KEY')
    base = values.get('CO_IOA_API_BASE') or values.get('OPENAI_BASE_URL')
    chat_model = model or values.get('CO_IOA_MODEL') or values.get('IOA_CHAT_MODEL')
    if not key or not base or not chat_model:
        raise ValueError('native IoA needs API key, base URL and chat model; use --native-model if needed')
    embed = embedding_model or values.get('IOA_EMBEDDING_MODEL') or 'GLM-Embedding-3'
    dimension = embedding_dimension or values.get('IOA_EMBEDDING_DIMENSION') or 2048
    if not str(dimension).isdigit() or int(dimension) <= 0:
        raise ValueError('native embedding dimension must be a positive integer')
    env = {'OPENAI_API_KEY': key, 'OPENAI_BASE_URL': base.rstrip('/'),
           'IOA_EMBEDDING_MODEL': embed, 'IOA_EMBEDDING_DIMENSION': str(dimension),
           'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUNBUFFERED': '1',
           'NO_PROXY': 'localhost,127.0.0.1,server,standalone',
           'no_proxy': 'localhost,127.0.0.1,server,standalone'}
    return env, chat_model, float(values.get('CO_IOA_TEMPERATURE') or 0)


def _run(command, *, environment=None, timeout=600, stdout=None):
    result = subprocess.run(command, env=environment, text=True,
                            stdout=stdout if stdout is not None else subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=timeout, check=False)
    if result.returncode:
        detail = (result.stdout or '')[-3000:] if stdout is None else f'see {stdout.name}'
        raise RuntimeError(f"native container command failed ({result.returncode}): {detail}")
    return result


def run_native_experiment(scenario, output, *, env_file, model=None,
                          embedding_model=None, embedding_dimension=None,
                          max_turns=20, wall_wait_seconds=1200,
                          formal_budget=None, protocol_sha256=None, formal_native_config=None,
                          early_failure=False):
    output = Path(output).resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError('native output already contains run files; choose a new directory')
    if max_turns < 1 or wall_wait_seconds <= 0:
        raise ValueError('native turn and wall wait limits must be positive')
    environment, chat_model, temperature = _provider(env_file, model=model,
        embedding_model=embedding_model, embedding_dimension=embedding_dimension)
    if formal_native_config is not None:
        actual = {'model': chat_model, 'temperature': temperature,
                  'embedding_model': environment['IOA_EMBEDDING_MODEL'],
                  'embedding_dimension': int(environment['IOA_EMBEDDING_DIMENSION'])}
        if actual != formal_native_config:
            raise ValueError('native model/embedding settings differ from frozen protocol')
    if formal_budget is not None:
        environment['CO_IOA_FORMAL_PROFILE'] = json.dumps(formal_budget, separators=(',', ':'))
        environment['CO_IOA_FORMAL_BUDGET_PATH'] = '/runtime/model_budget.json'
    if early_failure:
        environment['CO_IOA_EARLY_FAILURE_RULE'] = 'physical_deadline_v1'
    if not (UPSTREAM / 'im_client/simulation/clocked_communication.py').is_file():
        raise ValueError('native upstream adapter is missing')
    _run(['docker', 'image', 'inspect', IMAGE], timeout=30)
    output.mkdir(parents=True, exist_ok=True)
    (output / 'scenario.json').write_text(json.dumps(scenario, ensure_ascii=False, indent=2) + '\n')
    project = 'coioanative' + secrets.token_hex(5)
    with tempfile.TemporaryDirectory(prefix='coioa_native_') as temporary:
        env_path = Path(temporary) / 'provider.env'
        env_path.write_text(''.join(f'{key}={value}\n' for key, value in environment.items()))
        env_path.chmod(0o600)
        compose_env = dict(os.environ, NATIVE_ENV_FILE=str(env_path))
        compose = ['docker', 'compose', '-p', project, '-f', str(STACK)]
        started = False
        try:
            started = True
            _run(compose + ['up', '-d', '--wait', 'server'],
                 environment=compose_env, timeout=600)
            command = ['docker', 'run', '--rm', '--network', project + '_default',
                '--env-file', str(env_path),
                '--mount', f'type=bind,src={output},dst=/runtime',
                '--mount', f'type=bind,src={ROOT / "scripts/native_experiment_worker.py"},dst=/co/native_experiment_worker.py,readonly',
                '--mount', f'type=bind,src={ROOT / "common_environment"},dst=/native_env/common_environment,readonly',
                '--mount', f'type=bind,src={UPSTREAM / "im_client"},dst=/native_client,readonly',
                '-w', '/opt/ioa/client', '--entrypoint', 'python', IMAGE,
                '-B', '/co/native_experiment_worker.py', '--scenario', '/runtime/scenario.json',
                '--output', '/runtime', '--model', chat_model,
                '--temperature', str(temperature), '--max-turns', str(max_turns),
                '--wall-wait-seconds', str(wall_wait_seconds)]
            with (output / 'worker.log').open('w', encoding='utf-8') as log:
                _run(command, timeout=wall_wait_seconds * 3 + 600, stdout=log)
            result_path = output / 'result.json'
            if not result_path.is_file():
                raise RuntimeError('native worker exited without result.json')
            result = json.loads(result_path.read_text())
            scenario_digest = hashlib.sha256(json.dumps(scenario, sort_keys=True)
                .encode('utf-8')).hexdigest()
            (output / 'manifest.json').write_text(json.dumps({
                'method': 'ioa_original', 'model': chat_model,
                'backend': 'ioa', 'planner': 'NativeIoA',
                'scenario_sha256': scenario_digest, 'seed': scenario.get('seed', 0),
                'embedding_model': environment['IOA_EMBEDDING_MODEL'],
                'embedding_dimension': int(environment['IOA_EMBEDDING_DIMENSION']),
                'max_turns': max_turns, 'wall_wait_seconds': wall_wait_seconds,
                'formal_budget': formal_budget, 'protocol_sha256': protocol_sha256,
                'early_failure_rule': 'physical_deadline_v1' if early_failure else None,
                'comparison_ready': formal_budget is not None,
            }, ensure_ascii=False, indent=2) + '\n')
            return result
        finally:
            if started:
                subprocess.run(compose + ['down', '-v', '--remove-orphans'],
                               env=compose_env, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=180, check=False)
