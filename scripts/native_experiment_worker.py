"""One isolated original-IoA run inside the upstream dependency image.

This process hosts only the physical gateway and clock driver. Each original
IoA Agent runs its own unmodified FastAPI client process; the original Server
is a separate container. No CO planner/controller is imported here.
"""
import argparse
import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
import secrets
import sys
from time import monotonic

sys.path.insert(0, '/native_env')

import httpx
import uvicorn

from common_environment.contracts import initial_observation
from common_environment.evaluator import evaluate_native
from common_environment.native_clock import NativeSimulationClock
from common_environment.native_clock_driver import HttpClockControl, NativeClockDriver
from common_environment.native_tool import NativeSimulationTools
from common_environment.native_tool_api import create_app
from common_environment.native_transport import NativeWindowRouter


TOOL_CONTRACT = (
    'Physical execution uses your SimulationMission tool. A delegated task must '
    'ultimately give that tool exactly one line SIM_TASK_JSON: {"task_id":"<scenario task ID>",'
    '"assignment_id":"<new unique ID>"}. Optional fields are requested_start, '
    'inputs (dependency task IDs mapped to actual completion receipts), valid_until, '
    'and revision_index (only an explicitly allowed scenario revision). The tool '
    'decides actual duration, capability, deadline and success. A verbal claim or '
    'invented receipt cannot substitute for a tool result.'
)


def goal_text(scenario):
    objective = scenario['objective']
    payload = {key: scenario.get(key) for key in
               ('requirements', 'hard_constraints', 'adjustment_policy', 'revisions')}
    return (f'Mission objective: {objective}\n'
            'Use the native IoA discovery, team formation and coordination process. '
            'Choose teammates and task assignments yourself. Do not assume future '
            'contact changes are known until observed or communicated.\n'
            'Initial authorized task specification: '
            + json.dumps(payload, ensure_ascii=False, sort_keys=True)
            + '\n' + TOOL_CONTRACT)


def config_for(scenario, node, *, model, temperature, gateway_url):
    name = node['id']
    observation = initial_observation(scenario, name, include_forecast=True)
    observation['tool_contract'] = TOOL_CONTRACT
    role = 'Human Assistant' if 'INITIATOR' in node.get('roles', []) else 'Thing Assistant'
    description = (f"{node['platform']} platform; roles: {', '.join(node.get('roles', []))}; "
                   f"capabilities: {', '.join(node.get('capabilities', [])) or 'none'}.")
    return {
        'server': {'hostname': 'server', 'port': 7788},
        'tool_agent': {'agent_type': 'SimulationMission', 'agent_id': name,
                       'base_url': gateway_url, 'token_env': 'NATIVE_SIM_AGENT_TOKEN'},
        'simulation_clock': {'base_url': gateway_url,
                             'token_env': 'NATIVE_SIM_AGENT_TOKEN',
                             'initial_observation': observation},
        'comm': {'name': name, 'desc': description, 'type': role,
                 'support_nested_teams': False, 'max_team_up_attempts': 3,
                 'discussion_only': False, 'observation_func': 'dummy',
                 'llm': {'llm_type': 'formal-openai-chat' if os.environ.get('CO_IOA_FORMAL_PROFILE') else 'openai-chat', 'model': model,
                         'temperature': temperature}},
    }


async def wait_for_http(url, *, processes=(), seconds=120):
    started = monotonic()
    async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
        while monotonic() - started < seconds:
            for process in processes:
                if process.returncode is not None:
                    raise RuntimeError(f'native Agent exited during startup: {process.returncode}')
            try:
                response = await client.get(url)
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.2)
    raise TimeoutError(f'native service did not start: {url}')


