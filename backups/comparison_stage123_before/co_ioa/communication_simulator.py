from copy import deepcopy
from .models import ContactWindow, MessageStatus, primitive


class ContactSchedule:
    """Each instance is either physical truth or one node's prediction copy."""
    def __init__(self, windows=(), latency=0):
        if type(latency) is not int or latency < 0:
            raise ValueError("latency must be a nonnegative integer")
        self.windows = [ContactWindow(**w) if isinstance(w, dict) else deepcopy(w) for w in windows]
        self.latency = latency
        self.version = 0

    def next_delivery(self, sender, receiver, now):
        if sender == receiver:
            return now, now
        candidates = []
        for window in self.windows:
            if (window.sender_id, window.receiver_id) != (sender, receiver):
                continue
            start = max(now, window.start)
            # Positive transmissions occupy [start, arrival); zero-time delivery
            # still requires a start strictly before the window's end.
            if start < window.end and start + self.latency <= window.end:
                candidates.append((start, start + self.latency))
        return min(candidates, default=None)

    def replace(self, sender, receiver, intervals):
        self.windows = [w for w in self.windows if (w.sender_id, w.receiver_id) != (sender, receiver)]
        self.windows.extend(ContactWindow(sender, receiver, start, end) for start, end in intervals)
        self.version += 1


class CommunicationSimulator:
    def __init__(self, simulation, windows, latency=0):
        self.sim = simulation
        self.truth = ContactSchedule(windows, latency)
        self.inflight = []

    def transmit(self, wrapper, row, now):
        message = row["message"]
        opportunity = self.truth.next_delivery(message.sender_id, message.receiver_id, now)
        if opportunity is None or opportunity[0] != now:
            return False
        arrival = opportunity[1]
        row["status"] = MessageStatus.SENT
        row["sent_at"] = now
        self.inflight.append((arrival, now, deepcopy(message)))
        self.sim.log("message_sent", message.sender_id, message=message, arrives_at=arrival)
        return True

    def deliver(self, now):
        ready = [row for row in self.inflight if row[0] <= now]
        self.inflight = [row for row in self.inflight if row[0] > now]
        for arrival, sent_at, message in ready:
            if message.sender_id != message.receiver_id and not any(
                w.sender_id == message.sender_id and w.receiver_id == message.receiver_id
                and w.start <= sent_at < w.end and arrival <= w.end for w in self.truth.windows):
                self.sim.log("transmission_interrupted", message.sender_id, message=message,
                             reason="physical window changed during transmission")
                continue
            if message.expires_at is not None and arrival >= message.expires_at:
                self.sim.log("message_expired", message.receiver_id, message=message)
                self.sim.message_counts["expired"] += 1
            else:
                self.deliver_message(message, arrival)
                self.sim.message_counts["delivered"] += 1
                self.sim.log("message_delivered", message.receiver_id, message=message)
        return bool(ready)

    def deliver_message(self, message, arrival):
        if self.sim.infrastructure is not None and message.sender_id != message.receiver_id:
            self.sim.infrastructure.deliver(message, arrival)
        else:
            # Self-delivery is a local operation and requires no network window.
            self.sim.agents[message.receiver_id].runtime.enqueue(primitive(message))
            self.sim.call_agent(message.receiver_id, "receive", {"message_id": message.message_id, "arrival": arrival})
