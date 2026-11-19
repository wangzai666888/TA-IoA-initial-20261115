from dataclasses import dataclass, field
from .registry import Registry
from .reservation_ledger import ReservationLedger


@dataclass
class StateStore:
    registry: Registry = field(default_factory=Registry)
    ledger: ReservationLedger = field(default_factory=ReservationLedger)
    collaborations: dict = field(default_factory=dict)
    executions: dict = field(default_factory=dict)
    archived_results: dict = field(default_factory=dict)
    messages: list = field(default_factory=list)
    processed_message_ids: set = field(default_factory=set)
    processed_external_event_ids: set = field(default_factory=set)
    assignment_versions: dict = field(default_factory=dict)
    group_capabilities: dict = field(default_factory=dict)
    counters: dict = field(default_factory=lambda: {"P": 0, "C": 0, "server_requests": 0})
