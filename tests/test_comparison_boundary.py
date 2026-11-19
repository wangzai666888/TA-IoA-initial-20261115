import ast
import json
from pathlib import Path
from unittest.mock import patch
import pytest
from common_environment.contracts import initial_observation
from common_environment.execution import assess_execution, completion_at, cancellation_status
from co_ioa.comparison_protocol import load_protocol, validate_files, validate_scenario, validate_model_settings
from co_ioa.event_loop import Simulation
from co_ioa.methods import NoForecastSchedule, AdapterNotReadyError
from co_ioa.run import load_scenario
from co_ioa.runner import run_experiment
from co_ioa.matcher import Matcher

ROOT = Path(__file__).resolve().parents[1]


def test_all_frozen_inputs_unchanged():
    assert validate_files(load_protocol()) == []


def test_frozen_scenario_rejects_physics_edit_but_accepts_paired_seed():
    s = load_scenario('baseline'); s['seed'] = 42; s['planner'] = 'llm'
    validate_scenario(s, load_protocol())
    s['windows'][0]['end'] += 1
    with pytest.raises(ValueError, match='frozen'):
        validate_scenario(s, load_protocol())


def test_native_entry_never_falls_back_to_co(tmp_path):
    expected = {'method': 'ioa_original', 'outcome': 'incomplete'}
    with patch('co_ioa.runner.Simulation', side_effect=AssertionError('must not construct CO')):
        with patch('co_ioa.native_runner.run_native_experiment', return_value=expected) as native:
            actual = run_experiment(load_scenario('baseline'), tmp_path/'original',
                method='ioa_original', backend='ioa', planner='llm',
                native_env_file=tmp_path/'provider.env')
    assert actual is expected
    assert native.call_args.kwargs['env_file'] == tmp_path/'provider.env'
    assert native.call_args.args[0]['name'] == 'baseline'
    with pytest.raises(AdapterNotReadyError):
        Simulation(load_scenario('baseline'), method='ioa_original')


def test_native_run_rejects_interrupted_output_without_erasing_evidence(tmp_path):
    from co_ioa.native_runner import run_native_experiment
    old = tmp_path / 'interrupted'
    old.mkdir()
    evidence = old / 'worker.log'
    evidence.write_text('partial run evidence')
    with pytest.raises(ValueError, match='already contains run files'):
        run_native_experiment(load_scenario('baseline'), old, env_file=None)
    assert evidence.read_text() == 'partial run evidence'


def test_common_package_does_not_import_strategy():
    for p in (ROOT/'common_environment').glob('*.py'):
        for node in ast.walk(ast.parse(p.read_text())):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or '').startswith(('co_ioa', 'im_client', 'im_server', 'ortools'))
            if isinstance(node, ast.Import):
                assert not any(x.name.startswith(('co_ioa', 'im_client', 'im_server', 'ortools')) for x in node.names)


def test_observation_projection_has_no_future_events_or_truth_reference():
    s = load_scenario('delayed_contact')
    view = initial_observation(s, 'AUV-1', include_forecast=False)
    assert not {'events','windows','predicted_windows','requirements','initial_registry','agents'} & set(view)
    view['self']['capabilities'].append('invented')
    assert 'invented' not in next(a for a in s['agents'] if a['id']=='AUV-1')['capabilities']
    server = initial_observation(s, s['server_id'], include_forecast=True)
    assert 'initial_registry' in server and 'events' not in server


def test_no_forecast_in_local_and_server_matching_and_truth_still_enforced():
    calls = []
    original = Matcher.solve
    def spy(self, requirements, capabilities, schedule, ledger, **kwargs):
        calls.append(schedule)
        assert isinstance(schedule, NoForecastSchedule)
        return original(self, requirements, capabilities, schedule, ledger, **kwargs)
    s = load_scenario('delayed_contact')
    with patch.object(Matcher, 'solve', spy):
        sim = Simulation(s, method='co_ioa_no_forecast'); sim.run()
    assert calls
    kinds = {r['event_type'] for r in sim.events}
    assert {'local_matching','server_matching','forecast_ignored_by_ablation'} <= kinds
    assert all(isinstance(a.known_contacts, NoForecastSchedule) for a in sim.agents.values())
    assert sim.communication.truth.windows
    # No cross-node physical send is possible merely because the planner assumed availability.
    for r in sim.events:
        if r['event_type']=='message_sent' and r['sender_id'] != r['receiver_id']:
            assert any(w.sender_id == r['sender_id'] and w.receiver_id == r['receiver_id']
                       and w.start <= r['sim_time'] < w.end for w in sim.communication.truth.windows)


