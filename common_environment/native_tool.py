"""Physical tool boundary for a native IoA execution Agent.

The service owns scenario truth. Native Agents submit identifiers and received
input receipts; they cannot choose duration, capability, deadline or outcome.
The clock is advanced only by the privileged simulation driver (stage 5).
"""
from copy import deepcopy
import secrets

from .execution import assess_execution, cancellation_status, completion_at

TERMINAL = {"COMPLETED", "FAILED", "REJECTED", "CANCELLED"}


class NativeSimulationTools:
    def __init__(self, scenario):
        self.scenario = deepcopy(scenario)
        self.now = 0
        self.requirements = {row["task_id"]: row for row in scenario["requirements"]}
        self.cancelled_at = None
        self.agents = {row["id"]: row for row in scenario["agents"]}
        self.aliases = {alias.casefold(): capability
                        for capability, spec in scenario["capability_templates"].items()
                        for alias in [capability, *spec.get("aliases", [])]}
        self.tasks = {}
        self.receipts = {}  # opaque receipt -> completed task ID
        self.events = []

    def _record(self, row, kind, **details):
        self.events.append({"sim_time": self.now, "event_type": kind,
                            "agent_id": row["agent_id"], "task_id": row["task_id"],
                            "assignment_id": row["assignment_id"], **details})

    def _view(self, row):
        return deepcopy({key: value for key, value in row.items() if key != "fingerprint"})

    def submit(self, *, agent_id, task_id, assignment_id, requested_start=0,
               inputs=None, valid_until=None, revision_index=None):
        if agent_id not in self.agents or task_id not in self.requirements:
            raise ValueError("unknown agent or task")
        if not isinstance(assignment_id, str) or not assignment_id or len(assignment_id) > 128:
            raise ValueError("invalid assignment_id")
        if type(requested_start) is not int or requested_start < 0:
            raise ValueError("requested_start must be a nonnegative integer")
        if valid_until is not None and (type(valid_until) is not int or valid_until < 0):
            raise ValueError("valid_until must be a nonnegative integer or null")
        if revision_index is not None and (type(revision_index) is not int or revision_index < 0):
            raise ValueError("revision_index must be a nonnegative integer or null")
        if not isinstance(inputs, dict) and inputs is not None:
            raise ValueError("inputs must be a mapping of dependency IDs to receipts")
        inputs = dict(inputs or {})
        if any(not isinstance(key, str) or not isinstance(value, str) for key, value in inputs.items()):
            raise ValueError("dependency IDs and receipts must be strings")
        key = (agent_id, assignment_id)
        fingerprint = (task_id, requested_start, tuple(sorted(inputs.items())), valid_until, revision_index)
        if key in self.tasks:
            if self.tasks[key]["fingerprint"] != fingerprint:
                raise ValueError("conflicting duplicate assignment")
            return self._view(self.tasks[key])
        if self.cancelled_at is not None:
            raise ValueError("objective has been cancelled")
        if self.now > self.scenario.get("max_time", 1200):
            raise ValueError("experiment horizon already passed")
        requirement = self.requirements[task_id]
        if revision_index is not None:
            candidates = self.scenario.get('revisions', [])
            if (not self.scenario.get('adjustment_policy', {}).get('allow_revision')
                    or revision_index >= len(candidates)):
                raise ValueError('revision is not authorized by scenario')
            revised = {row['task_id']: row for row in candidates[revision_index]}
            if task_id not in revised:
                raise ValueError('task absent from authorized revision')
            requirement = revised[task_id]
        agent = self.agents[agent_id]
        capability = self.aliases.get(requirement["capability_description"].casefold())
        dependencies = requirement.get("dependencies", [])
        inputs_available = all(self.receipts.get(inputs.get(dep)) == dep for dep in dependencies)
        busy = [(row["scheduled_start"], row["scheduled_end"])
                for row in self.tasks.values() if row["agent_id"] == agent_id
                and row["status"] in {"ACCEPTED", "RUNNING"}]
        admission = assess_execution(
            now=self.now, requested_start=requested_start,
            duration=requirement["duration"],
            execution_delay=agent.get("execution_delay", 0),
            earliest_start=requirement.get("earliest_start", 0),
            deadline=requirement["deadline"], valid_until=valid_until,
            capable=capability in agent.get("capabilities", []),
            inputs_available=inputs_available,
            dependencies=dependencies, inputs=inputs, busy_intervals=busy)
        reason = admission.timing_reason or admission.eligibility_reason
        row = {"agent_id": agent_id, "task_id": task_id, "assignment_id": assignment_id,
               "status": "REJECTED" if reason else "ACCEPTED", "reason": reason,
               "accepted_at": None if reason else self.now,
               "scheduled_start": admission.start, "scheduled_end": admission.end,
               "actual_start_time": None, "actual_end_time": None, "result": None,
               "valid_until": valid_until, "inputs": inputs, "fingerprint": fingerprint,
               "revision_index": revision_index,
               "requirement_snapshot": deepcopy(requirement)}
        self.tasks[key] = row
        self._record(row, "task_rejected" if reason else "task_accepted", reason=reason)
        if not reason:
            self._advance_one(self.now)
        return self._view(row)

    def _advance_one(self, now):
        self.now = now
        for row in self.tasks.values():
            if row["status"] == "ACCEPTED" and now >= row["scheduled_start"]:
                row["status"] = "RUNNING"
                row["actual_start_time"] = now
                self._record(row, "task_started")
            if row["status"] != "RUNNING":
                continue
            requirement = row['requirement_snapshot']
            agent = self.agents[row["agent_id"]]
            status, reason = completion_at(
                now=now, scheduled_end=row["scheduled_end"],
                valid_until=row["valid_until"],
                configured_failure=row["task_id"] in agent.get("fail_tasks", []))
            if status == "RUNNING":
                continue
            row["status"], row["reason"], row["actual_end_time"] = status, reason, now
            if status == "COMPLETED":
                receipt = secrets.token_urlsafe(24)
                self.receipts[receipt] = row["task_id"]
                row["result"] = {"capability": requirement["capability_description"],
                                 "executor_id": row["agent_id"],
                                 "parameters": deepcopy(requirement.get("parameters", {})),
                                 "receipt": receipt}
            self._record(row, "task_completed" if status == "COMPLETED" else "task_failed", reason=reason)

    def advance(self, now, *, before_second=None):
        if type(now) is not int or now < self.now or now > self.scenario.get("max_time", 1200):
            raise ValueError("clock must advance monotonically within scenario horizon")
        for second in range(self.now + 1, now + 1):
            self.now = second
            if before_second is not None:
                before_second(second)
            self._advance_one(second)
        return {"sim_time": self.now, "events": len(self.events)}

    def status(self, *, agent_id, assignment_id):
        row = self.tasks.get((agent_id, assignment_id))
        if row is None:
            raise KeyError("unknown assignment for agent")
        return self._view(row)

    def apply_objective_update(self, payload):
        """Change physical admissibility at the event tick; Agent knowledge is separate."""
        if payload.get('cancel'):
            self.cancelled_at = self.now
        if 'requirements' in payload:
            rows = payload['requirements']
            if not isinstance(rows, list) or len({r['task_id'] for r in rows}) != len(rows):
                raise ValueError('objective update has invalid requirements')
            self.requirements = {row['task_id']: deepcopy(row) for row in rows}
        self.events.append({'sim_time': self.now, 'event_type': 'physical_objective_updated',
                            'cancelled': bool(payload.get('cancel')),
                            'task_ids': sorted(self.requirements)})

    def cancel(self, *, agent_id, assignment_id):
        row = self.tasks.get((agent_id, assignment_id))
        if row is None:
            raise KeyError("unknown assignment for agent")
        new_status, stopped = cancellation_status(
            row["status"], self.agents[agent_id].get("uncancellable", False))
        if stopped and row["status"] not in TERMINAL:
            row["status"], row["reason"], row["actual_end_time"] = new_status, "cancelled", self.now
            self._record(row, "task_cancelled")
        return {"stopped": stopped, **self._view(row)}
