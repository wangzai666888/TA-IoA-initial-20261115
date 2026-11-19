"""Directed-window transport for native IoA group and Server business traffic.

The upstream CommunicationLayer still chooses teammates, message content and
synchronous/asynchronous assignment. This router changes only when each native
message/request/reply becomes visible to its recipient.
"""
from copy import deepcopy
from dataclasses import dataclass
from uuid import uuid4

from .contacts import ContactSchedule


@dataclass
class Transit:
    transit_id: str
    sender: str
    receiver: str
    kind: str
    payload: dict
    created_at: int
    sequence: int
    status: str = 'QUEUED'
    sent_at: int | None = None
    arrival_at: int | None = None


class NativeWindowRouter:
    def __init__(self, environment, *, clock, forward_http, deliver_ws):
        self.environment = environment
        self.clock = clock
        self.schedule = ContactSchedule(environment.scenario['windows'],
                                        environment.scenario.get('latency', 0))
        self.server_id = environment.scenario['server_id']
        self.forward_http = forward_http
        self.deliver_ws = deliver_ws
        self.sessions = {}
        self.chat_records = {}
        self.transits = []
        self.requests = {}
        self.pending_acks = {}  # delivery ID -> recipient
        self.events = []
        self.sequence = 0

    @property
    def now(self):
        return self.environment.now

    def _event(self, kind, transit=None, **extra):
        row = {'sim_time': self.now, 'event_type': kind, **extra}
        if transit is not None:
            row.update(transit_id=transit.transit_id, sender_id=transit.sender,
                       receiver_id=transit.receiver, kind=transit.kind,
                       created_at=transit.created_at)
        self.events.append(row)

    def _queue(self, sender, receiver, kind, payload):
        if sender not in self.environment.agents or receiver not in self.environment.agents:
            raise ValueError('unknown sender or receiver')
        self.sequence += 1
        row = Transit(uuid4().hex, sender, receiver, kind, deepcopy(payload),
                      self.now, self.sequence)
        self.transits.append(row)
        self._event('native_message_generated', row)
        return row

    def enqueue_group(self, sender, message):
        if not isinstance(message, dict) or message.get('sender') != sender:
            raise ValueError('group message sender differs from authenticated Agent')
        comm_id = message.get('comm_id')
        members = self.sessions.get(comm_id)
        if not members or sender not in members:
            raise ValueError('unknown native session or sender is not a member')
        if sender == self.server_id:
            self.chat_records[comm_id]['chat_record'].append(deepcopy(message))
            return [self._queue(sender, recipient, 'group',
                                {'message': message, 'origin': sender})
                    for recipient in members]
        # Original IoA sends group traffic to its Server, which then copies it
        # to the group. Physical uplink and each Server downlink are separate
        # directed contacts; the sender's local processing echo stays local.
        rows = [self._queue(sender, sender, 'group',
                            {'message': message, 'origin': sender})]
        rows.append(self._queue(sender, self.server_id, 'group_uplink',
                                {'message': message, 'origin': sender}))
        return rows

    def enqueue_external(self, recipient, event, *, event_index):
        if recipient not in self.environment.agents:
            raise ValueError('external event has unknown observer')
        row = self._queue(recipient, recipient, 'external_event',
                          {'event': deepcopy(event), 'event_index': event_index})
        self._event('native_external_event_queued', row, event_index=event_index)
        return row

    def submit_http(self, sender, operation, payload):
        if operation not in {'retrieve_assistant', 'query_assistant', 'teamup'}:
            raise ValueError('unknown native Server operation')
        if operation != 'query_assistant' and payload.get('sender') != sender:
            raise ValueError('Server request sender differs from authenticated Agent')
        ticket = uuid4().hex
        self.requests[ticket] = {'agent_id': sender, 'operation': operation,
                                'status': 'PENDING', 'result': None, 'reason': None}
        self._queue(sender, self.server_id, 'http_request',
                    {'ticket': ticket, 'operation': operation, 'body': payload})
        return ticket

    def request_status(self, sender, ticket):
        row = self.requests.get(ticket)
        if row is None or row['agent_id'] != sender:
            raise KeyError('unknown native Server request for Agent')
        return deepcopy(row)

    def blocking_ack_count(self):
        # A native synchronous assignment occupies that Agent's listener.
        # Delivered frames remain in its local socket mailbox; waiting for an
        # immediate ACK would stop the task clock before it can finish.
        return sum(not self.clock.listener_waiting(recipient)
                   for recipient in self.pending_acks.values())

    def ack(self, recipient, transit_id):
        if self.pending_acks.get(transit_id) != recipient:
            raise ValueError('unknown delivery acknowledgement for Agent')
        del self.pending_acks[transit_id]
        self._event('native_delivery_acknowledged', transit_id=transit_id,
                    receiver_id=recipient)

    def next_boundary(self, now):
        times = [row.arrival_at for row in self.transits
                 if row.status == 'SENT' and row.arrival_at is not None and row.arrival_at > now]
        for row in self.transits:
            if row.status != 'QUEUED':
                continue
            opportunity = self.schedule.next_delivery(row.sender, row.receiver, now)
            if opportunity and opportunity[0] > now:
                times.append(opportunity[0])
        return min(times, default=None)

    def has_due(self, now):
        return any(row.status == 'SENT' and row.arrival_at <= now or
                   row.status == 'QUEUED' and
                   (opportunity := self.schedule.next_delivery(row.sender, row.receiver, now)) is not None
                   and opportunity[0] == now for row in self.transits)

    async def settle(self, now):
        if now != self.now:
            raise ValueError('router must settle the current simulation second')
        self.clock.process_events_at(now)
        for _ in range(self.environment.scenario.get('same_time_limit', 1000)):
            changed = False
            for row in sorted(self.transits,
                              key=lambda item: (item.kind != 'external_event', item.sequence)):
                if row.status != 'QUEUED':
                    continue
                opportunity = self.schedule.next_delivery(row.sender, row.receiver, now)
                if opportunity is None or opportunity[0] != now:
                    continue
                row.status, row.sent_at, row.arrival_at = 'SENT', now, opportunity[1]
                self._event('native_message_sent', row, arrival_at=row.arrival_at)
                changed = True
            for row in sorted(self.transits,
                              key=lambda item: (item.kind != 'external_event', item.sequence)):
                if row.status != 'SENT' or row.arrival_at > now:
                    continue
                if row.sender != row.receiver and not any(
                    window.sender_id == row.sender and window.receiver_id == row.receiver
                    and window.start <= row.sent_at < window.end and row.arrival_at <= window.end
                    for window in self.schedule.windows):
                    row.status = 'INTERRUPTED'
                    self._event('native_transmission_interrupted', row)
                    changed = True
                    continue
                row.status = 'DELIVERED'
                self._event('native_message_delivered', row)
                changed = True
                if row.kind == 'group':
                    self.pending_acks[row.transit_id] = row.receiver
                    frame = dict(row.payload['message'])
                    frame['_sim_delivery_id'] = row.transit_id
                    frame['_sim_created_at'] = row.payload.get('origin_created_at', row.created_at)
                    frame['_sim_delivered_at'] = now
                    await self.deliver_ws(row.receiver, frame)
                elif row.kind == 'group_uplink':
                    message = row.payload['message']
                    comm_id = message['comm_id']
                    self.chat_records[comm_id]['chat_record'].append(deepcopy(message))
                    for recipient in self.sessions[comm_id]:
                        if recipient != row.sender:
                            self._queue(self.server_id, recipient, 'group',
                                {'message': message, 'origin': row.sender,
                                 'origin_created_at': row.created_at})
                elif row.kind == 'external_event':
                    self.pending_acks[row.transit_id] = row.receiver
                    await self.deliver_ws(row.receiver, {
                        '_sim_kind': 'external_event',
                        '_sim_delivery_id': row.transit_id,
                        '_sim_created_at': row.created_at,
                        '_sim_delivered_at': now,
                        'event': deepcopy(row.payload['event']),
                    })
                elif row.kind == 'http_request':
                    ticket = row.payload['ticket']
                    try:
                        result = await self.forward_http(row.payload['operation'], row.payload['body'])
                        if row.payload['operation'] == 'teamup':
                            self._remember_session(result, row.payload['body'])
                        reply = {'ticket': ticket, 'status': 'READY', 'result': result}
                    except Exception as exc:
                        reply = {'ticket': ticket, 'status': 'ERROR',
                                 'reason': type(exc).__name__ + ': ' + str(exc)}
                    self._queue(self.server_id, row.sender, 'http_response', reply)
                elif row.kind == 'http_response':
                    ticket = row.payload['ticket']
                    self.requests[ticket].update(status=row.payload['status'],
                                                  result=row.payload.get('result'),
                                                  reason=row.payload.get('reason'))
                    self.clock.wake_message(row.receiver, ticket)
            if not changed:
                return
        raise RuntimeError('native same-time routing exceeded guard')

    def _remember_session(self, result, request):
        comm_id = result['comm_id']
        members = list(dict.fromkeys(result['agent_names']))
        if request['sender'] not in members:
            raise ValueError('native Server teamup omitted initiating Agent')
        self.sessions[comm_id] = members
        self.chat_records[comm_id] = {'comm_id': comm_id, 'agent_names': members,
                                      'team_name': request.get('team_name'),
                                      'chat_record': []}
