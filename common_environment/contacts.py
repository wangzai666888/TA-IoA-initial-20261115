from copy import deepcopy
from dataclasses import dataclass


@dataclass
class ContactWindow:
    sender_id: str
    receiver_id: str
    start: int
    end: int

    def __post_init__(self):
        if type(self.start) is not int or type(self.end) is not int or not 0 <= self.start < self.end:
            raise ValueError("contact windows require integer 0 <= start < end")


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