async def run(scenario, output, *, model, temperature, max_turns, wall_wait_seconds):
    output.mkdir(parents=True, exist_ok=True)
    environment = NativeSimulationTools(scenario)
    early_failure_rule = os.environ.get('CO_IOA_EARLY_FAILURE_RULE')
    if early_failure_rule not in (None, 'physical_deadline_v1'):
        raise ValueError('unknown external early-failure rule')
    early_failure = early_failure_rule == 'physical_deadline_v1'
    clock = NativeSimulationClock(environment, early_failure=early_failure)
    gateway_url = 'http://127.0.0.1:17881'
    tokens = {node['id']: secrets.token_urlsafe(24) for node in scenario['agents']}
    control_token = secrets.token_urlsafe(24)

    async def forward(operation, body):
        async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
            response = await client.post('http://server:7788/' + operation, json=body)
            response.raise_for_status()
            return response.json()

    router = NativeWindowRouter(environment, clock=clock,
                                forward_http=forward, deliver_ws=None)
    async def publish_progress():
        while True:
            snapshot = clock.snapshot()
            progress = {'sim_time': snapshot['sim_time'],
                        'active_decisions': snapshot['active_decisions'],
                        'waiting_tools': snapshot['waiting_tools'],
                        'pending_deliveries': snapshot['pending_deliveries']}
            temporary = output / 'progress.json.tmp'
            temporary.write_text(json.dumps(progress) + '\n')
            temporary.replace(output / 'progress.json')
            await asyncio.sleep(5)

    progress_task = asyncio.create_task(publish_progress())
    app = create_app(environment, agent_tokens=tokens, control_token=control_token,
                     clock=clock, router=router)
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=17881,
                                          log_level='error', access_log=False))
    gateway_task = asyncio.create_task(server.serve())
    clients = []
    launch_task = None
    launch_result = None
    run_error = None
    try:
        await wait_for_http(gateway_url + '/openapi.json', seconds=30)
        configs = output / 'native_configs'
        logs = output / 'client_logs'
        configs.mkdir(exist_ok=True)
        logs.mkdir(exist_ok=True)
        # Upstream common.log checks then creates this shared directory without
        # exist_ok; concurrent Agent imports would otherwise race at startup.
        Path('/opt/ioa/client/logs').mkdir(parents=True, exist_ok=True)
        async with httpx.AsyncClient(timeout=15, trust_env=False) as probe:
            health = await probe.post('http://server:7788/health_check', json={})
            health.raise_for_status()
        for index, node in enumerate(scenario['agents']):
            name = node['id']
            path = configs / (name + '.json')
            path.write_text(json.dumps(config_for(scenario, node, model=model,
                temperature=temperature, gateway_url=gateway_url), ensure_ascii=False,
                indent=2) + '\n')
            log = (logs / (name + '.log')).open('w', encoding='utf-8')
            child_env = dict(os.environ, CUSTOM_CONFIG=str(path),
                             NATIVE_SIM_AGENT_TOKEN=tokens[name],
                             OPENAI_RESPONSE_LOG_PATH=str(output / 'model_calls' / name))
            process = await asyncio.create_subprocess_exec(sys.executable, '-B',
                '-m', 'uvicorn', 'main:app', '--app-dir', '/native_client',
                '--host', '127.0.0.1', '--port', str(15000 + index),
                '--log-level', 'error', stdout=log, stderr=asyncio.subprocess.STDOUT,
                env=child_env, cwd='/opt/ioa/client')
            log.close()
            clients.append(process)
        for index, _node in enumerate(scenario['agents']):
            await wait_for_http(f'http://127.0.0.1:{15000 + index}/openapi.json',
                                processes=clients, seconds=180)
        print('Native original Server and all Agent clients ready; launching original IoA goal.', flush=True)
        initiators = [node['id'] for node in scenario['agents']
                      if 'INITIATOR' in node.get('roles', [])]
        if len(initiators) != 1:
            raise ValueError('native scenario needs one initiator')
        initiator_index = next(index for index, node in enumerate(scenario['agents'])
                               if node['id'] == initiators[0])
        payload = {'goal': goal_text(scenario), 'team_member_names': None,
                   'team_up_depth': 0, 'is_collaborative_planning_enabled': False,
                   'max_turns': max_turns}
        (output / 'launch_request.json').write_text(json.dumps(payload,
            ensure_ascii=False, indent=2) + '\n')

        async def launch():
            async with httpx.AsyncClient(timeout=None, trust_env=False) as client:
                response = await client.post(f'http://127.0.0.1:{15000 + initiator_index}/launch_goal',
                                             json=payload)
                response.raise_for_status()
                return response.json()

        launch_task = asyncio.create_task(launch())
        for _ in range(3000):
            if clock.activities or router.sessions or launch_task.done():
                break
            await asyncio.sleep(0.01)
        if not clock.activities and not router.sessions and launch_task.done():
            launch_result = await launch_task
            raise RuntimeError('native goal returned before any coordinated action')
        if not clock.activities and not router.sessions:
            raise TimeoutError('native goal did not start a clocked decision')
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            control = HttpClockControl(base_url=gateway_url,
                control_token=control_token, client=client)
            driver = NativeClockDriver(status=control.status, step=control.step,
                settle=control.settle, poll_seconds=0.05,
                max_wall_wait_seconds=wall_wait_seconds,
                stop_when=lambda _state: evaluate_native(environment, router, clock,
                    allow_early_failure=early_failure)['outcome'] in {'success', 'failed'})
            await driver.run_until(scenario.get('max_time', 1200))
            stopped_judgement = evaluate_native(environment, router, clock,
                allow_early_failure=early_failure)
            if stopped_judgement['terminal_certificate'] is not None:
                clock._event('external_early_failure_certified',
                             certificate=stopped_judgement['terminal_certificate'])
            if stopped_judgement['outcome'] == 'success':
                print(f'Native external judge confirmed success at simulation t={clock.now}.', flush=True)
        if launch_task.done():
            launch_result = await launch_task
        else:
            try:
                launch_result = await asyncio.wait_for(asyncio.shield(launch_task), timeout=5)
            except asyncio.TimeoutError:
                pass
    except Exception as exc:
        run_error = type(exc).__name__ + ': ' + str(exc)
        clock.fault = clock.fault or {'reason': run_error}
        print('Native run technical error: ' + run_error, flush=True)
    finally:
        progress_task.cancel()
        await asyncio.gather(progress_task, return_exceptions=True)
        if launch_task is not None and not launch_task.done():
            launch_task.cancel()
            await asyncio.gather(launch_task, return_exceptions=True)
        for process in clients:
            if process.returncode is None:
                process.terminate()
        for process in clients:
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        server.should_exit = True
        await gateway_task

    judged = evaluate_native(environment, router, clock,
                             allow_early_failure=early_failure)
    if launch_result is not None:
        (output / 'native_conclusion.json').write_text(json.dumps(launch_result,
            ensure_ascii=False, indent=2) + '\n')
    (output / 'native_chat_records.json').write_text(json.dumps(router.chat_records,
        ensure_ascii=False, indent=2) + '\n')
    (output / 'transits.json').write_text(json.dumps([asdict(row) for row in router.transits],
        ensure_ascii=False, indent=2) + '\n')
    with (output / 'events.jsonl').open('w') as stream:
        for source, records in (('clock', clock.events), ('physical', environment.events),
                                ('transport', router.events)):
            for index, row in enumerate(records):
                stream.write(json.dumps({'source': source, 'source_sequence': index,
                    **row}, ensure_ascii=False) + '\n')
    budget_path = output / 'model_budget.json'
    model_calls = (json.loads(budget_path.read_text())['attempts'] if budget_path.is_file()
                   else sum(1 for _ in (output / 'model_calls').rglob('openai_response_*.json')))
    result = {'method': 'ioa_original', 'scenario': scenario['name'],
              'seed': scenario.get('seed', 0), 'planner': 'NativeIoA',
              'comparison_ready': bool(os.environ.get('CO_IOA_FORMAL_PROFILE')),
              'evaluation_scope': 'shared external judge',
              'outcome': judged['outcome'],
              'reason': run_error or ('all required results received' if judged['outcome'] == 'success'
                                     else 'shared external judge certified impossible on-time completion'
                                     if judged['terminal_certificate'] is not None
                                     else judged['outcome']),
              'ended_at': judged['ended_at'], 'error_kind': (
                  'budget_exhausted' if run_error and os.environ.get('CO_IOA_FORMAL_PROFILE') and
                  (model_calls >= json.loads(os.environ['CO_IOA_FORMAL_PROFILE'])['max_model_requests_per_run_including_retries']
                   or 'budget exhausted' in run_error)
                  else 'technical' if run_error else None),
              'evaluation_valid': judged['outcome'] != 'error',
              'external_judgement': judged,
              'metrics': {key: judged[key] for key in
                  ('on_time_execution_completion_rate', 'collaboration_success_rate',
                   'all_required_results_received_at')},
              'calls': {'model_calls': model_calls,
                        'server_requests': sum(row.kind == 'http_request' for row in router.transits)},
              'messages': {'generated': sum(row.kind == 'group' for row in router.transits),
                           'delivered': sum(row.kind == 'group' and row.status == 'DELIVERED'
                                            for row in router.transits),
                           'expired': 0},
              'pending_cleanup': judged['pending_remote_work'],
              'unreleased_reservations': [],
              'native_comm_id': launch_result[0] if isinstance(launch_result, list) and launch_result else None}
    (output / 'result.json').write_text(json.dumps(result,
        ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'scenario': scenario['name'], 'outcome': result['outcome'],
        'ended_at': result['ended_at'], 'model_calls': model_calls,
        'technical_error': run_error}, ensure_ascii=False), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scenario', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--temperature', type=float, default=0)
    parser.add_argument('--max-turns', type=int, default=20)
    parser.add_argument('--wall-wait-seconds', type=float, default=1200)
    args = parser.parse_args()
    scenario = json.loads(args.scenario.read_text())
    asyncio.run(run(scenario, args.output, model=args.model,
        temperature=args.temperature, max_turns=args.max_turns,
        wall_wait_seconds=args.wall_wait_seconds))


if __name__ == '__main__':
    main()
