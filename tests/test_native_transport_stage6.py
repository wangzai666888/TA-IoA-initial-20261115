"""Native IoA window routing: group copies and Server business use one clock."""
import asyncio

import httpx
import pytest

from common_environment.native_clock import ClockConflict, NativeSimulationClock
from common_environment.native_tool import NativeSimulationTools
from common_environment.native_tool_api import create_app
from common_environment.native_transport import NativeWindowRouter
from test_native_tool_stage4 import scene


def make_router(scenario=None):
    environment = NativeSimulationTools(scenario or scene())
    clock = NativeSimulationClock(environment)
    deliveries = []
    server_calls = []

    async def forward(operation, body):
        server_calls.append((clock.now, operation, body))
        if operation == 'teamup':
            return {'comm_id': 'native-team', 'agent_names': ['UAV-1', 'USV-1', 'AUV-1']}
        if operation == 'query_assistant':
            return [{'name': 'AUV-1', 'type': 'Thing Assistant', 'desc': 'sonar'}]
        return []

    async def deliver(receiver, frame):
        deliveries.append((clock.now, receiver, frame))

    router = NativeWindowRouter(environment, clock=clock, forward_http=forward,
                                deliver_ws=deliver)
    clock.router = router
    return environment, clock, router, deliveries, server_calls


def test_server_request_and_reply_obey_windows_and_wake_at_same_second():
    asyncio.run(_server_request_and_reply())


async def _server_request_and_reply():
    environment, clock, router, _, calls = make_router()
    clock.begin('UAV-1', 'native-discovery')
    ticket = router.submit_http('UAV-1', 'teamup',
                                {'sender': 'UAV-1', 'agent_names': ['USV-1', 'AUV-1']})
    with pytest.raises(ClockConflict, match='decision still active'):
        clock.advance()
    clock.wait_for_message('UAV-1', 'native-discovery', ticket)
    await router.settle(0)
    assert calls[0][0:2] == (0, 'teamup')
    assert router.request_status('UAV-1', ticket)['status'] == 'READY'
    assert clock.snapshot()['pending_wakes'] == 1
    with pytest.raises(ClockConflict, match='wake'):
        clock.advance()
    clock.resume_message('UAV-1', 'native-discovery', ticket)
    clock.end('UAV-1', 'native-discovery')
    assert router.sessions['native-team'] == ['UAV-1', 'USV-1', 'AUV-1']
    assert clock.advance()['sim_time'] == 180


def test_group_copies_preserve_order_and_local_echo_without_link():
    asyncio.run(_group_copies_preserve_order())


def test_original_server_relay_uses_uplink_then_each_downlink():
    async def check():
        _, clock, router, deliveries, _ = make_router()
        router.sessions['team'] = ['UAV-1', 'USV-1', 'AUV-1']
        router.chat_records['team'] = {'chat_record': []}
        router.enqueue_group('UAV-1', {'sender': 'UAV-1', 'comm_id': 'team',
                                        'content': 'inspect using the original Server relay'})
        assert not router.chat_records['team']['chat_record']
        await router.settle(0)
        assert [recipient for _, recipient, _ in deliveries] == ['UAV-1', 'USV-1']
        assert len(router.chat_records['team']['chat_record']) == 1
        for _, recipient, frame in deliveries:
            clock.begin(recipient, frame['_sim_delivery_id'], listener=True)
            router.ack(recipient, frame['_sim_delivery_id'])
            clock.end(recipient, frame['_sim_delivery_id'])
        assert clock.advance()['sim_time'] == 180
        await router.settle(180)
        auv = [(recipient, frame) for _, recipient, frame in deliveries if recipient == 'AUV-1']
        assert len(auv) == 1
        assert auv[0][1]['_sim_created_at'] == 0
        assert auv[0][1]['_sim_delivered_at'] == 180
        assert router.schedule.next_delivery('UAV-1', 'AUV-1', 0) is None
    asyncio.run(check())


