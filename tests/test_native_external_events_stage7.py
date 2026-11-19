"""Stage 7: physical changes and private observations use one simulation tick."""
import asyncio

from common_environment.native_clock import NativeSimulationClock, ClockConflict
from common_environment.native_tool import NativeSimulationTools
from common_environment.native_transport import NativeWindowRouter
from test_native_tool_stage4 import scene


def setup(name):
    environment = NativeSimulationTools(scene(name))
    clock = NativeSimulationClock(environment)
    deliveries = []

    async def deliver(recipient, frame):
        deliveries.append((environment.now, recipient, frame))

    async def forward(_operation, _payload):
        raise AssertionError('external news must not use native Server business call')

    router = NativeWindowRouter(environment, clock=clock, forward_http=forward,
                                deliver_ws=deliver)
    clock.router = router
    return environment, clock, router, deliveries


def acknowledge(clock, router, receiver, frame):
    clock.begin(receiver, frame['_sim_delivery_id'], listener=True)
    router.ack(receiver, frame['_sim_delivery_id'])
    clock.end(receiver, frame['_sim_delivery_id'])


def test_observed_change_updates_truth_but_only_named_observer_gets_forecast():
    async def check():
        _, clock, router, deliveries = setup('delayed_contact')
        assert clock.advance()['sim_time'] == 120
        assert router.schedule.next_delivery('USV-1', 'AUV-1', 180) == (720, 720)
        assert not deliveries
        await router.settle(120)
        assert [(t, recipient) for t, recipient, _ in deliveries] == [(120, 'USV-1')]
        frame = deliveries[0][2]
        assert frame['_sim_kind'] == 'external_event'
        assert frame['event'] == {'time': 120, 'type': 'prediction_update',
                                  'sender': 'USV-1', 'receiver': 'AUV-1',
                                  'intervals': [[720, 740]]}
        assert 'observers' not in frame['event']
        assert all(row.kind != 'group' for row in router.transits)
        await router.settle(120)
        assert len(deliveries) == 1
        try:
            clock.advance()
            raise AssertionError('unacknowledged local news let clock move')
        except ClockConflict:
            pass
        acknowledge(clock, router, 'USV-1', frame)
        assert clock.advance()['sim_time'] == 180
    asyncio.run(check())


def test_unobserved_physical_change_is_not_leaked_to_any_agent():
    async def check():
        _, clock, router, deliveries = setup('unobserved_window_change')
        assert clock.advance()['sim_time'] == 120
        assert router.schedule.next_delivery('USV-1', 'AUV-1', 180) == (720, 720)
        await router.settle(120)
        assert not deliveries
        assert not any(row.kind == 'external_event' for row in router.transits)
        assert clock.advance()['sim_time'] == 180
    asyncio.run(check())


def test_objective_update_is_private_to_initiator_and_preserves_payload():
    async def check():
        _, clock, router, deliveries = setup('objective_revision_parallel')
        assert clock.advance()['sim_time'] == 120
        await router.settle(120)
        assert [recipient for _, recipient, _ in deliveries] == ['UAV-1']
        frame = deliveries[0][2]
        assert frame['event']['type'] == 'objective_update'
        assert frame['event']['time'] == 120
        assert len(frame['event']['payload']['requirements']) == 3
        assert not router.chat_records
        acknowledge(clock, router, 'UAV-1', frame)
    asyncio.run(check())


def test_change_precedes_same_second_physical_completion():
    environment, clock, router, _ = setup('delayed_contact')
    environment.submit(agent_id='AUV-1', task_id='inspect', assignment_id='i1')
    observed = []
    original = environment._advance_one

    def inspect_at(second):
        if second == 120:
            observed.append(router.schedule.next_delivery('USV-1', 'AUV-1', 180))
        return original(second)

    environment._advance_one = inspect_at
    assert clock.advance()['sim_time'] == 120
    assert observed == [(720, 720)]
