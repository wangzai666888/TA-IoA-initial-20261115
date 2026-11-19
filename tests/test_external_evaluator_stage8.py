"""Shared judge rejects unsupported success claims and uses physical evidence."""
import asyncio
import json

import httpx

from common_environment.evaluator import evaluate_native
from common_environment.native_clock import NativeSimulationClock
from common_environment.native_clock_driver import NativeClockDriver
from common_environment.native_tool import NativeSimulationTools
from common_environment.native_tool_api import create_app
from common_environment.native_transport import NativeWindowRouter
from co_ioa.event_loop import Simulation
from co_ioa.external_evidence import evaluate_co
from co_ioa.scenarios import suite
from test_native_tool_stage4 import scene


def native_rig(scenario):
    environment = NativeSimulationTools(scenario)
    clock = NativeSimulationClock(environment)
    frames = []

    async def deliver(receiver, frame):
        frames.append((receiver, frame))

    async def forward(_operation, _body):
        raise AssertionError('unexpected Server business request')

    router = NativeWindowRouter(environment, clock=clock, forward_http=forward,
                                deliver_ws=deliver)
    clock.router = router
    router.sessions['team'] = ['USV-1', 'AUV-1']
    router.chat_records['team'] = {'chat_record': []}
    return environment, clock, router, frames


def test_native_physical_completion_needs_matching_result_and_collector_ack():
    async def check():
        environment, clock, router, frames = native_rig(scene())
        environment.advance(180)
        environment.submit(agent_id='AUV-1', task_id='inspect', assignment_id='i1')
        environment.advance(540)
        result = environment.status(agent_id='AUV-1', assignment_id='i1')
        assert evaluate_native(environment, router, clock)['tasks']['inspect']['status'] == 'unreported'
        forged = dict(result, result=dict(result['result'], receipt='forged'))
        for index, payload in enumerate((forged, result)):
            router.enqueue_group('AUV-1', {'sender': 'AUV-1', 'comm_id': 'team',
                'type': 7, 'task_conclusion': json.dumps(dict(
                    status='COMPLETED', task_id='inspect', assignment_id='i1',
                    actual_end_time=540, result=payload['result']))})
        await router.settle(540)
        for receiver, frame in frames:
            if receiver == 'AUV-1':
                router.ack(receiver, frame['_sim_delivery_id'])
        assert evaluate_native(environment, router, clock)['outcome'] == 'incomplete'
        environment.advance(660)
        await router.settle(660)
        assert evaluate_native(environment, router, clock)['outcome'] == 'incomplete'
        usv = [frame for receiver, frame in frames if receiver == 'USV-1']
        assert len(usv) == 2
        router.ack('USV-1', usv[0]['_sim_delivery_id'])
        assert evaluate_native(environment, router, clock)['outcome'] == 'incomplete'
        router.ack('USV-1', usv[1]['_sim_delivery_id'])
        judged = evaluate_native(environment, router, clock)
        assert judged['outcome'] == 'success'
        assert judged['tasks']['inspect']['physical_completed_at'] == 540
        assert judged['all_required_results_received_at'] == 660
    asyncio.run(check())


def test_native_driver_stops_at_acknowledged_external_success():
    async def check():
        environment = NativeSimulationTools(scene())
        clock = NativeSimulationClock(environment)
        router = None

        async def deliver(receiver, frame):
            router.ack(receiver, frame['_sim_delivery_id'])

        async def forward(_operation, _body):
            raise AssertionError('unexpected Server business request')

        router = NativeWindowRouter(environment, clock=clock,
            forward_http=forward, deliver_ws=deliver)
        clock.router = router
        router.sessions['team'] = ['USV-1', 'AUV-1']
        router.chat_records['team'] = {'chat_record': []}
        environment.advance(180)
        environment.submit(agent_id='AUV-1', task_id='inspect', assignment_id='one')
        environment.advance(540)
        completed = environment.status(agent_id='AUV-1', assignment_id='one')
        router.enqueue_group('AUV-1', {'sender': 'AUV-1', 'comm_id': 'team',
            'type': 7, 'task_conclusion': json.dumps(dict(
                status='COMPLETED', task_id='inspect', assignment_id='one',
                actual_end_time=540, result=completed['result']))})

        async def status():
            return clock.snapshot()

        async def step():
            return clock.advance()

        driver = NativeClockDriver(status=status, step=step, settle=router.settle,
            stop_when=lambda _state: evaluate_native(environment, router, clock)['outcome'] == 'success')
        state = await driver.run_until(1150)
        assert state['sim_time'] == 660
        assert evaluate_native(environment, router, clock)['all_required_results_received_at'] == 660
    asyncio.run(check())