async def _group_copies_preserve_order():
    _, clock, router, deliveries, _ = make_router()
    router.sessions['native-team'] = ['USV-1', 'AUV-1', 'UAV-1']
    router.chat_records['native-team'] = {'comm_id': 'native-team', 'agent_names': router.sessions['native-team'],
                                          'team_name': None, 'chat_record': []}
    for index in (1, 2):
        router.enqueue_group('USV-1', {'sender': 'USV-1', 'comm_id': 'native-team',
                                        'content': 'command-' + str(index)})
    with pytest.raises(ClockConflict, match='settle'):
        clock.advance()
    await router.settle(0)
    assert [frame['content'] for _, receiver, frame in deliveries if receiver == 'AUV-1'] == []
    assert [receiver for _, receiver, _ in deliveries] == ['USV-1', 'UAV-1', 'USV-1', 'UAV-1']
    with pytest.raises(ClockConflict, match='acknowledgement'):
        clock.advance()
    for _, receiver, frame in deliveries:
        clock.begin(receiver, frame['_sim_delivery_id'])
        router.ack(receiver, frame['_sim_delivery_id'])
        clock.end(receiver, frame['_sim_delivery_id'])
    assert clock.advance()['sim_time'] == 180
    await router.settle(180)
    auv = [(receiver, frame) for _, receiver, frame in deliveries if receiver == 'AUV-1']
    assert [frame['content'] for _, frame in auv] == ['command-1', 'command-2']
    assert all(frame['_sim_created_at'] == 0 and frame['_sim_delivered_at'] == 180 for _, frame in auv)
    for receiver, frame in auv:
        clock.begin(receiver, frame['_sim_delivery_id'])
        router.ack(receiver, frame['_sim_delivery_id'])
        clock.end(receiver, frame['_sim_delivery_id'])
    assert len(router.chat_records['native-team']['chat_record']) == 2
    assert len({row.transit_id for row in router.transits if row.status == 'DELIVERED'}) == 6


def test_group_sender_identity_and_server_request_identity_checked():
    _, clock, router, _, _ = make_router()
    router.sessions['team'] = ['UAV-1', 'USV-1']
    router.chat_records['team'] = {'chat_record': []}
    with pytest.raises(ValueError, match='sender differs'):
        router.enqueue_group('UAV-1', {'sender': 'AUV-1', 'comm_id': 'team'})
    with pytest.raises(ValueError, match='sender differs'):
        router.submit_http('UAV-1', 'teamup', {'sender': 'AUV-1', 'agent_names': []})
    with pytest.raises(KeyError):
        router.request_status('AUV-1', 'unknown')


def test_api_ticket_authorization_and_window_queue():
    asyncio.run(_api_ticket_authorization_and_window_queue())


