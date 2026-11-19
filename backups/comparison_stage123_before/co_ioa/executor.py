from copy import deepcopy
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
        elif not self.agent.catalog.supports(self.agent.capabilities, task.capability_description):
            reason = "NO_CAPABILITY_MATCH"
        elif (not message.payload.get("inputs_available")
              or any(dep not in message.payload.get("inputs", {}) for dep in task.dependencies)):
            reason = "missing_dependency_inputs"
        actual_start = max(now, a.planned_start_time)
        duration = task.duration + self.agent.config.get("execution_delay", 0)
        actual_end = actual_start + duration
        if actual_end > task.deadline:
            reason = "late_delivery_or_execution_deadline"
        if a.valid_until is not None and actual_end > a.valid_until:
            reason = "execution_validity_limit"
        if any(e["status"] in {TaskStatus.ACCEPTED, TaskStatus.RUNNING, TaskStatus.CANCEL_PENDING}
               and actual_start < e["scheduled_end"] and e["scheduled_start"] < actual_end
               for e in store.executions.values()):
            reason = "local_resource_conflict"
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
        if execution and execution["status"] == TaskStatus.RUNNING and self.agent.config.get("uncancellable", False):
            self.respond("TASK_PROGRESS", execution, status="RUNNING", reason="uncancellable")
            return
        if execution and execution["status"] not in {TaskStatus.COMPLETED, TaskStatus.FAILED}:
            execution["status"] = TaskStatus.CANCELLED
            self.agent.sim.log("task_cancelled", self.agent.agent_id, assignment_id=aid, task_id=execution["assignment"].task_id)
        self.agent.wrapper.send(message.sender_id, "TASK_CANCEL_ACK", {"assignment_id": aid,
            "task_id": message.payload["task_id"], "stopped": execution is None or execution["status"] != TaskStatus.RUNNING},
            correlation_id=message.message_id)

    def complete(self, now):
        changed = False
        for aid, e in self.agent.store.executions.items():
            if e["status"] != TaskStatus.RUNNING:
                continue
            if e["assignment"].valid_until is not None and now >= e["assignment"].valid_until and now < e["scheduled_end"]:
                e["status"] = TaskStatus.FAILED
                self.respond("TASK_FAILED", e, reason="valid_until", actual_start_time=e["actual_start_time"], actual_end_time=now)
                self.agent.sim.log("task_failed", self.agent.agent_id, task_id=e["assignment"].task_id, assignment_id=aid, reason="valid_until")
                changed = True
            elif now >= e["scheduled_end"]:
                task = e["requirement"]
                failed = task.task_id in self.agent.config.get("fail_tasks", [])
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
