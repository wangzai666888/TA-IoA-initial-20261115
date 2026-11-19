"""No-model native transport smoke inside the upstream dependency image.

Mount CO project at /co and current upstream im_client at /native_client.
The fake Server preserves the original teamup response shape; this smoke
checks the new native client transport over real loopback HTTP/WebSocket.
"""
import asyncio
import json
import os
from pathlib import Path
import sys
from types import MethodType, SimpleNamespace

sys.path[:0] = ['/native_client', '/opt/ioa/client']
sys.path.append('/native_env')

import uvicorn
from common.config import global_config
from common.types.communication import AgentMessage, CommunicationState, CommunicationType
from common_environment.native_clock import NativeSimulationClock
from common_environment.native_tool import NativeSimulationTools
from common_environment.native_tool_api import create_app
from common_environment.native_transport import NativeWindowRouter
from common_environment.native_clock_driver import HttpClockControl
from simulation.clock_bridge import NativeClockBridge
from simulation.window_transport import SimulationServerHelper, WindowedNativeSocket
from simulation.clocked_communication import ClockedCommunicationLayer


async def main():
    scenario = json.loads(Path('/co/co_ioa/scenarios/baseline.json').read_text())
    # Test a private observation over the real native WebSocket without
    # changing the frozen scenario on disk.
    scenario['events'] = [{'time': 120, 'type': 'prediction_update',
                           'sender': 'USV-1', 'receiver': 'AUV-1',
                           'intervals': [[180, 200]], 'observers': ['USV-1']}]
    environment = NativeSimulationTools(scenario)
    clock = NativeSimulationClock(environment)
    calls = []

    async def forward(operation, body):
        calls.append((clock.now, operation))
        if operation == 'teamup':
            return {'comm_id': 'native-smoke-team',
                    'agent_names': ['UAV-1', 'USV-1', 'AUV-1']}
        raise AssertionError('unexpected original Server operation')

    router = NativeWindowRouter(environment, clock=clock, forward_http=forward,
                                deliver_ws=None)
    tokens = {'UAV-1': 'test-uav', 'USV-1': 'test-usv', 'AUV-1': 'test-auv'}
    app = create_app(environment, agent_tokens=tokens, control_token='test-driver',
                     clock=clock, router=router)
    port = 17883
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port,
                                          log_level='error', access_log=False))
    running = asyncio.create_task(server.serve())
    for _ in range(1000):
        if server.started:
            break
        await asyncio.sleep(0.01)
    assert server.started
    base = f'http://127.0.0.1:{port}'
    global_config['simulation_clock'] = {'base_url': base, 'token_env': 'NATIVE_SIM_AGENT_TOKEN'}
    os.environ['NATIVE_SIM_AGENT_TOKEN'] = tokens['UAV-1']
    uav_bridge = NativeClockBridge(agent_id='UAV-1', base_url=base, token=tokens['UAV-1'])
    SimulationServerHelper.clock_bridge = uav_bridge
    control = HttpClockControl(base_url=base, control_token='test-driver')
    sockets = {name: WindowedNativeSocket(agent_id=name, base_url=base, token=token)
               for name, token in tokens.items()}
    try:
        for socket in sockets.values():
            await socket.connect()

        async def teamup():
            async with uav_bridge.activity():
                return await SimulationServerHelper.teamup('UAV-1', ['USV-1', 'AUV-1'])

        pending = asyncio.create_task(teamup())
        for _ in range(1000):
            if clock.snapshot()['waiting_tools'] == 1:
                break
            await asyncio.sleep(0.01)
        assert clock.snapshot()['waiting_tools'] == 1
        await control.settle(0)
        result = await asyncio.wait_for(pending, 5)
        assert result.comm_id == 'native-smoke-team'
        assert calls == [(0, 'teamup')]

        usv_bridge = NativeClockBridge(agent_id='USV-1', base_url=base, token=tokens['USV-1'])
        message = AgentMessage(sender='USV-1', comm_id='native-smoke-team',
                               content='Inspect the site', state=CommunicationState.DISCUSSION,
                               type=CommunicationType.DISCUSSION)
        async with usv_bridge.activity():
            await sockets['USV-1'].send_message(message.model_dump_json())
        await control.settle(0)
        for name, bridge in [('USV-1', usv_bridge), ('UAV-1', uav_bridge)]:
            delivered, delivery_id = await asyncio.wait_for(sockets[name].receive_message(), 5)
            async with bridge.activity(listener=True):
                await sockets[name].ack(delivery_id)
            assert delivered.content == 'Inspect the site'
        assert (await control.step())['sim_time'] == 120
        await control.settle(120)
        local, delivery_id = await asyncio.wait_for(sockets['USV-1'].receive_message(), 5)
        assert local['event']['type'] == 'prediction_update'
        assert local['event']['time'] == 120
        assert local['delivered_at'] == 120
        # Exercise the native listener's event-to-discussion adapter without
        # invoking the model or changing upstream CommunicationLayer policy.
        probe = object.__new__(ClockedCommunicationLayer)
        probe.name = 'USV-1'
        probe.native_clock = usv_bridge
        probe.native_external_events = []
        probe._known_observation = {}
        probe.comm_bank = {'native-smoke-team': SimpleNamespace(
            goal='Inspect the site', team_members=[{'name': name} for name in tokens],
            max_turns=None)}
        observed_by_native = []

        async def record_coordination(self, message, comm_id, max_turns=None):
            observed_by_native.append((message, comm_id))

        probe.coordination = MethodType(record_coordination, probe)
        async with usv_bridge.activity(listener=True):
            await sockets['USV-1'].ack(delivery_id)
            await probe._receive_external_event(local)
        assert len(observed_by_native) == 1
        assert observed_by_native[0][0].sender == 'environment'
        assert observed_by_native[0][0].next_speaker == 'USV-1'
        assert observed_by_native[0][1] == 'native-smoke-team'
        assert (await control.step())['sim_time'] == 180
        await control.settle(180)
        delivered, delivery_id = await asyncio.wait_for(sockets['AUV-1'].receive_message(), 5)
        auv_bridge = NativeClockBridge(agent_id='AUV-1', base_url=base, token=tokens['AUV-1'])
        async with auv_bridge.activity(listener=True):
            await sockets['AUV-1'].ack(delivery_id)
        assert 'sent at simulation t=0; delivered at t=180' in delivered.content
        assert clock.snapshot()['pending_deliveries'] == 0
        print(json.dumps({'passed': True, 'teamup_at': 0,
                          'private_forecast_to_usv_at': 120,
                          'group_message_to_auv_at': 180,
                          'forwarded_server_calls': calls}))
    finally:
        for socket in sockets.values():
            if socket.websocket is not None:
                await socket.close()
        server.should_exit = True
        await running


if __name__ == '__main__':
    asyncio.run(main())
