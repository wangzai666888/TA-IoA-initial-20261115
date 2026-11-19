from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class CollaborationPhase(StrEnum):
    P = "planning"
    C = "checking"
    A = "active"
    E = "ended"


class TaskStatus(StrEnum):
    PENDING = "PENDING"
    ASSIGNED = "ASSIGNED"
    ACCEPTED = "ACCEPTED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"


class MessageStatus(StrEnum):
    QUEUED = "QUEUED"
    SENT = "SENT"
    DELIVERED = "DELIVERED"
    EXPIRED = "EXPIRED"


@dataclass
class ContactWindow:
    sender_id: str
    receiver_id: str
    start: int
    end: int

    def __post_init__(self):
        if type(self.start) is not int or type(self.end) is not int or not 0 <= self.start < self.end:
            raise ValueError("contact windows require integer 0 <= start < end")


@dataclass
class AgentRegistryEntry:
    capability_description: str
    next_available_contact_time: int | None


@dataclass
class TaskRequirement:
    task_id: str
    capability_description: str
    earliest_start: int
    deadline: int
    duration: int
    dependencies: list[str] = field(default_factory=list)
    parameters: dict = field(default_factory=dict)
    required: bool = True


@dataclass
class TaskAssignment:
    assignment_id: str
    task_id: str
    executor_id: str
    dispatch_agent_id: str
    planned_dispatch_time: int
    planned_start_time: int
    planned_end_time: int
    requirement_version: int
    plan_version: int
    valid_until: int | None = None


@dataclass
class TaskRecord:
    requirement: TaskRequirement
    assignment: TaskAssignment | None = None
    status: TaskStatus = TaskStatus.PENDING
    wait_reason: str | None = None
    actual_start_time: int | None = None
    actual_end_time: int | None = None
    result: dict | None = None
    result_received_at: int | None = None
    history: list[dict] = field(default_factory=list)


@dataclass
class MemberState:
    agent_id: str
    capability_description: str
    next_available_contact_time: int | None
    assigned_tasks: list[str] = field(default_factory=list)
    last_updated_at: int = 0
    reachability: str = "unknown"


@dataclass
class Collaboration:
    collaboration_id: str
    initiator_id: str
    coordinator_id: str | None
    phase: CollaborationPhase
    objective: str
    hard_constraints: dict
    adjustment_policy: dict
    requirements: dict[str, TaskRequirement]
    task_records: dict[str, TaskRecord]
    member_states: dict[str, MemberState] = field(default_factory=dict)
    requirement_version: int = 1
    plan_version: int = 0
    pending_requests: dict = field(default_factory=dict)
    processed_event_ids: set[str] = field(default_factory=set)
    outcome: str | None = None
    reason: str | None = None
    error_kind: str | None = None


@dataclass
class Message:
    message_id: str
    collaboration_id: str | None
    sender_id: str
    receiver_id: str
    message_type: str
    created_at: int
    expires_at: int | None
    requirement_version: int | None
    plan_version: int | None
    correlation_id: str | None
    payload: dict


@dataclass
class MatchResult:
    status: str
    assignments: list[TaskAssignment] = field(default_factory=list)
    deferred_task_ids: list[str] = field(default_factory=list)
    coordinator_id: str | None = None
    reason_codes: list[str] = field(default_factory=list)
    explanation: str = ""


def primitive(value: Any):
    if hasattr(value, "__dataclass_fields__"):
        return primitive(asdict(value))
    if isinstance(value, dict):
        return {k: primitive(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [primitive(v) for v in (sorted(value) if isinstance(value, set) else value)]
    return value