def test_ablation_does_not_read_initial_predictions():
    s = load_scenario('baseline'); s['perfect_prediction'] = False; s['predicted_windows'] = []
    first = Simulation(s, method='co_ioa_no_forecast'); first_result = first.run()
    # Impossible initial predictions must not affect a strategy that does not use forecasts.
    s['predicted_windows'] = [{"sender_id":"USV-1","receiver_id":"AUV-2","start":1100,"end":1120}]
    second = Simulation(s, method='co_ioa_no_forecast'); second_result = second.run()
    assert first_result == second_result
    assert first.events == second.events


def admission(**kwargs):
    values=dict(now=10, requested_start=0, duration=20, execution_delay=0, deadline=50,
                valid_until=None, capable=True, inputs_available=True, dependencies=[], inputs={}, busy_intervals=[])
    values.update(kwargs)
    return assess_execution(**values)


def test_common_physical_admission_and_lifecycle():
    assert (admission().start, admission().end) == (10,30)
    assert admission(earliest_start=20).start == 20
    assert admission(now=40).timing_reason == 'late_delivery_or_execution_deadline'
    assert admission(busy_intervals=[(20,40)]).timing_reason == 'local_resource_conflict'
    assert admission(busy_intervals=[(30,40)]).timing_reason is None
    assert admission(dependencies=['a']).eligibility_reason == 'missing_dependency_inputs'
    assert admission(capable=False).eligibility_reason == 'NO_CAPABILITY_MATCH'
    assert completion_at(now=29,scheduled_end=30,valid_until=None) == ('RUNNING',None)
    assert completion_at(now=30,scheduled_end=30,valid_until=30) == ('COMPLETED',None)
    assert completion_at(now=25,scheduled_end=30,valid_until=25) == ('FAILED','valid_until')
    assert cancellation_status('RUNNING',True) == ('RUNNING',False)
    assert cancellation_status('RUNNING',False) == ('CANCELLED',True)


def test_runnable_methods_share_one_environment_hash(tmp_path):
    s=load_scenario('baseline')
    for name in ('co_ioa','co_ioa_no_forecast'):
        result=run_experiment(s,tmp_path/name,backend='offline',method=name)
        assert result['method']==name and result['comparison_ready'] is False
    manifests=[json.loads((tmp_path/n/'manifest.json').read_text()) for n in ('co_ioa','co_ioa_no_forecast')]
    assert manifests[0]['environment_sha256']==manifests[1]['environment_sha256']
    assert manifests[0]['scenario_sha256']==manifests[1]['scenario_sha256']
    assert manifests[0]['uses_communication_forecast'] is True
    assert manifests[1]['uses_communication_forecast'] is False


def test_model_drift_is_rejected(monkeypatch):
    monkeypatch.setenv('CO_IOA_MODEL','different-model')
    with pytest.raises(ValueError,match='configuration differs'):
        validate_model_settings(load_protocol())


def test_method_output_cannot_overwrite_another_method(tmp_path):
    run_experiment(load_scenario('baseline'), tmp_path, backend='offline')
    before = (tmp_path/'result.json').read_bytes()
    with pytest.raises(ValueError, match='another method'):
        run_experiment(load_scenario('baseline'), tmp_path, backend='offline', method='co_ioa_no_forecast')
    assert (tmp_path/'result.json').read_bytes() == before


def test_no_forecast_candidate_tie_rotates_with_seed_without_window_truth():
    chosen = []
    for seed in (0, 1):
        scenario = load_scenario('baseline')
        scenario['seed'] = seed
        sim = Simulation(scenario, method='co_ioa_no_forecast')
        sim.run()
        assignments = [row for row in sim.events if row['event_type'] == 'assignment_committed'
                       and row['task_id'] == 'inspect']
        chosen.append(assignments[0]['executor_id'])
    assert chosen == ['AUV-1', 'AUV-2']


def test_delayed_link_expires_without_forecast_and_reacts_to_local_failure():
    sim = Simulation(load_scenario('delayed_contact'), method='co_ioa_no_forecast')
    result = sim.run()
    assert result['outcome'] == 'failed' and result['ended_at'] == 541
    assert not [x for x in sim.events if x['event_type'] == 'task_started']
    expired = [x for x in sim.events if x['event_type'] == 'message_expired'
               and x.get('message_type') == 'TASK_ASSIGN']
    observed = [x for x in sim.events if x['event_type'] == 'dispatch_expiration_observed']
    assert len(expired) == len(observed) == 1
    assert expired[0]['sim_time'] == observed[0]['sim_time'] == 541
    assert not result['unreleased_reservations']