def test_same_judge_reads_co_execution_and_actual_received_message():
    simulation = Simulation(scene('baseline'))
    original = simulation.run()
    judgement = evaluate_co(simulation)
    assert original['outcome'] == judgement['outcome'] == 'success'
    assert judgement['tasks']['inspect']['physical_completed_at'] == 540
    assert judgement['tasks']['inspect']['result_received_at'] == 660
    coordinator = simulation.agents['USV-1']
    result_message = next(row for row in coordinator.store.messages if row['message_type'] == 'TASK_RESULT')
    result_message['payload']['result']['executor_id'] = 'forged'
    assert evaluate_co(simulation)['outcome'] == 'failed'


def test_revisions_cancel_and_unknown_result_are_distinct():
    revised = Simulation(scene('authorized_revision'))
    revised.run()
    judged = evaluate_co(revised)
    assert judged['outcome'] == 'success' and judged['tasks']['inspect']['deadline'] == 1200
    cancelled = Simulation(scene('cancel_pending_cleanup'))
    cancelled.run()
    assert evaluate_co(cancelled)['outcome'] == 'cancelled'
    lost = Simulation(scene('lost_ack_result'))
    lost.run()
    result = evaluate_co(lost)
    assert result['outcome'] == 'failed'
    assert result['tasks']['inspect']['status'] == 'unreported'
    assert result['on_time_execution_completion_rate'] == 1.0


def test_lost_result_blocks_both_possible_executors_without_blocking_execution():
    async def check(executor, start, finish):
        scenario = scene('lost_ack_result')
        assert suite()['lost_ack_result'] == scenario
        assert {agent['id'] for agent in scenario['agents']
                if 'sonar' in agent.get('capabilities', [])} == {'AUV-1', 'AUV-2'}
        assert not [window for window in scenario['windows']
                    if window['sender_id'] in {'AUV-1', 'AUV-2'}
                    and window['receiver_id'] == 'USV-1']
        environment, clock, router, frames = native_rig(scenario)
        router.sessions['team'] = ['USV-1', executor]
        environment.advance(start)
        accepted = environment.submit(agent_id=executor, task_id='inspect',
                                      assignment_id='one')
        assert accepted['status'] == 'RUNNING'
        environment.advance(finish)
        completed = environment.status(agent_id=executor, assignment_id='one')
        assert completed['status'] == 'COMPLETED'
        router.enqueue_group(executor, {'sender': executor, 'comm_id': 'team',
            'type': 7, 'task_conclusion': json.dumps(dict(
                status='COMPLETED', task_id='inspect', assignment_id='one',
                actual_end_time=finish, result=completed['result']))})
        await router.settle(finish)
        for receiver, frame in frames:
            assert receiver == executor
            router.ack(receiver, frame['_sim_delivery_id'])
        assert any(row.kind == 'group_uplink' and row.status == 'QUEUED'
                   for row in router.transits)
        environment.advance(scenario['max_time'])
        judgement = evaluate_native(environment, router, clock)
        assert judgement['outcome'] == 'failed'
        assert judgement['on_time_execution_completion_rate'] == 1.0
        assert judgement['tasks']['inspect']['physical_completed_at'] == finish
        assert judgement['tasks']['inspect']['result_received_at'] is None
    asyncio.run(check('AUV-1', 180, 540))
    asyncio.run(check('AUV-2', 300, 660))


def test_objective_event_updates_native_physical_catalog_without_agent_truth_leak():
    async def check():
        environment, clock, router, frames = native_rig(scene('objective_revision_parallel'))
        assert 'extra' not in environment.requirements
        clock.advance(120)
        assert 'extra' in environment.requirements
        await router.settle(120)
        assert [receiver for receiver, _ in frames] == ['UAV-1']
        assert all(row.kind != 'group' for row in router.transits)
        environment.submit(agent_id='USV-1', task_id='extra', assignment_id='e1')
        assert environment.status(agent_id='USV-1', assignment_id='e1')['status'] == 'RUNNING'
    asyncio.run(check())


def test_native_revision_is_scenario_authorized_and_result_uses_revised_deadline():
    environment, clock, router, _ = native_rig(scene('authorized_revision'))
    environment.advance(720)
    denied = environment.submit(agent_id='AUV-1', task_id='inspect', assignment_id='old')
    assert denied['status'] == 'REJECTED'
    revised = environment.submit(agent_id='AUV-1', task_id='inspect',
                                 assignment_id='new', revision_index=0)
    assert revised['status'] == 'RUNNING'
    assert revised['scheduled_end'] == 1080
    environment.advance(1080)
    assert environment.status(agent_id='AUV-1', assignment_id='new')['status'] == 'COMPLETED'


def test_judge_api_requires_driver_token():
    async def check():
        environment, clock, router, _ = native_rig(scene())
        app = create_app(environment, agent_tokens={'AUV-1': 'agent-secret'},
                         control_token='driver-secret', clock=clock, router=router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
            agent = await client.get('http://sim/evaluation',
                                     headers={'Authorization': 'Bearer agent-secret'})
            driver = await client.get('http://sim/evaluation',
                                      headers={'Authorization': 'Bearer driver-secret'})
            assert agent.status_code == 403
            assert driver.status_code == 200
            assert driver.json()['outcome'] == 'incomplete'
    asyncio.run(check())
