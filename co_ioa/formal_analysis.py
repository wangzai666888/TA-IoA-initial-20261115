"""Apply the frozen outcome definitions to every scheduled formal run."""
import argparse
import json
from pathlib import Path


def classify(result, *, launcher_failed=False):
    if launcher_failed:
        return 'technical_error'
    if result is None:
        return 'not_run'
    kind = result.get('error_kind')
    if kind == 'budget_exhausted':
        return 'budget_exhausted'
    if kind == 'planning_validation':
        return 'model_invalid'
    if result.get('outcome') == 'error' or kind in {'technical', 'model_service'}:
        return 'technical_error'
    outcome = result.get('external_judgement', {}).get('outcome')
    if outcome in {'success', 'failed', 'cancelled', 'incomplete'}:
        return outcome
    return 'technical_error'


def _reported_tokens(result, path):
    if result is None:
        return None
    if isinstance(result.get('model_calls'), list):
        usages = [row['usage'] for row in result['model_calls'] if isinstance(row.get('usage'), dict)]
    else:
        usages = []
        for response in (path / 'model_calls').rglob('openai_response_*.json'):
            try:
                usage = json.loads(response.read_text()).get('response', {}).get('usage')
                if isinstance(usage, dict):
                    usages.append(usage)
            except (OSError, ValueError):
                pass
    return {key: sum(row.get(key, 0) or 0 for row in usages)
            for key in ('prompt_tokens', 'completion_tokens', 'total_tokens')} if usages else None


def collect(root):
    root = Path(root)
    schedule = json.loads((root / 'schedule.json').read_text())['schedule']
    rows = []
    for item in schedule:
        path = root / item['scenario'] / f"repeat_{item['repetition']:03d}" / item['method']
        result_path = path / 'result.json'
        result = json.loads(result_path.read_text()) if result_path.is_file() else None
        manifest_path = path / 'manifest.json'
        manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else {}
        judge = result.get('external_judgement', {}) if result else {}
        category = classify(result, launcher_failed=(path / 'launcher_failure.json').is_file())
        tasks = judge.get('tasks', {})
        unknown_remote = bool(judge.get('pending_remote_work')) or any(
            row.get('status') == 'unreported' for row in tasks.values())
        rows.append({**item, 'category': category,
                     'raw_outcome': result.get('outcome') if result else None,
                     'error_kind': result.get('error_kind') if result else None,
                     'reason': result.get('reason') if result else None,
                     'evaluation_valid': result.get('evaluation_valid') if result else None,
                     'behavioral_denominator': category not in {'not_run', 'technical_error'},
                     'mission_success': category == 'success',
                     'on_time_execution_completion_rate': judge.get('on_time_execution_completion_rate'),
                     'all_required_results_received_at': (
                         judge.get('all_required_results_received_at') if category == 'success' else None),
                     'evaluated_until': judge.get('evaluated_until'),
                     'terminal_certificate': judge.get('terminal_certificate'),
                     'unknown_remote_work': unknown_remote,
                     'model_attempts': (len(result['model_calls']) if result and isinstance(result.get('model_calls'), list)
                                        else result.get('calls', {}).get('model_calls') if result else None),
                     'reported_tokens': _reported_tokens(result, path),
                     'messages': result.get('messages') if result else None,
                     'scenario_sha256': manifest.get('scenario_sha256'),
                     'protocol_sha256': manifest.get('protocol_sha256'),
                     'run_directory': str(path)})
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='Formal run root containing schedule.json')
    args = parser.parse_args(argv)
    rows = collect(args.output)
    by_method = {}
    for row in rows:
        counts = by_method.setdefault(row['method'], {})
        counts[row['category']] = counts.get(row['category'], 0) + 1
    path = args.output / 'formal_rows.jsonl'
    path.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows))
    print(json.dumps({'planned': len(rows), 'by_method': by_method,
                      'rows': str(path)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
