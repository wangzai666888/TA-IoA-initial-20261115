"""One integer simulation clock for native dialogue and physical tool work.

An active native action keeps the current simulation second open. Once an
accepted physical task is submitted, its action may wait without blocking the
clock. A completion creates a wake barrier; the clock cannot pass that second
until the native action resumes and can report the result. This is a scheduler,
not a matching, messaging, contact-routing or evaluation policy.
"""
from copy import deepcopy

from .native_tool import TERMINAL


class ClockConflict(RuntimeError):
    pass


class NativeSimulationClock:
    def __init__(self, environment, *, early_failure=False):
        self.environment = environment
        self.early_failure = early_failure
        self.activities = {}  # (agent_id, activity_id) -> active | waiting + assignment
        self.pending_wakes = set()
        self.fault = None
        self.events = []
        self.router = None
        self.processed_external_events = set()

    @property
    def now(self):
        return self.environment.now

    def _event(self, kind, agent_id=None, **extra):
        self.events.append({'sim_time': self.now, 'event_type': kind,
                            'agent_id': agent_id, **extra})

    def begin(self, agent_id, activity_id, *, listener=False):
        if not isinstance(activity_id, str) or not activity_id or len(activity_id) > 128:
            raise ValueError('invalid activity_id')
        key = (agent_id, activity_id)
        if agent_id not in self.environment.agents or key in self.activities:
            raise ClockConflict('unknown agent or duplicate activity')
        if self.fault is not None or self.now > self.environment.scenario.get('max_time', 1200):
            raise ClockConflict('clock stopped by technical fault or horizon')
        self.activities[key] = {'state': 'active', 'assignment_id': None, 'wait_kind': None,
                                'listener': bool(listener)}
        self._event('native_action_started', agent_id, activity_id=activity_id)
        return {'sim_time': self.now, 'activity_id': activity_id}

    def listener_waiting(self, agent_id):
        return any(owner == agent_id and row['listener'] and row['state'] == 'waiting'
                   for (owner, _), row in self.activities.items())

    def has_active(self, agent_id):
        return any(owner == agent_id and row['state'] == 'active'
                   for (owner, _), row in self.activities.items())

    def wait_for_tool(self, agent_id, activity_id, assignment_id):
        row = self.activities.get((agent_id, activity_id))
        if row is None or row['state'] != 'active':
            raise ClockConflict('activity is not active')
        task = self.environment.status(agent_id=agent_id, assignment_id=assignment_id)
        if task['status'] not in {'ACCEPTED', 'RUNNING'}:
            raise ClockConflict('only accepted physical work can release the clock')
        row.update(state='waiting', assignment_id=assignment_id, wait_kind='tool')
        self._event('native_action_waiting_for_tool', agent_id,
                    activity_id=activity_id, assignment_id=assignment_id)
        return {'sim_time': self.now, 'activity_id': activity_id}

    def resume(self, agent_id, activity_id, assignment_id, *, aborted=False):
        row = self.activities.get((agent_id, activity_id))
        if row is None or row['state'] != 'waiting' or row['wait_kind'] != 'tool' or row['assignment_id'] != assignment_id:
            raise ClockConflict('activity is not waiting for that assignment')
        task = self.environment.status(agent_id=agent_id, assignment_id=assignment_id)
        if not aborted and task['status'] not in TERMINAL:
            raise ClockConflict('physical task has not reached a terminal state')
        if aborted:
            self.fault = {'agent_id': agent_id, 'activity_id': activity_id,
                          'assignment_id': assignment_id, 'reason': 'tool_wait_aborted'}
        self.pending_wakes.discard((agent_id, activity_id))
        row.update(state='active', assignment_id=None, wait_kind=None)
        self._event('native_action_resumed', agent_id, activity_id=activity_id,
                    assignment_id=assignment_id, status=task['status'], aborted=aborted)
        return {'sim_time': self.now, 'activity_id': activity_id}

    def wait_for_message(self, agent_id, activity_id, ticket):
        row = self.activities.get((agent_id, activity_id))
        if row is None or row['state'] != 'active' or self.router is None:
            raise ClockConflict('message wait needs one active native action and router')
        request = self.router.request_status(agent_id, ticket)
        if request['status'] != 'PENDING':
            raise ClockConflict('Server request is already complete')
        row.update(state='waiting', assignment_id=ticket, wait_kind='message')
        self._event('native_action_waiting_for_message', agent_id,
                    activity_id=activity_id, ticket=ticket)
        return {'sim_time': self.now, 'activity_id': activity_id}

    def wake_message(self, agent_id, ticket):
        for (owner, activity_id), row in self.activities.items():
            if owner == agent_id and row['state'] == 'waiting' and row['wait_kind'] == 'message' \
                    and row['assignment_id'] == ticket:
                self.pending_wakes.add((agent_id, activity_id))
                self._event('native_message_wake_ready', agent_id,
                            activity_id=activity_id, ticket=ticket)

    def resume_message(self, agent_id, activity_id, ticket, *, aborted=False):
        row = self.activities.get((agent_id, activity_id))
        if row is None or row['state'] != 'waiting' or row['wait_kind'] != 'message' \
                or row['assignment_id'] != ticket:
            raise ClockConflict('activity is not waiting for that Server request')
        if not aborted and self.router.request_status(agent_id, ticket)['status'] == 'PENDING':
            raise ClockConflict('Server response has not been delivered')
        if aborted:
            self.fault = {'agent_id': agent_id, 'activity_id': activity_id,
                          'ticket': ticket, 'reason': 'message_wait_aborted'}
        self.pending_wakes.discard((agent_id, activity_id))
        row.update(state='active', assignment_id=None, wait_kind=None)
        self._event('native_action_resumed_from_message', agent_id,
                    activity_id=activity_id, ticket=ticket, aborted=aborted)
        return {'sim_time': self.now, 'activity_id': activity_id}

    def end(self, agent_id, activity_id):
        key = (agent_id, activity_id)
        row = self.activities.get(key)
        if row is None or row['state'] != 'active':
            raise ClockConflict('activity must be active before it ends')
        del self.activities[key]
        self._event('native_action_ended', agent_id, activity_id=activity_id)
        return {'sim_time': self.now, 'activity_id': activity_id}

    def next_boundary(self):
        horizon = self.environment.scenario.get('max_time', 1200)
        if self.now >= horizon:
            return None
        times = [horizon]
        times.extend(event['time'] for event in self.environment.scenario.get('events', []))
        times.extend(point for window in self.environment.scenario.get('windows', [])
                     for point in (window['start'], window['end']))
        if self.early_failure:
            # The first second after the shortest possible physical start is
            # a judge boundary, even if no communication window opens then.
            times.extend(row['deadline'] - row['duration'] + 1
                         for row in self.environment.requirements.values()
                         if row.get('required', True))
        for row in self.environment.tasks.values():
            if row['status'] in {'ACCEPTED', 'RUNNING'}:
                times.extend((row['scheduled_start'], row['scheduled_end']))
                if row['valid_until'] is not None:
                    times.append(row['valid_until'])
        if self.router is not None:
            boundary = self.router.next_boundary(self.now)
            if boundary is not None:
                times.append(boundary)
        return min(time for time in times if self.now < time <= horizon)

    def process_events_at(self, second):
        """Apply environment truth and queue only explicitly observed local news."""
        # The stage-5 clock is also usable alone for scheduler tests. A full
        # native scenario mounts its router before the first clock tick.
        if self.router is None:
            return
        for index, event in enumerate(self.environment.scenario.get('events', [])):
            if event['time'] != second or index in self.processed_external_events:
                continue
            kind = event['type']
            if kind == 'contact_change':
                if self.router is None:
                    raise ClockConflict('contact change requires native window router')
                self.router.schedule.replace(event['sender'], event['receiver'], event['intervals'])
                self._event('physical_contact_changed', event['sender'],
                            receiver_id=event['receiver'], event_index=index)
            elif kind == 'prediction_update':
                if self.router is None:
                    raise ClockConflict('prediction update requires native event transport')
                for recipient in event['observers']:
                    observed = {key: deepcopy(event[key]) for key in
                                ('time', 'type', 'sender', 'receiver', 'intervals', 'reason') if key in event}
                    self.router.enqueue_external(recipient, observed, event_index=index)
                self._event('external_prediction_observed', event_index=index,
                            observers=list(event['observers']))
            elif kind == 'objective_update':
                if self.router is None:
                    raise ClockConflict('objective update requires native event transport')
                initiators = [name for name, agent in self.environment.agents.items()
                              if 'INITIATOR' in agent.get('roles', [])]
                if len(initiators) != 1:
                    raise ClockConflict('objective update requires exactly one initiator')
                self.environment.apply_objective_update(event['payload'])
                self.router.enqueue_external(initiators[0],
                    {'time': second, 'type': kind, 'payload': deepcopy(event['payload'])},
                    event_index=index)
                self._event('external_objective_observed', initiators[0], event_index=index)
            else:
                raise ClockConflict('unsupported native external event: ' + str(kind))
            self.processed_external_events.add(index)

    def advance(self, target=None):
        if self.fault is not None:
            raise ClockConflict('technical fault; clock advancement stopped')
        if any(row['state'] == 'active' for row in self.activities.values()):
            raise ClockConflict('native decision still active at current simulation second')
        if self.pending_wakes:
            raise ClockConflict('completed tool or Server response must wake its native action first')
        if self.router is not None and self.router.blocking_ack_count():
            raise ClockConflict('delivered native messages require Agent acknowledgement')
        if self.router is not None and any(event['time'] == self.now and index not in self.processed_external_events
                                           for index, event in enumerate(self.environment.scenario.get('events', []))):
            raise ClockConflict('settle current-second external events before advancing')
        if self.router is not None and self.router.has_due(self.now):
            raise ClockConflict('settle current-second native traffic before advancing')
        boundary = self.next_boundary()
        if boundary is None:
            if target is None:
                return self.snapshot()
            raise ClockConflict('experiment horizon reached')
        if target is None:
            target = boundary
        if type(target) is not int or not self.now < target <= boundary:
            raise ClockConflict(f'clock cannot skip the next boundary at t={boundary}')
        before = {(row['agent_id'], row['assignment_id']) for row in self.environment.tasks.values()
                  if row['status'] in TERMINAL}
        self.environment.advance(target, before_second=self.process_events_at)
        for (agent_id, activity_id), row in self.activities.items():
            assignment_id = row['assignment_id']
            if row['state'] == 'waiting' and row['wait_kind'] == 'tool' and (agent_id, assignment_id) not in before:
                task = self.environment.status(agent_id=agent_id, assignment_id=assignment_id)
                if task['status'] in TERMINAL:
                    self.pending_wakes.add((agent_id, activity_id))
        self._event('simulation_clock_advanced', boundary=boundary)
        return self.snapshot()

    def snapshot(self):
        return {'sim_time': self.now,
                'active_decisions': sum(row['state'] == 'active' for row in self.activities.values()),
                'waiting_tools': sum(row['state'] == 'waiting' for row in self.activities.values()),
                'pending_wakes': len(self.pending_wakes),
                'pending_deliveries': self.router.blocking_ack_count() if self.router else 0,
                'buffered_deliveries': len(self.router.pending_acks) if self.router else 0,
                'fault': self.fault,
                'next_boundary': self.next_boundary()}
