"""Stage 4 native tool boundary: physical execution without a collaboration policy."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import types

import httpx
import pytest

from common_environment.native_tool import NativeSimulationTools
from common_environment.native_tool_api import create_app

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM_AGENT = Path('/root/IoA_upstream/im_client/agents/simulation/simulation_agent.py')


def scene(name='baseline'):
    return json.loads((ROOT / 'co_ioa/scenarios' / (name + '.json')).read_text())


def native_agent_class():
    """Load only the new adapter: upstream's unrelated LLM stack lives in its Docker image."""
    package = types.ModuleType('stage4_native')
    package.__path__ = []
    subpackage = types.ModuleType('stage4_native.simulation')
    subpackage.__path__ = []
    base = types.ModuleType('stage4_native.base')
    base.AgentAdapter = object
    old = {name: sys.modules.get(name) for name in
           ('stage4_native', 'stage4_native.simulation', 'stage4_native.base')}
    sys.modules.update({'stage4_native': package, 'stage4_native.simulation': subpackage,
                        'stage4_native.base': base})
    try:
        spec = importlib.util.spec_from_file_location('stage4_native.simulation.simulation_agent', UPSTREAM_AGENT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.SimulationMissionAgent, module.SimulationTaskError, module.parse_task
    finally:
        for name, value in old.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


def request(task_id, assignment_id, **extra):
    return 'Physical assignment\nSIM_TASK_JSON: ' + json.dumps(
        {'task_id': task_id, 'assignment_id': assignment_id, **extra}) + '\n'


def test_physical_rule_duration_resource_and_deadline():
    environment = NativeSimulationTools(scene())
    environment.advance(180)
    first = environment.submit(agent_id='AUV-1', task_id='inspect', assignment_id='one')
    assert (first['status'], first['actual_start_time'], first['scheduled_end']) == ('RUNNING', 180, 540)
    assert environment.submit(agent_id='AUV-1', task_id='inspect', assignment_id='one') == first
    with pytest.raises(ValueError, match='conflicting duplicate'):
        environment.submit(agent_id='AUV-1', task_id='inspect', assignment_id='one', requested_start=1)
    busy = environment.submit(agent_id='AUV-1', task_id='inspect', assignment_id='two')
    assert busy['status'] == 'REJECTED' and busy['reason'] == 'local_resource_conflict'
    incapable = environment.submit(agent_id='UAV-1', task_id='inspect', assignment_id='three')
    assert incapable['status'] == 'REJECTED' and incapable['reason'] == 'NO_CAPABILITY_MATCH'
    too_late = environment.submit(agent_id='AUV-2', task_id='inspect', assignment_id='four', requested_start=541)
    assert too_late['status'] == 'REJECTED' and too_late['reason'] == 'late_delivery_or_execution_deadline'
    environment.advance(539)
    assert environment.status(agent_id='AUV-1', assignment_id='one')['status'] == 'RUNNING'
    environment.advance(540)
    done = environment.status(agent_id='AUV-1', assignment_id='one')
    assert done['status'] == 'COMPLETED' and done['actual_end_time'] == 540
    assert done['result']['executor_id'] == 'AUV-1'
    with pytest.raises(ValueError, match='monotonically'):
        environment.advance(539)


def test_dependency_requires_completed_receipt_not_claimed_input():
    environment = NativeSimulationTools(scene('dependency'))
    missing = environment.submit(agent_id='USV-1', task_id='analyse', assignment_id='a0')
    assert missing['status'] == 'REJECTED' and missing['reason'] == 'missing_dependency_inputs'
    forged = environment.submit(agent_id='USV-1', task_id='analyse', assignment_id='a1',
                                inputs={'inspect': 'fabricated'})
    assert forged['status'] == 'REJECTED'
    environment.advance(180)
    environment.submit(agent_id='AUV-1', task_id='inspect', assignment_id='i1')
    environment.advance(540)
    receipt = environment.status(agent_id='AUV-1', assignment_id='i1')['result']['receipt']
    accepted = environment.submit(agent_id='USV-1', task_id='analyse', assignment_id='a2',
                                  inputs={'inspect': receipt})
    assert accepted['status'] == 'RUNNING' and accepted['scheduled_end'] == 570
    environment.advance(570)
    assert environment.status(agent_id='USV-1', assignment_id='a2')['status'] == 'COMPLETED'


def test_uncancellable_and_configured_failure():
    cancellation_scene = scene('cancel_pending_cleanup')
    cancellation_scene['max_time'] = 600  # test copy only; fixed scenario file is untouched
    environment = NativeSimulationTools(cancellation_scene)
    environment.advance(180)
    environment.submit(agent_id='AUV-1', task_id='inspect', assignment_id='i1')
    environment.advance(200)
    assert environment.cancel(agent_id='AUV-1', assignment_id='i1')['stopped'] is False
    assert environment.status(agent_id='AUV-1', assignment_id='i1')['status'] == 'RUNNING'
    environment.advance(540)
    assert environment.status(agent_id='AUV-1', assignment_id='i1')['status'] == 'COMPLETED'
    failed_scene = scene()
    next(a for a in failed_scene['agents'] if a['id'] == 'AUV-1')['fail_tasks'] = ['inspect']
    failed = NativeSimulationTools(failed_scene)
    failed.submit(agent_id='AUV-1', task_id='inspect', assignment_id='x')
    failed.advance(360)
    assert failed.status(agent_id='AUV-1', assignment_id='x')['status'] == 'FAILED'
    assert failed.status(agent_id='AUV-1', assignment_id='x')['result'] is None


def test_http_boundary_and_native_agent_wait_for_clock():
    asyncio.run(_http_boundary_and_native_agent_wait_for_clock())


def test_model_rephrased_inline_task_marker_is_accepted_once():
    _, error_cls, parse_task = native_agent_class()
    inline = ('Execute sonar. Call SimulationMission with exactly one line: '
              'SIM_TASK_JSON: {"task_id":"inspect","assignment_id":"inline-1",'
              '"requested_start":180}. Wait for the real receipt.')
    assert parse_task(inline) == {
        'task_id': 'inspect', 'assignment_id': 'inline-1', 'requested_start': 180}
    markerless = ('Submit to SimulationMission exactly one line: '
                  '{"task_id":"inspect","assignment_id":"inline-1",'
                  '"requested_start":180}. Wait for the actual receipt.')
    assert parse_task(markerless) == parse_task(inline)
    wrapped = ('## Task Inputs (including dialogues and takeaways from PREVIOUS collaboration)\n'
               'A historical instruction had SIM_TASK_JSON: '
               '{"task_id":"inspect","assignment_id":"old"}.\n'
               '## Task Description\n' + markerless)
    assert parse_task(wrapped) == parse_task(markerless)
    with pytest.raises(error_cls, match='exactly one'):
        parse_task(inline + ' SIM_TASK_JSON: {"task_id":"inspect","assignment_id":"inline-2"}')
    with pytest.raises(error_cls, match='exactly one'):
        parse_task(markerless + ' {"task_id":"inspect","assignment_id":"inline-2"}')
    with pytest.raises(error_cls, match='allowed optional fields'):
        parse_task('SIM_TASK_JSON: {"task_id":"inspect","assignment_id":"x","secret":1}')


async def _http_boundary_and_native_agent_wait_for_clock():
    environment = NativeSimulationTools(scene())
    app = create_app(environment, agent_tokens={'AUV-1': 'token-1', 'AUV-2': 'token-2'},
                     control_token='driver-secret')
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport) as client:
        agent_cls, error_cls, parse_task = native_agent_class()
        with pytest.raises(error_cls, match='exactly one'):
            parse_task('inspect')
        agent = agent_cls(agent_id='AUV-1', base_url='http://sim', token='token-1',
                          client=client, poll_interval=0.001)
        headers = {'Authorization': 'Bearer token-1'}
        driver = {'Authorization': 'Bearer driver-secret'}
        assert (await client.post('http://sim/clock/advance', json={'now': 180}, headers=headers)).status_code == 403
        assert (await client.post('http://sim/clock/advance', json={'now': 180}, headers=driver)).status_code == 200
        running = asyncio.create_task(agent.run(request('inspect', 'native-1')))
        for _ in range(100):
            if ('AUV-1', 'native-1') in environment.tasks:
                break
            await asyncio.sleep(0.001)
        assert ('AUV-1', 'native-1') in environment.tasks and not running.done()
        assert (await client.get('http://sim/tasks/native-1',
                                 headers={'Authorization': 'Bearer token-2'})).status_code == 404
        await client.post('http://sim/clock/advance', json={'now': 539}, headers=driver)
        assert not running.done()
        await client.post('http://sim/clock/advance', json={'now': 540}, headers=driver)
        result = json.loads(await asyncio.wait_for(running, 2))
        assert result['status'] == 'COMPLETED' and result['actual_end_time'] == 540
        assert result['result']['receipt']
        rejected = json.loads(await agent.run(request('inspect', 'too-late', requested_start=541)))
        assert rejected['status'] == 'REJECTED'
        assert rejected['reason'] == 'late_delivery_or_execution_deadline'
        assert rejected['executor_id'] == 'AUV-1'
        assert rejected['result'] is None
        invalid = json.loads(await agent.run(request('sonar', 'wrong-task-id')))
        assert invalid['status'] == 'REJECTED'
        assert invalid['reason'] == 'unknown agent or task'
        assert invalid['executor_id'] == 'AUV-1'
        malformed = json.loads(await agent.run(request('inspect', 'bad-time', requested_start='soon')))
        assert malformed['status'] == 'REJECTED'
        assert malformed['executor_id'] == 'AUV-1'