async def _api_ticket_authorization_and_window_queue():
    environment, clock, router, _, calls = make_router()
    # Use the real ASGI endpoints with a fake Server backend. Group delivery
    # itself is exercised in the direct router test above.
    app = create_app(environment, agent_tokens={'UAV-1': 'agent-uav', 'AUV-1': 'agent-auv'},
                     control_token='driver-secret', clock=clock, router=router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
        uav = {'Authorization': 'Bearer agent-uav'}
        auv = {'Authorization': 'Bearer agent-auv'}
        driver = {'Authorization': 'Bearer driver-secret'}
        assert (await client.post('http://sim/transport/request',
            json={'operation': 'teamup', 'body': {'sender': 'UAV-1', 'agent_names': ['AUV-1']}},
            headers=uav)).status_code == 409
        begun = await client.post('http://sim/clock/activity/begin',
                                  json={'activity_id': 'choose'}, headers=uav)
        assert begun.status_code == 200
        ticket = (await client.post('http://sim/transport/request',
            json={'operation': 'teamup', 'body': {'sender': 'UAV-1', 'agent_names': ['AUV-1']}},
            headers=uav)).json()['ticket']
        assert (await client.get('http://sim/transport/requests/' + ticket, headers=auv)).status_code == 404
        assert (await client.post('http://sim/clock/activity/wait-message',
            json={'activity_id': 'choose', 'assignment_id': ticket}, headers=uav)).status_code == 200
        assert (await client.post('http://sim/transport/settle', headers=driver)).status_code == 200
        assert calls[0][0:2] == (0, 'teamup')
        assert (await client.post('http://sim/clock/step', headers=driver)).status_code == 409
        assert (await client.post('http://sim/clock/activity/resume-message',
            json={'activity_id': 'choose', 'assignment_id': ticket}, headers=uav)).status_code == 200
        assert (await client.post('http://sim/clock/activity/end',
            json={'activity_id': 'choose'}, headers=uav)).status_code == 200
        assert (await client.post('http://sim/clock/step', headers=driver)).json()['sim_time'] == 180


def test_sync_listener_mailbox_does_not_deadlock_physical_task():
    asyncio.run(_sync_listener_mailbox())


async def _sync_listener_mailbox():
    environment, clock, router, deliveries, _ = make_router()
    router.sessions['native-team'] = ['USV-1', 'AUV-1']
    router.chat_records['native-team'] = {'chat_record': []}
    clock.advance(180)
    clock.begin('AUV-1', 'native-sync', listener=True)
    environment.submit(agent_id='AUV-1', task_id='inspect', assignment_id='inspect-1')
    clock.wait_for_tool('AUV-1', 'native-sync', 'inspect-1')
    router.enqueue_group('USV-1', {'sender': 'USV-1', 'comm_id': 'native-team',
                                    'content': 'new message during synchronous work'})
    await router.settle(180)
    auv_delivery = next(frame for _, receiver, frame in deliveries if receiver == 'AUV-1')
    assert clock.snapshot()['buffered_deliveries'] == 2  # local echo plus AUV copy
    assert clock.snapshot()['pending_deliveries'] == 1  # USV listener is free
    usv_delivery = next(frame for _, receiver, frame in deliveries if receiver == 'USV-1')
    clock.begin('USV-1', 'ack-usv', listener=True)
    router.ack('USV-1', usv_delivery['_sim_delivery_id'])
    clock.end('USV-1', 'ack-usv')
    assert clock.snapshot()['pending_deliveries'] == 0
    for expected in (200, 300, 320, 540):
        assert clock.advance()['sim_time'] == expected
    clock.resume('AUV-1', 'native-sync', 'inspect-1')
    clock.end('AUV-1', 'native-sync')
    assert clock.snapshot()['pending_deliveries'] == 1
    with pytest.raises(ClockConflict, match='acknowledgement'):
        clock.advance()
    clock.begin('AUV-1', 'process-mailbox', listener=True)
    router.ack('AUV-1', auv_delivery['_sim_delivery_id'])
    clock.end('AUV-1', 'process-mailbox')
    assert clock.advance()['sim_time'] == 660


def test_server_query_cannot_bypass_closed_uplink_or_missing_reply_window():
    asyncio.run(_server_query_cannot_bypass())


async def _server_query_cannot_bypass():
    environment, clock, router, _, calls = make_router()
    ticket = router.submit_http('AUV-1', 'query_assistant', {'name': 'USV-1'})
    await router.settle(0)
    assert calls == []
    assert router.request_status('AUV-1', ticket)['status'] == 'PENDING'
    # The AUV-1 -> USV-1 physical uplink begins at t=660. Its reverse
    # USV-1 -> AUV-1 window already ended at t=200.
    environment.advance(660)
    await router.settle(660)
    assert calls[0][0:2] == (660, 'query_assistant')
    assert router.request_status('AUV-1', ticket)['status'] == 'PENDING'
    response = next(row for row in router.transits if row.kind == 'http_response')
    assert response.status == 'QUEUED'
    assert router.schedule.next_delivery('USV-1', 'AUV-1', 660) is None


def test_positive_latency_requires_full_window_and_preserves_arrival_second():
    asyncio.run(_positive_latency_window())


async def _positive_latency_window():
    scenario = scene()
    scenario['latency'] = 2
    scenario['windows'] = [{'sender_id': 'USV-1', 'receiver_id': 'AUV-1',
                            'start': 5, 'end': 10}]
    environment, clock, router, deliveries, _ = make_router(scenario)
    router.sessions['team'] = ['USV-1', 'AUV-1']
    router.chat_records['team'] = {'chat_record': []}
    router.enqueue_group('USV-1', {'sender': 'USV-1', 'comm_id': 'team', 'content': 'inspect'})
    await router.settle(0)
    assert [receiver for _, receiver, _ in deliveries] == ['USV-1']
    environment.advance(5)
    await router.settle(5)
    assert [receiver for _, receiver, _ in deliveries] == ['USV-1']
    assert router.next_boundary(5) == 7
    environment.advance(7)
    await router.settle(7)
    assert deliveries[-1][0:2] == (7, 'AUV-1')
