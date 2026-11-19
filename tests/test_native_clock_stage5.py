"""Stage 5 clock bridge: wall time is not simulation time; tool waits are."""
import asyncio
import importlib.util
import json
from pathlib import Path

import httpx
import pytest

from common_environment.native_clock import ClockConflict, NativeSimulationClock
from common_environment.native_tool import NativeSimulationTools
from common_environment.native_tool_api import create_app
from test_native_tool_stage4 import native_agent_class, request, scene

UPSTREAM_BRIDGE = Path('/root/IoA_upstream/im_client/simulation/clock_bridge.py')


def bridge_class():
    spec = importlib.util.spec_from_file_location('stage5_clock_bridge', UPSTREAM_BRIDGE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.NativeClockBridge


def test_decision_lease_freezes_wall_time_and_boundaries():
    environment = NativeSimulationTools(scene('delayed_contact'))
    clock = NativeSimulationClock(environment)
    clock.begin('UAV-1', 'planning')
    with pytest.raises(ClockConflict, match='decision still active'):
        clock.advance()
    assert clock.now == 0
    clock.end('UAV-1', 'planning')
    assert clock.next_boundary() == 120
    with pytest.raises(ClockConflict, match='cannot skip'):
        clock.advance(180)
    assert clock.advance()['sim_time'] == 120
    assert clock.next_boundary() == 180
    assert clock.advance()['sim_time'] == 180


def test_teamup_handoff_keeps_same_second_until_discussion_starts():
    async def check():
        environment = NativeSimulationTools(scene())
        clock = NativeSimulationClock(environment)
        app = create_app(environment, agent_tokens={'UAV-1': 'agent-secret'},
                         control_token='driver-secret', clock=clock)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
            bridge = bridge_class()(agent_id='UAV-1', base_url='http://sim',
                                    token='agent-secret', client=client)
            async with bridge.activity():
                # Native team_up opens a continuation lease before returning.
                handoff = bridge.activity()
                await handoff.__aenter__()
            assert clock.snapshot()['active_decisions'] == 1
            with pytest.raises(ClockConflict, match='decision still active'):
                clock.advance()
            async with bridge.activity():
                # Coordination registers before releasing the handoff lease.
                await handoff.__aexit__(None, None, None)
                assert clock.snapshot()['active_decisions'] == 1
                with pytest.raises(ClockConflict, match='decision still active'):
                    clock.advance()
            assert clock.snapshot()['active_decisions'] == 0
            assert clock.advance()['sim_time'] == 180
    asyncio.run(check())


def test_wait_requires_admitted_work_and_wake_barrier():
    environment = NativeSimulationTools(scene())
    clock = NativeSimulationClock(environment)
    clock.advance(180)
    clock.begin('AUV-1', 'work')
    with pytest.raises(KeyError):
        clock.wait_for_tool('AUV-1', 'work', 'missing')
    environment.submit(agent_id='AUV-1', task_id='inspect', assignment_id='inspect-1')
    clock.wait_for_tool('AUV-1', 'work', 'inspect-1')
    for expected in (200, 300, 320, 540):
        assert clock.advance()['sim_time'] == expected
    assert clock.snapshot()['pending_wakes'] == 1
    with pytest.raises(ClockConflict, match='wake'):
        clock.advance()
    clock.resume('AUV-1', 'work', 'inspect-1')
    with pytest.raises(ClockConflict, match='decision still active'):
        clock.advance()
    clock.end('AUV-1', 'work')
    assert clock.advance()['sim_time'] == 660
    assert environment.status(agent_id='AUV-1', assignment_id='inspect-1')['actual_end_time'] == 540


def test_api_and_native_tool_sync_and_async_wait():
    asyncio.run(_api_and_native_tool_sync_and_async_wait())


async def _api_and_native_tool_sync_and_async_wait():
    environment = NativeSimulationTools(scene())
    clock = NativeSimulationClock(environment)
    app = create_app(environment, agent_tokens={'AUV-1': 'agent-secret'},
                     control_token='driver-secret', clock=clock)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
        agent_cls, _, _ = native_agent_class()
        bridge_cls = bridge_class()
        agent = agent_cls(agent_id='AUV-1', base_url='http://sim', token='agent-secret',
                          client=client, poll_interval=0.001)
        bridge = bridge_cls(agent_id='AUV-1', base_url='http://sim', token='agent-secret', client=client)
        agent.clock_bridge = bridge
        agent_headers = {'Authorization': 'Bearer agent-secret'}
        driver_headers = {'Authorization': 'Bearer driver-secret'}
        assert (await client.post('http://sim/tasks', json={'task_id': 'inspect', 'assignment_id': 'premature'},
                                  headers=agent_headers)).status_code == 409
        assert (await client.post('http://sim/clock/step', headers=agent_headers)).status_code == 403

        model_started = asyncio.Event()
        model_release = asyncio.Event()

        async def slow_native_decision():
            async with bridge.activity():
                model_started.set()
                await model_release.wait()  # stand-in for a slow external LLM

        model = asyncio.create_task(slow_native_decision())
        await model_started.wait()
        await asyncio.sleep(0.02)  # elapsed wall time must not move simulation
        assert clock.now == 0
        assert (await client.post('http://sim/clock/step', headers=driver_headers)).status_code == 409
        model_release.set()
        await model
        assert (await client.post('http://sim/clock/step', headers=driver_headers)).json()['sim_time'] == 180

        async def native_sync_assignment():
            async with bridge.activity():
                # The native synchronous branch awaits this coroutine.
                return json.loads(await agent.run(request('inspect', 'native-sync')))

        sync_work = asyncio.create_task(native_sync_assignment())
        for _ in range(200):
            if clock.snapshot()['waiting_tools'] == 1:
                break
            await asyncio.sleep(0.001)
        assert clock.snapshot()['waiting_tools'] == 1
        assert clock.snapshot()['active_decisions'] == 0
        for expected in (200, 300, 320, 540):
            assert (await client.post('http://sim/clock/step', headers=driver_headers)).json()['sim_time'] == expected
        assert clock.snapshot()['pending_wakes'] == 1
        assert (await client.post('http://sim/clock/step', headers=driver_headers)).status_code == 409
        result = await asyncio.wait_for(sync_work, timeout=2)
        assert result['status'] == 'COMPLETED' and result['actual_end_time'] == 540
        assert clock.snapshot()['pending_wakes'] == 0
        assert clock.snapshot()['active_decisions'] == 0

        # Native async assignment can release its discussion turn after the
        # background tool task has registered; waiting work continues alone.
        async def native_async_assignment():
            async with bridge.activity():
                return json.loads(await agent.run(request('inspect', 'native-async')))

        async_work = asyncio.create_task(native_async_assignment())
        for _ in range(200):
            if clock.snapshot()['waiting_tools'] == 1:
                break
            await asyncio.sleep(0.001)
        assert clock.snapshot()['waiting_tools'] == 1
        assert clock.advance()['sim_time'] == 660
        assert clock.advance()['sim_time'] == 700
        assert clock.advance()['sim_time'] == 900  # physical work ends at 540+360
        assert clock.snapshot()['pending_wakes'] == 1
        second = await asyncio.wait_for(async_work, timeout=2)
        assert second['actual_end_time'] == 900


def test_technical_abort_stops_clock():
    environment = NativeSimulationTools(scene())
    clock = NativeSimulationClock(environment)
    clock.begin('AUV-1', 'work')
    environment.submit(agent_id='AUV-1', task_id='inspect', assignment_id='x')
    clock.wait_for_tool('AUV-1', 'work', 'x')
    clock.resume('AUV-1', 'work', 'x', aborted=True)
    clock.end('AUV-1', 'work')
    with pytest.raises(ClockConflict, match='technical fault'):
        clock.advance()


def test_async_driver_advances_while_native_tool_waits_and_separates_wall_timeout():
    asyncio.run(_async_driver_advances_while_native_tool_waits())


async def _async_driver_advances_while_native_tool_waits():
    from common_environment.native_clock_driver import HttpClockControl, NativeClockDriver, ClockWaitTimeout

    environment = NativeSimulationTools(scene())
    clock = NativeSimulationClock(environment)
    app = create_app(environment, agent_tokens={'AUV-1': 'agent-secret'},
                     control_token='driver-secret', clock=clock)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
        agent_cls, _, _ = native_agent_class()
        agent = agent_cls(agent_id='AUV-1', base_url='http://sim', token='agent-secret',
                          client=client, poll_interval=0.001)
        bridge = bridge_class()(agent_id='AUV-1', base_url='http://sim',
                                token='agent-secret', client=client)
        agent.clock_bridge = bridge
        control = HttpClockControl(base_url='http://sim', control_token='driver-secret', client=client)
        worker = None

        async def native_job():
            async with bridge.activity():
                return json.loads(await agent.run(request('inspect', 'driver-job')))

        async def settle(now):
            nonlocal worker
            if now == 0 and worker is None:
                worker = asyncio.create_task(native_job())
                while clock.snapshot()['waiting_tools'] != 1:
                    await asyncio.sleep(0.001)

        driver = NativeClockDriver(status=control.status, step=control.step,
                                   settle=settle, poll_seconds=0.001,
                                   max_wall_wait_seconds=1)
        state = await asyncio.wait_for(driver.run_until(660), 2)
        assert state['sim_time'] == 660
        assert (await worker)['actual_end_time'] == 360
        assert state['active_decisions'] == 0 and state['pending_wakes'] == 0
        assert any(row['event_type'] == 'native_action_waiting_for_tool' for row in clock.events)
        assert any(row['event_type'] == 'native_action_resumed' for row in clock.events)

    stuck_environment = NativeSimulationTools(scene())
    stuck_clock = NativeSimulationClock(stuck_environment)
    stuck_clock.begin('UAV-1', 'model-call')

    async def stuck_status():
        return stuck_clock.snapshot()

    async def stuck_step():
        return stuck_clock.advance()

    async def no_messages(_now):
        pass

    stuck_driver = NativeClockDriver(status=stuck_status, step=stuck_step,
                                     settle=no_messages, poll_seconds=0.001,
                                     max_wall_wait_seconds=0.02)
    with pytest.raises(ClockWaitTimeout, match='simulation t=0'):
        await stuck_driver.run_until(180)
    assert stuck_clock.now == 0  # a slow model is a technical timeout, not t=900

    async def unacknowledged_status():
        return {'sim_time': 720, 'active_decisions': 0, 'waiting_tools': 0,
                'pending_wakes': 0, 'pending_deliveries': 1, 'fault': None}

    delivery_driver = NativeClockDriver(status=unacknowledged_status, step=stuck_step,
        settle=no_messages, poll_seconds=0.001, max_wall_wait_seconds=1,
        max_delivery_ack_wait_seconds=0.02)
    with pytest.raises(ClockWaitTimeout, match='not acknowledged at simulation t=720'):
        await delivery_driver.run_until(900)
