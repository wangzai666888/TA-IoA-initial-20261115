from copy import deepcopy
from common_environment.execution import assess_execution, completion_at, cancellation_status
from .models import TaskAssignment, TaskRequirement, TaskStatus, primitive


class CapabilityExecutor:
    def __init__(self, agent):
        self.agent = agent

    def respond(self, kind, execution, **payload):
        a = execution["assignment"]
        self.agent.wrapper.send(a.dispatch_agent_id, kind,
            {"assignment_id": a.assignment_id, "task_id": a.task_id, **payload},
            requirement_version=a.requirement_version, plan_version=a.plan_version)

    def accept(self, message):
        a = TaskAssignment(**message.payload["assignment"])
        task = TaskRequirement(**message.payload["requirement"])
        store, now = self.agent.store, self.agent.sim.now
        if a.assignment_id in store.executions:
            self.agent.sim.log("duplicate_assignment_ignored", self.agent.agent_id, assignment_id=a.assignment_id, task_id=a.task_id)
            return
        key = (message.collaboration_id, a.task_id)
        version = (a.requirement_version, a.plan_version)
        reason = None
        if a.executor_id != self.agent.agent_id or a.dispatch_agent_id != message.sender_id:
            reason = "invalid_assignment_identity"
        elif version < store.assignment_versions.get(key, (0, 0)):
            reason = "old_assignment_version"
        assessment = assess_execution(now=now, requested_start=a.planned_start_time,
            duration=task.duration, earliest_start=task.earliest_start, execution_delay=self.agent.config.get("execution_delay", 0),
            deadline=task.deadline, valid_until=a.valid_until,
            capable=self.agent.catalog.supports(self.agent.capabilities, task.capability_description),
            inputs_available=message.payload.get("inputs_available"), dependencies=task.dependencies,
            inputs=message.payload.get("inputs", {}),
            busy_intervals=[(e["scheduled_start"], e["scheduled_end"]) for e in store.executions.values()
                if e["status"] in {TaskStatus.ACCEPTED, TaskStatus.RUNNING, TaskStatus.CANCEL_PENDING}])
        reason = assessment.timing_reason or reason or assessment.eligibility_reason
        actual_start, actual_end = assessment.start, assessment.end
        execution = {"assignment": a, "requirement": task, "status": TaskStatus.ACCEPTED,
                     "scheduled_start": actual_start, "scheduled_end": actual_end,
                     "actual_start_time": None, "actual_end_time": None,
                     "result": None, "inputs": deepcopy(message.payload.get("inputs", {})),
                     "coordinator_id": message.sender_id, "collaboration_id": message.collaboration_id}
        if reason:
            self.respond("TASK_REJECT", execution, reason=reason)
            self.agent.sim.log("task_rejected", self.agent.agent_id, assignment_id=a.assignment_id, task_id=a.task_id, reason=reason)
            return
        store.assignment_versions[key] = max(version, store.assignment_versions.get(key, (0, 0)))
        store.executions[a.assignment_id] = execution
        self.respond("TASK_ACK", execution, accepted_at=now)
        self.agent.sim.log("task_accepted", self.agent.agent_id, assignment_id=a.assignment_id, task_id=a.task_id)

    def cancel(self, message):
        aid = message.payload["assignment_id"]
        execution = self.agent.store.executions.get(aid)
        if execution is not None and message.sender_id != execution["assignment"].dispatch_agent_id:
            raise ValueError("cancellation source is not dispatcher")
        new_status, stopped = cancellation_status(execution["status"], self.agent.config.get("uncancellable", False)) if execution else (None, True)
        if not stopped:
            self.respond("TASK_PROGRESS", execution, status="RUNNING", reason="uncancellable")
            return
        if execution and execution["status"] not in {TaskStatus.COMPLETED, TaskStatus.FAILED}:
            execution["status"] = TaskStatus(new_status)
            self.agent.sim.log("task_cancelled", self.agent.agent_id, assignment_id=aid, task_id=execution["assignment"].task_id)
        self.agent.wrapper.send(message.sender_id, "TASK_CANCEL_ACK", {"assignment_id": aid,
            "task_id": message.payload["task_id"], "stopped": execution is None or execution["status"] != TaskStatus.RUNNING},
            correlation_id=message.message_id)

    def complete(self, now):
        changed = False
        for aid, e in self.agent.store.executions.items():
            if e["status"] != TaskStatus.RUNNING:
                continue
            physical_status, physical_reason = completion_at(now=now, scheduled_end=e["scheduled_end"],
                valid_until=e["assignment"].valid_until,
                configured_failure=e["requirement"].task_id in self.agent.config.get("fail_tasks", []))
            if physical_reason == "valid_until":
                e["status"] = TaskStatus.FAILED
                self.respond("TASK_FAILED", e, reason="valid_until", actual_start_time=e["actual_start_time"], actual_end_time=now)
                self.agent.sim.log("task_failed", self.agent.agent_id, task_id=e["assignment"].task_id, assignment_id=aid, reason="valid_until")
                changed = True
            elif physical_status in {"COMPLETED", "FAILED"}:
                task = e["requirement"]
                failed = physical_status == "FAILED"
                e["status"] = TaskStatus.FAILED if failed else TaskStatus.COMPLETED
                e["actual_end_time"] = now
                e["result"] = None if failed else {"capability": task.capability_description, "executor_id": self.agent.agent_id,
                                                   "parameters": deepcopy(task.parameters)}
                self.respond("TASK_FAILED" if failed else "TASK_RESULT", e,
                             actual_start_time=e["actual_start_time"], actual_end_time=now,
                             result=e["result"], reason="configured_execution_failure" if failed else None)
                self.agent.sim.log("task_failed" if failed else "task_completed", self.agent.agent_id,
                                   task_id=task.task_id, assignment_id=aid)
                changed = True
        return changed

    def start(self, now):
        changed = False
        for aid, e in self.agent.store.executions.items():
            if e["status"] == TaskStatus.ACCEPTED and now >= e["scheduled_start"]:
                e["status"] = TaskStatus.RUNNING
                e["actual_start_time"] = now
                self.respond("TASK_PROGRESS", e, status="RUNNING", actual_start_time=now)
                self.agent.sim.log("task_started", self.agent.agent_id, task_id=e["assignment"].task_id, assignment_id=aid)
                changed = True
        return changed