def test_authorized_revision_uses_only_expired_dispatch_feedback():
    sim = Simulation(load_scenario('authorized_revision'), method='co_ioa_no_forecast')
    result = sim.run()
    assert result['outcome'] == 'success' and result['ended_at'] == 1080
    assert [x['sim_time'] for x in sim.events if x['event_type'] == 'dispatch_expiration_observed'] == [541]
    plans = [x for x in sim.events if x['event_type'] == 'plan_validated']
    assert len(plans) == 2 and plans[1]['sim_time'] == 541
    assert result['tasks']['inspect']['requirement']['deadline'] == 1200
    starts = [x for x in sim.events if x['event_type'] == 'task_started']
    assert [(x['agent_id'], x['sim_time']) for x in starts] == [('AUV-1', 720)]
    assert not result['unreleased_reservations']


def test_missing_reverse_link_is_not_treated_as_execution_failure():
    sim = Simulation(load_scenario('lost_ack_result'), method='co_ioa_no_forecast')
    result = sim.run()
    assert result['outcome'] == 'failed' and result['ended_at'] == 1150
    assert [(x['agent_id'], x['sim_time']) for x in sim.events if x['event_type']=='task_completed'] == [('AUV-1', 540)]
    assert not [x for x in sim.events if x['event_type']=='dispatch_expiration_observed']
    assert result['tasks']['inspect']['result_received_at'] is None
    assert len([x for x in sim.events if x['event_type']=='task_started']) == 1


def test_disconnect_after_acceptance_preserves_running_work():
    sim = Simulation(load_scenario('accepted_then_disconnected'), method='co_ioa_no_forecast')
    result = sim.run()
    assert result['outcome']=='success' and result['ended_at']==660
    assert [(x['agent_id'],x['sim_time']) for x in sim.events if x['event_type']=='task_started'] == [('AUV-1',180)]
    assert [(x['agent_id'],x['sim_time']) for x in sim.events if x['event_type']=='task_completed'] == [('AUV-1',540)]
    assert not [x for x in sim.events if x['event_type']=='dispatch_expiration_observed']


def test_tie_break_cannot_override_earlier_finish_or_retention():
    from co_ioa.capability_catalog import CapabilityCatalog
    from co_ioa.models import TaskAssignment, TaskRequirement
    from co_ioa.reservation_ledger import ReservationLedger
    from co_ioa.methods import NoForecastSchedule
    catalog = CapabilityCatalog({'sonar': {'duration': 360}})
    matcher = Matcher(catalog, tie_break_seed=0)
    task = TaskRequirement('inspect', 'sonar', 0, 900, 360)
    capabilities = {'AUV-1': ['sonar'], 'AUV-2': ['sonar']}
    busy = ReservationLedger()
    busy.reserve('mission', [TaskAssignment('busy', 'other', 'AUV-1', 'USV-1', 0, 0, 200, 1, 1)])
    result = matcher.solve({'inspect': task}, capabilities, NoForecastSchedule(), busy,
                           now=0, dispatch_agent_id='USV-1', collaboration_id='mission')
    assert result.assignments[0].executor_id == 'AUV-2'  # Finishes 200 seconds sooner.
    empty = ReservationLedger()
    prior = TaskAssignment('old', 'inspect', 'AUV-2', 'USV-1', 0, 0, 360, 1, 1)
    retained = matcher.solve({'inspect': task}, capabilities, NoForecastSchedule(), empty,
                             now=0, dispatch_agent_id='USV-1', previous=[prior])
    assert retained.assignments[0].executor_id == 'AUV-2'


def test_previous_ablation_protocol_cannot_be_relabelled_v2(tmp_path):
    from co_ioa.comparison_protocol import load_protocol
    from co_ioa.runner import run_experiment
    from pathlib import Path
    old = ROOT/'experiments/comparison_v1/protocol.json'
    new = ROOT/'experiments/comparison_v2/protocol.json'
    assert load_protocol(old)['protocol_id'] != load_protocol(new)['protocol_id']
    with pytest.raises(ValueError, match='ablation policy differs'):
        run_experiment(load_scenario('baseline'), tmp_path/'old', backend='offline',
                       method='co_ioa_no_forecast', protocol=old)
    result = run_experiment(load_scenario('baseline'), tmp_path/'new', backend='offline',
                            method='co_ioa_no_forecast', protocol=new)
    assert result['method_policy_version'] == 'v2'


def test_v1_output_cannot_be_overwritten_by_v2(tmp_path):
    output = tmp_path/'v1_output'
    output.mkdir()
    legacy = {'method': 'co_ioa_no_forecast', 'outcome': 'success'}
    (output/'result.json').write_text(json.dumps(legacy))
    with pytest.raises(ValueError, match='another policy version'):
        run_experiment(load_scenario('baseline'), output, backend='offline',
                       method='co_ioa_no_forecast',
                       protocol=ROOT/'experiments/comparison_v2/protocol.json')
    assert json.loads((output/'result.json').read_text()) == legacy
