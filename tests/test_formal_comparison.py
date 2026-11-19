"""Formal comparison scheduling and API-budget invariants; no paid model calls."""
import json
import subprocess
from pathlib import Path

import httpx
import pytest

from co_ioa.errors import ModelBudgetError
from co_ioa.formal_analysis import classify
from co_ioa.formal_experiments import build_schedule
from co_ioa.model_api import ModelAPI
from co_ioa.run import load_scenario
from co_ioa.runner import run_experiment


def test_formal_schedule_covers_all_scenes_and_interleaves_methods():
    from co_ioa.comparison_protocol import load_protocol
    protocol = load_protocol('experiments/comparison_formal_v5/protocol.json')
    schedule = build_schedule(protocol)
    assert len(schedule) == 195
    assert len({row['scenario'] for row in schedule}) == 13
    groups = {}
    for row in schedule:
        key = row['scenario'], row['repetition']
        groups.setdefault(key, []).append(row)
        assert row['seed'] == row['repetition']
    assert all({row['method'] for row in rows} == {'co_ioa', 'ioa_original', 'co_ioa_no_forecast'}
               for rows in groups.values())
    assert set(protocol['formal_design']['primary_scenarios']) == set(protocol['scenarios'])
    assert {len(groups_for) for groups_for in
            ([key for key in groups if key[0] == name] for name in protocol['formal_design']['primary_scenarios'])} == {5}
    assert len({tuple(row['method'] for row in rows) for rows in groups.values()}) == 3


def test_formal_model_budget_applies_output_limit_and_counts_retries():
    seen = []
    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(200, json={'choices': [{'message': {'content': '{}'}}]})
    budget = {'max_output_tokens_per_request': 123,
              'max_model_requests_per_run_including_retries': 1,
              'max_model_wall_seconds_per_run': 60}
    environment = {'CO_IOA_API_BASE': 'https://example.test/v1', 'CO_IOA_MODEL': 'test',
                   'CO_IOA_API_KEY': 'secret', 'CO_IOA_MODEL_TIMEOUT': '1'}
    audit = []
    model = ModelAPI(audit, environment=environment, transport=httpx.MockTransport(handler), formal_budget=budget)
    assert model('prompt', {}, '') == '{}'
    assert seen[0]['max_tokens'] == 123
    with pytest.raises(ModelBudgetError, match='request budget'):
        model('prompt', {}, '')
    assert len(seen) == len(audit) == 1
    second = ModelAPI(audit, environment=environment, transport=httpx.MockTransport(handler),
                      formal_budget=budget, budget_state=model.budget_state)
    with pytest.raises(ModelBudgetError, match='request budget'):
        second('prompt', {}, '')


@pytest.mark.parametrize('kind,outcome,expected', [
    (None, 'success', 'success'),
    (None, 'failed', 'failed'),
    (None, 'cancelled', 'cancelled'),
    ('planning_validation', 'error', 'model_invalid'),
    ('budget_exhausted', 'error', 'budget_exhausted'),
    ('model_service', 'error', 'technical_error'),
])
def test_formal_status_categories_are_distinct(kind, outcome, expected):
    result = {'error_kind': kind, 'outcome': outcome, 'external_judgement': {'outcome': outcome}}
    assert classify(result) == expected
    assert classify(None, launcher_failed=True) == 'technical_error'
    assert classify(None) == 'not_run'


def test_formal_co_run_uses_budget_and_marks_comparison_ready(tmp_path, monkeypatch):
    from co_ioa.comparison_protocol import load_protocol
    protocol_path = 'experiments/comparison_formal_v5/protocol.json'
    protocol = load_protocol(protocol_path)
    config = protocol['co_model_configuration']
    for name, value in {
        'CO_IOA_MODEL': config['model'], 'CO_IOA_API_BASE': config['provider_base'],
        'CO_IOA_TEMPERATURE': str(config['temperature']),
        'CO_IOA_MODEL_TIMEOUT': str(config['read_timeout_seconds']),
        'CO_IOA_MODEL_MAX_TIMEOUT': str(config['max_read_timeout_seconds']),
        'CO_IOA_MODEL_RETRIES': str(config['transport_retries']),
        'CO_IOA_MODEL_RETRY_BACKOFF': str(config['retry_backoff_seconds']),
        'CO_IOA_API_KEY': 'test-only-key',
    }.items():
        monkeypatch.setenv(name, value)
    scenario = load_scenario('baseline')
    seen = []
    def handler(request):
        seen.append(json.loads(request.content))
        content = json.dumps({'requirements': scenario['requirements'], 'change_summary': 'initial'})
        return httpx.Response(200, json={'choices': [{'message': {'content': content}}]})
    original_init = ModelAPI.__init__
    def patched_init(self, *args, **kwargs):
        return original_init(self, *args, transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(ModelAPI, '__init__', patched_init)
    result = run_experiment(scenario, tmp_path / 'formal', backend='ioa', planner='llm',
                            protocol=protocol_path)
    manifest = json.loads((tmp_path / 'formal' / 'manifest.json').read_text())
    assert result['comparison_ready'] and manifest['comparison_ready']
    assert result['external_judgement']['outcome'] == 'success'
    assert seen and all(body['max_tokens'] == 8192 for body in seen)


def test_interleaved_launcher_resumes_without_overwriting_runs(tmp_path, monkeypatch):
    from co_ioa import formal_experiments as module
    protocol = {'scenarios': {'baseline': {}}, 'formal_design': {
        'primary_scenarios': ['baseline'], 'primary_repetitions': 2,
        'secondary_repetitions': 1, 'seed_start': 0}}
    protocol_path = tmp_path / 'protocol.json'
    protocol_path.write_text(json.dumps(protocol))
    monkeypatch.setattr(module, 'load_protocol', lambda _path: protocol)
    monkeypatch.setattr(module, '_load_formal_environment', lambda _path: {})
    monkeypatch.setattr(module, 'preflight', lambda _protocol, _environment: None)
    calls = []
    def fake_run(command, **_kwargs):
        destination = tmp_path / 'runs' / 'baseline' / 'repeat_000' / 'co_ioa'
        # The first scheduled method is CO; resume advances to the next method.
        actual = command[command.index('--output') + 1]
        calls.append(actual)
        path = Path(actual)
        assert path != destination or not path.exists()
        path.mkdir()
        (path / 'result.json').write_text(json.dumps({
            'outcome': 'success', 'external_judgement': {'all_required_results_received_at': 5}}))
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(module.subprocess, 'run', fake_run)
    args = ['--protocol', str(protocol_path), '--output', str(tmp_path / 'runs'),
            '--execute', '--max-runs', '1']
    assert module.main(args) == 0
    assert module.main(args) == 0
    assert len(calls) == 2 and calls[0] != calls[1]
    assert (tmp_path / 'runs' / 'schedule.json').is_file()
