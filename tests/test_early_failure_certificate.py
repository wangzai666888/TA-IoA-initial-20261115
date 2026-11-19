"""A terminal failure must follow physical evidence, never a policy guess."""
import asyncio
from types import SimpleNamespace

from common_environment.evaluator import evaluate, evaluate_native
from common_environment.native_clock import NativeSimulationClock
from common_environment.native_clock_driver import NativeClockDriver
from common_environment.native_tool import NativeSimulationTools
from co_ioa.event_loop import Simulation
from co_ioa.external_evidence import evaluate_co
from co_ioa.run import load_scenario


def judge(scenario, now, *, physical=(), deliveries=(), fault=None):
    return evaluate(scenario, physical=physical, deliveries=deliveries,
                    evaluated_until=now, technical_fault=fault,
                    allow_early_failure=True)


def test_delayed_contact_is_proven_impossible_only_after_latest_start():
    scenario = load_scenario('delayed_contact')
    assert judge(scenario, 540)['outcome'] == 'incomplete'
    verdict = judge(scenario, 541)
    assert verdict['outcome'] == 'failed'
    assert verdict['ended_at'] is None
    assert verdict['evaluated_until'] == 541
    assert verdict['terminal_certificate']['impossible_tasks'] == [
        {'task_id': 'inspect', 'deadline': 900,
         'minimum_duration': 360, 'latest_possible_start': 540}]
    assert judge(scenario, 660)['terminal_certificate']['certified_at'] == 660
    assert evaluate(scenario, physical=[], deliveries=[], evaluated_until=660)['outcome'] == 'incomplete'


def test_active_work_and_completed_unreported_work_must_remain_open():
    scenario = load_scenario('delayed_contact')
    active = {'task_id': 'inspect', 'assignment_id': 'a', 'executor_id': 'AUV-1',
              'status': 'RUNNING', 'accepted_at': 180,
              'requirement_snapshot': scenario['requirements'][0],
              'actual_end_time': None, 'result': None}
    assert judge(scenario, 660, physical=[active])['outcome'] == 'incomplete'
    completed = dict(active, status='COMPLETED', actual_end_time=540,
                     result={'receipt': 'real'})
    verdict = judge(scenario, 660, physical=[completed])
    assert verdict['outcome'] == 'incomplete'
    assert verdict['tasks']['inspect']['status'] == 'unreported'
    received = {'kind': 'TASK_RESULT', 'task_id': 'inspect', 'assignment_id': 'a',
                'sender_id': 'AUV-1', 'receiver_id': 'USV-1', 'received_at': 660,
                'created_at': 540, 'actual_end_time': 540,
                'result': {'receipt': 'real'}}
    assert judge(scenario, 660, physical=[completed], deliveries=[received])['outcome'] == 'success'


def test_future_objective_and_authorized_revision_block_certificate():
    scenario = load_scenario('delayed_contact')
    scenario['events'].append({'time': 700, 'type': 'objective_update',
                               'payload': {'cancel': True}})
    assert judge(scenario, 660)['outcome'] == 'incomplete'
    scenario['events'].pop()
    scenario['adjustment_policy']['allow_revision'] = True
    assert judge(scenario, 660)['outcome'] == 'incomplete'
    scenario['adjustment_policy']['allow_revision'] = False
    assert judge(scenario, 660, fault={'reason': 'API failure'})['outcome'] == 'error'


def test_all_required_task_metrics_must_be_fixed():
    scenario = load_scenario('dependency')
    # Analyse can still start at t=660 and finish before its deadline.
    assert judge(scenario, 660)['outcome'] == 'incomplete'
    assert judge(scenario, 871)['outcome'] == 'failed'


def test_native_driver_stops_on_proof_even_with_an_active_decision():
    async def check():
        environment = NativeSimulationTools(load_scenario('delayed_contact'))
        clock = NativeSimulationClock(environment)
        environment.advance(660)
        clock.begin('USV-1', 'slow-model-decision')
        router = SimpleNamespace(events=[], transits=[])

        async def status():
            return clock.snapshot()

        async def step():
            raise AssertionError('an impossible mission must not wait for model decisions')

        driver = NativeClockDriver(status=status, step=step, settle=lambda _now: None,
            stop_when=lambda _state: evaluate_native(environment, router, clock,
                allow_early_failure=True)['terminal_certificate'] is not None)
        state = await asyncio.wait_for(driver.run_until(1150), 1)
        assert state['sim_time'] == 660 and state['active_decisions'] == 1
    asyncio.run(check())


def test_both_co_methods_use_the_same_certificate_and_preserve_legacy_runs():
    scenario = load_scenario('hard_deadline_failure')
    for method in ('co_ioa', 'co_ioa_no_forecast'):
        simulation = Simulation(scenario, method=method, early_failure=True)
        simulation.event_driven = True
        assert simulation.run()['evaluated_until'] == 541
        verdict = evaluate_co(simulation, allow_early_failure=True)
        assert verdict['outcome'] == 'failed'
        assert verdict['terminal_certificate']['certified_at'] == 541
    legacy = Simulation(scenario, method='co_ioa')
    legacy.event_driven = True
    assert legacy.run()['evaluated_until'] == scenario['max_time']


def test_native_clock_schedules_proof_boundary_without_exposing_it_to_agents():
    scenario = load_scenario('hard_deadline_failure')
    environment = NativeSimulationTools(scenario)
    clock = NativeSimulationClock(environment, early_failure=True)
    assert clock.next_boundary() == 541
    assert NativeSimulationClock(environment).next_boundary() == 660
    assert clock.advance()['sim_time'] == 541
    router = SimpleNamespace(events=[], transits=[])
    assert evaluate_native(environment, router, clock,
                           allow_early_failure=True)['terminal_certificate']['certified_at'] == 541
