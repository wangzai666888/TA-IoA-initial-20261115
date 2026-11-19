"""Pure physical admission/lifecycle rules shared by every method adapter.

No message types, assignment versions, planning, matching, ACK or global state.
Adapters own protocol validation, persistence and feedback delivery.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Admission:
    start: int
    end: int
    eligibility_reason: str | None
    timing_reason: str | None


def assess_execution(*, now, requested_start, duration, execution_delay, deadline,
                     valid_until, capable, inputs_available, dependencies, inputs,
                     busy_intervals, earliest_start=0):
    eligibility = None
    if not capable:
        eligibility = "NO_CAPABILITY_MATCH"
    elif not inputs_available or any(dep not in inputs for dep in dependencies):
        eligibility = "missing_dependency_inputs"
    start = max(now, requested_start, earliest_start)
    end = start + duration + execution_delay
    timing = None
    if end > deadline:
        timing = "late_delivery_or_execution_deadline"
    if valid_until is not None and end > valid_until:
        timing = "execution_validity_limit"
    if any(start < busy_end and busy_start < end for busy_start, busy_end in busy_intervals):
        timing = "local_resource_conflict"
    return Admission(start, end, eligibility, timing)


def completion_at(*, now, scheduled_end, valid_until, configured_failure=False):
    if valid_until is not None and now >= valid_until and now < scheduled_end:
        return "FAILED", "valid_until"
    if now >= scheduled_end:
        return ("FAILED", "configured_execution_failure") if configured_failure else ("COMPLETED", None)
    return "RUNNING", None


def cancellation_status(status, uncancellable):
    if status == "RUNNING" and uncancellable:
        return status, False
    if status not in {"COMPLETED", "FAILED"}:
        return "CANCELLED", True
    return status, True
