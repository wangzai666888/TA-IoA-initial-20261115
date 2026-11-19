"""Preflight and interleave the frozen three-method comparison.

Dry-run is the default. --execute spends model API calls and uses fresh isolated
directories for every scenario/repetition/method. Interrupted runs are retained.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from dotenv import dotenv_values

from .comparison_protocol import load_protocol, model_settings, validate_files, validate_formal_budget


ROOT = Path(__file__).resolve().parents[1]
METHODS = ('co_ioa', 'ioa_original', 'co_ioa_no_forecast')


def build_schedule(protocol):
    design = protocol['formal_design']
    primary = design['primary_scenarios']
    secondary = sorted(set(protocol['scenarios']) - set(primary))
    if len(primary) != len(set(primary)) or set(primary) | set(secondary) != set(protocol['scenarios']):
        raise ValueError('invalid formal scenario selection')
    repetitions = {name: design['primary_repetitions'] for name in primary}
    repetitions.update({name: design['secondary_repetitions'] for name in secondary})
    names = primary + secondary
    schedule = []
    for repetition in range(max(repetitions.values())):
        for index, name in enumerate(names):
            if repetition >= repetitions[name]:
                continue
            rotation = (index + repetition) % len(METHODS)
            for method in METHODS[rotation:] + METHODS[:rotation]:
                schedule.append({'scenario': name, 'repetition': repetition,
                                 'seed': design['seed_start'] + repetition,
                                 'method': method})
    return schedule


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source_tree_hash(root, directories, files=()):
    paths = [path for directory in directories for path in (root / directory).rglob('*.py')]
    paths += [root / name for name in files]
    rows = [(path.relative_to(root).as_posix(), _sha(path)) for path in sorted(set(paths))]
    return hashlib.sha256(json.dumps(rows, separators=(',', ':')).encode()).hexdigest()


def validate_formal_sources(protocol):
    expected = protocol['formal_sources']
    upstream = Path(protocol['upstream']['root'])
    actual = {
        'co_tree_sha256': source_tree_hash(ROOT, ('co_ioa', 'common_environment'),
            ('scripts/native_experiment_worker.py', 'scripts/native_stack.yaml', 'requirements.lock.txt')),
        'upstream_client_sha256': source_tree_hash(upstream, ('im_client',)),
    }
    result = subprocess.run(['docker', 'image', 'inspect', 'ioa-upstream-smoke:local', '--format', '{{.Id}}'],
                            text=True, capture_output=True, check=False)
    actual['docker_image_id'] = result.stdout.strip() if result.returncode == 0 else None
    return {key: {'expected': value, 'actual': actual.get(key)} for key, value in expected.items()
            if actual.get(key) != value}


def _load_formal_environment(path):
    values = dotenv_values(path)
    if not values:
        raise ValueError('formal --env-file is empty or missing')
    environment = os.environ.copy()
    for key, value in values.items():
        if value is not None:
            environment[key] = value
    if not environment.get('CO_IOA_API_KEY'):
        raise ValueError('formal CO_IOA_API_KEY is missing')
    return environment


def preflight(protocol, environment):
    if protocol.get('status') != 'formal_frozen' or 'formal_budget' not in protocol:
        raise ValueError('protocol is not frozen for formal comparison')
    validate_formal_budget(protocol)
    mismatches = validate_files(protocol)
    mismatches += [f'formal source mismatch: {name}' for name in validate_formal_sources(protocol)]
    if model_settings(environment) != protocol['co_model_configuration']:
        mismatches.append('model configuration differs from frozen protocol')
    if mismatches:
        raise ValueError('; '.join(mismatches))


def _output_for(root, row):
    return root / row['scenario'] / f"repeat_{row['repetition']:03d}" / row['method']


def _load_or_write_schedule(root, schedule, protocol):
    root.mkdir(parents=True, exist_ok=True)
    planned = {'protocol_sha256': _sha(protocol), 'schedule': schedule}
    path = root / 'schedule.json'
    if path.exists():
        if json.loads(path.read_text()) != planned:
            raise ValueError('existing output has a different formal schedule or protocol')
    else:
        if any(root.iterdir()):
            raise ValueError('output directory is nonempty without schedule.json')
        path.write_text(json.dumps(planned, indent=2) + '\n')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--protocol', type=Path, default=ROOT / 'experiments/comparison_formal_v5/protocol.json')
    parser.add_argument('--env-file', type=Path, default=ROOT / '.env')
    parser.add_argument('--output', type=Path, default=ROOT / 'runs/formal_comparison_v5')
    parser.add_argument('--execute', action='store_true', help='Make paid model calls; default only checks and previews')
    parser.add_argument('--max-runs', type=int, help='Stop after this many newly executed runs; for staged execution')
    args = parser.parse_args(argv)
    protocol_path = args.protocol.resolve()
    protocol = load_protocol(protocol_path)
    environment = _load_formal_environment(args.env_file.resolve())
    preflight(protocol, environment)
    schedule = build_schedule(protocol)
    print(f"Formal preflight passed; {len(schedule)} planned runs, 13 scenes, 3 methods.", flush=True)
    if not args.execute:
        print('Dry run only. First nine:', json.dumps(schedule[:9], ensure_ascii=False), flush=True)
        return 0
    if args.max_runs is not None and args.max_runs < 1:
        parser.error('--max-runs must be positive')
    root = args.output.resolve()
    _load_or_write_schedule(root, schedule, protocol_path)
    new_count = 0
    for index, row in enumerate(schedule, 1):
        destination = _output_for(root, row)
        result_path = destination / 'result.json'
        if result_path.is_file() or (destination / 'launcher_failure.json').is_file():
            continue
        if destination.exists() and any(destination.iterdir()):
            raise RuntimeError(f'interrupted run preserved at {destination}; inspect before resuming')
        preflight(protocol, environment)
        destination.parent.mkdir(parents=True, exist_ok=True)
        command = [sys.executable, '-B', '-m', 'co_ioa.run', '--method', row['method'],
                   '--backend', 'ioa', '--planner', 'llm', '--scenario', row['scenario'],
                   '--seed', str(row['seed']), '--protocol', str(protocol_path),
                   '--env-file', str(args.env_file.resolve()), '--output', str(destination)]
        print(f"Starting {index}/{len(schedule)}: {row['scenario']}/{row['repetition']} {row['method']}", flush=True)
        launcher_log = destination.parent / (row['method'] + '.launcher.log')
        try:
            with launcher_log.open('w') as log:
                completed = subprocess.run(command, cwd=ROOT, env=environment, stdout=log,
                                           stderr=subprocess.STDOUT, check=False)
            exit_code = completed.returncode
            failure_reason = 'process exited nonzero or without result.json'
        except OSError as exc:
            exit_code = None
            failure_reason = 'process launch failed: ' + type(exc).__name__
        result = None
        if result_path.is_file() and exit_code == 0:
            try:
                result = json.loads(result_path.read_text())
            except (ValueError, OSError):
                failure_reason = 'result.json is unreadable'
        if result is not None:
            print(f"  {result['outcome']}; received_at={result['external_judgement']['all_required_results_received_at']}", flush=True)
        else:
            destination.mkdir(parents=True, exist_ok=True)
            (destination / 'launcher_failure.json').write_text(json.dumps({
                'kind': 'technical_error', 'exit_code': exit_code,
                'reason': failure_reason, 'run': row}, indent=2) + '\n')
            print(f"  technical_error; see {launcher_log}", flush=True)
        new_count += 1
        if args.max_runs is not None and new_count >= args.max_runs:
            break
    print(f'New runs executed: {new_count}; scheduled total: {len(schedule)}', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
