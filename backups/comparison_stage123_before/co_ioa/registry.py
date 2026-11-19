from copy import deepcopy
from .models import AgentRegistryEntry


def next_available_contact(schedule, agent_id, dispatch_agent_id, now, reservations=()):
    """First opportunity when the member is idle and can receive from dispatch."""
    probe = now
    for _ in range(len(reservations) + len(schedule.windows) + 2):
        opportunity = schedule.next_delivery(dispatch_agent_id, agent_id, probe)
        if opportunity is None:
            return None
        start, arrival = opportunity
        overlap = next(((s, e) for s, e in reservations if s <= start < e or s < arrival < e), None)
        if overlap is None:
            return arrival
        probe = overlap[1]
    return None


class Registry:
    def __init__(self):
        self.capabilities = {}
        self.metadata = {}

    def register(self, agent_id, capabilities, now, source):
        self.capabilities[agent_id] = deepcopy(capabilities)
        self.metadata[agent_id] = {"updated_at": now, "source": source}

    def query(self, agent_id, dispatch_agent_id, now, schedule, reservations=()):
        entry = AgentRegistryEntry(", ".join(self.capabilities[agent_id]),
            next_available_contact(schedule, agent_id, dispatch_agent_id, now, reservations))
        metadata = {**self.metadata[agent_id], "dispatch_agent_id": dispatch_agent_id,
                    "prediction_version": schedule.version, "queried_at": now}
        return entry, metadata
