from copy import deepcopy
from .models import (Collaboration, CollaborationPhase as Phase, TaskRequirement, TaskRecord,
                     TaskAssignment, TaskStatus, MemberState, primitive)
from .planner import PlanningContext, validate_requirements, preserve_requirement_names
from .errors import ModelServiceError, PlanningValidationError


class CollaborationController:
    def __init__(self, agent, scenario):
        self.agent, self.scenario = agent, scenario
        self.dirty = False
        self.handover_complete = False
        self.retry_count = 0
        self.last_failure = None
        self.awaiting = None
        self.proposed_coordinator = None
        self.disabled_members = set()

    @property
    def sim(self):
        return self.agent.sim

    @property
    def collab(self):
        return self.agent.store.collaborations.get("mission")

    def log(self, kind, **kwargs):
        self.sim.log(kind, self.agent.agent_id, **kwargs)

    def phase(self, value, reason):
        c = self.collab
        before = c.phase
        c.phase = value
        if before != value:
            self.log("phase_changed", phase_before=before, phase_after=value, reason=reason)

    def send(self, receiver, kind, payload, **kwargs):
        c = self.collab
        return self.agent.wrapper.send(receiver, kind, payload,
            requirement_version=c.requirement_version if c else None,
            plan_version=c.plan_version if c else None, **kwargs)

    def initialize(self):
        self.agent.wrapper.register()
        if "INITIATOR" not in self.agent.roles:
            return
        self.agent.store.collaborations["mission"] = Collaboration("mission", self.agent.agent_id, None,
            Phase.P, self.scenario["objective"], deepcopy(self.scenario.get("hard_constraints", {})),
            deepcopy(self.scenario.get("adjustment_policy", {})), {}, {})
        self.plan({"type": "initial"})
        if self.collab.phase == Phase.E:
            return
        self.awaiting = self.send(self.agent.server_id, "MATCH_REQUEST", {"purpose": "nominate",
            "initiator_id": self.agent.agent_id, "materials": self.materials()})
        self.collab.pending_requests[self.awaiting] = "nomination"

    def materials(self):
        c = self.collab
        return {"objective": c.objective, "initiator_id": c.initiator_id,
                "requirements": primitive(c.requirements), "hard_constraints": c.hard_constraints,
                "adjustment_policy": c.adjustment_policy, "requirement_version": c.requirement_version,
                "plan_version": c.plan_version, "reservations": primitive(self.agent.store.ledger.entries)}

    def plan(self, event):
        c = self.collab
        if c.phase == Phase.E:
            self.log("planning_ignored_after_end", reason=event.get("type"))
            return
        self.agent.store.counters["P"] += 1
        self.phase(Phase.P, event.get("type", "revision"))
        context = PlanningContext(c.objective, c.hard_constraints, c.adjustment_policy,
            deepcopy(c.requirements), [primitive(r.history) for r in c.task_records.values()],
            {k: {"status": r.status, "assignment_sent": bool(r.assignment and
                 not self.agent.wrapper.assignment_unsent(r.assignment.assignment_id))}
             for k, r in c.task_records.items()}, event,
            [] if self.last_failure is None else self.last_failure,
            initial_requirements={r["task_id"]: TaskRequirement(**deepcopy(r)) for r in self.scenario["requirements"]},
            simulation_time=self.sim.now, agent_id=self.agent.agent_id)
        try:
            result = self.agent.planner.plan(context)
            validate_requirements(result.requirements, self.agent.catalog, context)
            result.requirements = preserve_requirement_names(result.requirements, self.agent.catalog,
                c.requirements or context.initial_requirements)
            changed = primitive(result.requirements) != primitive(c.requirements)
            if c.requirements and not changed and event.get("type") == "matching_failed":
                self.end("failed", "no authorized requirement change; bounded retry stopped")
                return
            # Sent assignments may already be executing, even if their ACK is late.
            for key, record in c.task_records.items():
                if (key not in result.requirements or primitive(record.requirement) != primitive(result.requirements[key])):
                    if record.assignment and not self.agent.wrapper.assignment_unsent(record.assignment.assignment_id):
                        raise ValueError("sent assignment requires cancellation acknowledgement before revision")
                    if record.assignment:
                        self.void_unsent(record, "requirement revised")
            if c.requirements and changed:
                c.requirement_version += 1
            for key in list(c.task_records):
                if key not in result.requirements:
                    del c.task_records[key]
            c.requirements = deepcopy(result.requirements)
            for key, task in c.requirements.items():
                if key not in c.task_records:
                    c.task_records[key] = TaskRecord(deepcopy(task))
                else:
                    c.task_records[key].requirement = deepcopy(task)
            self.log("plan_validated", reason=result.change_summary, changed=changed,
                     requirements=primitive(c.requirements))
            self.phase(Phase.C, "requirements validated")
            self.dirty = True
        except (ModelServiceError, PlanningValidationError) as exc:
            c.error_kind = "model_service" if isinstance(exc, ModelServiceError) else "planning_validation"
            self.log("experiment_error", reason=str(exc), error_kind=c.error_kind)
            self.end("error", str(exc))
        except (ValueError, TypeError, KeyError) as exc:
            self.log("plan_rejected", reason=str(exc))
            self.end("failed", str(exc))

    def handle_message(self, message):
        a, c, p = self.agent, self.collab, message.payload
        kind = message.message_type
        if kind == "REGISTER":
            if "SERVER" not in a.roles:
                raise ValueError("registration delivered to nonserver")
            a.store.registry.register(message.sender_id, p["capabilities"], self.sim.now, message.sender_id)
            self.log("agent_registered", member_id=message.sender_id)
            return
        if kind == "MATCH_REQUEST":
            self.server_match(message)
            return
        if kind == "COORDINATOR_HANDOVER":
            if not a.catalog.supports(a.capabilities, "coordinate"):
                raise ValueError("handover receiver lacks coordination capability")
            if c is not None and self.handover_complete:
                return
            requirements = {k: TaskRequirement(**v) for k, v in p["requirements"].items()}
            a.store.collaborations["mission"] = Collaboration("mission", p["initiator_id"], a.agent_id,
                Phase.C, p["objective"], deepcopy(p["hard_constraints"]), deepcopy(p["adjustment_policy"]),
                requirements, {k: TaskRecord(deepcopy(t)) for k, t in requirements.items()},
                requirement_version=p["requirement_version"], plan_version=p["plan_version"])
            self.send(p["initiator_id"], "HANDOVER_ACK", {"coordinator_id": a.agent_id}, correlation_id=message.message_id)
            self.log("handover_received")
            self.dirty = False
            return
        if kind == "HANDOVER_ACK":
            if c is None or c.initiator_id != a.agent_id or message.sender_id != self.proposed_coordinator:
                raise ValueError("unexpected coordinator handover acknowledgement")
            c.coordinator_id = message.sender_id
            self.handover_complete = True
            self.send(message.sender_id, "STATUS_UPDATE", {"authority_committed": True})
            self.log("handover_acknowledged", coordinator_id=message.sender_id)
            self.awaiting = None
            return
        if kind == "STATUS_UPDATE":
            if p.get("authority_committed"):
                if c is None or message.sender_id != c.initiator_id:
                    raise ValueError("invalid authority confirmation")
                self.handover_complete = True
                self.dirty = True
                self.log("coordinator_authority_active")
            if "release" in p and "SERVER" in a.roles:
                for aid in p["release"]:
                    reservation = a.store.ledger.entries.get(aid)
                    if reservation and reservation["collaboration_id"] != message.collaboration_id:
                        raise ValueError("cannot release another collaboration's reservation")
                    a.store.ledger.release(aid)
            return
        if kind == "MATCH_RESPONSE":
            if message.sender_id != a.server_id or message.correlation_id != self.awaiting:
                self.log("stale_match_response_ignored", message=message)
                return
            self.awaiting = None
            c.pending_requests.pop(message.correlation_id, None)
            if p.get("purpose") == "nominate":
                if p.get("coordinator_id") is None:
                    self.end("failed", "no coordinator with a known handover contact")
                    return
                self.proposed_coordinator = p["coordinator_id"]
                self.send(self.proposed_coordinator, "COORDINATOR_HANDOVER", self.materials())
                self.dirty = False
                return
            if p["status"] in {"OPTIMAL", "FEASIBLE"}:
                a.store.group_capabilities.update(deepcopy(p["capabilities"]))
                for name, caps in p["capabilities"].items():
                    entry, _ = self.registry_entry(name, caps)
                    c.member_states[name] = MemberState(name, entry.capability_description,
                        entry.next_available_contact_time, last_updated_at=self.sim.now,
                        reachability="reachable" if entry.next_available_contact_time == self.sim.now else "expected_unreachable")
                self.commit([TaskAssignment(**row) for row in p["assignments"]], "server")
            else:
                self.match_failed(p["reason_codes"], server=True)
            return
        if kind == "TASK_ASSIGN":
            a.executor.accept(message)
            return
        if kind == "TASK_CANCEL":
            a.executor.cancel(message)
            return
        if kind in {"TASK_ACK", "TASK_REJECT", "TASK_PROGRESS", "TASK_RESULT", "TASK_FAILED", "TASK_CANCEL_ACK"}:
            if c is None:
                return
            self.task_feedback(message)
            return
        if kind == "OBJECTIVE_UPDATE":
            if c is None or message.sender_id != c.initiator_id:
                raise ValueError("objective update must come from initiator")
            if p.get("cancel"):
                self.end("cancelled", "initiator cancelled")
            else:
                self.plan({"type": "objective_update", **p})
            return
        if kind == "COLLABORATION_END":
            if c and c.coordinator_id == message.sender_id:
                c.phase, c.outcome, c.reason = Phase.E, p["outcome"], p["reason"]
            self.log("end_notification_received", reason=p["reason"])
            return
        raise ValueError("unsupported message type " + kind)

    def registry_entry(self, name, caps):
        from .models import AgentRegistryEntry
        from .registry import next_available_contact
        return AgentRegistryEntry(", ".join(caps), next_available_contact(self.agent.known_contacts,
            name, self.agent.agent_id, self.sim.now, self.agent.store.ledger.intervals(name))), {}

    def server_match(self, message):
        a, p = self.agent, message.payload
        if "SERVER" not in a.roles:
            raise ValueError("match request delivered to nonserver")
        if p["purpose"] == "nominate":
            options = []
            for name, caps in a.store.registry.capabilities.items():
                if not a.catalog.supports(caps, "coordinate"):
                    continue
                forward = a.known_contacts.next_delivery(p["initiator_id"], name, self.sim.now)
                back = a.known_contacts.next_delivery(name, p["initiator_id"], forward[1]) if forward else None
                if back:
                    load = sum(end-start for start, end in a.store.ledger.intervals(name))
                    options.append((back[1], load, name))
            choice = min(options)[2] if options else None
            a.wrapper.send(message.sender_id, "MATCH_RESPONSE", {"purpose": "nominate", "coordinator_id": choice},
                           correlation_id=message.message_id)
            self.log("coordinator_selected", coordinator_id=choice)
            return
        requirements = {k: TaskRequirement(**v) for k, v in p["requirements"].items()}
        fixed = [TaskAssignment(**row) for row in p.get("fixed", [])]
        caps = {name: values for name, values in a.store.registry.capabilities.items()
                if name not in p.get("disabled_members", [])}
        # A snapshot explicitly carried by the request supplies coordinator-known
        # fixed work; it never reads another node's local execution store.
        from .reservation_ledger import ReservationLedger
        ledger = ReservationLedger()
        ledger.entries = deepcopy(a.store.ledger.entries)
        ledger.rights = deepcopy(a.store.ledger.rights)
        for assignment in fixed:
            if assignment.assignment_id not in ledger.entries:
                ledger.reserve("mission", [assignment])
        result = a.matcher.solve(requirements, caps, a.known_contacts, ledger,
            now=self.sim.now, dispatch_agent_id=message.sender_id,
            completed=p.get("completed", []), fixed=fixed, collaboration_id="mission",
            requirement_version=p["requirement_version"], plan_version=p["plan_version"] + 1,
            previous=[TaskAssignment(**v) for v in p.get("previous", [])], id_factory=self.sim.new_id)
        selected = {row.executor_id for row in result.assignments} | set(self.scenario.get("initial_group", []))
        if result.status in {"OPTIMAL", "FEASIBLE"}:
            a.store.ledger.reserve("mission", result.assignments)
            # Exclusive resource rights for this single-instance prototype.
            for name in selected:
                a.store.ledger.rights[name] = "mission"
        a.wrapper.send(message.sender_id, "MATCH_RESPONSE", {"status": result.status,
            "assignments": primitive(result.assignments), "reason_codes": result.reason_codes,
            "capabilities": {name: caps[name] for name in sorted(selected) if name in caps},
            "deferred_task_ids": result.deferred_task_ids}, correlation_id=message.message_id)
        self.log("server_matching", solver_status=result.status, reason=result.reason_codes)

    def fixed(self):
        return [r.assignment for r in self.collab.task_records.values() if r.assignment is not None
                and r.status in {TaskStatus.ASSIGNED, TaskStatus.ACCEPTED, TaskStatus.RUNNING, TaskStatus.CANCEL_PENDING}]

    def completed(self):
        return [key for key, r in self.collab.task_records.items() if r.status == TaskStatus.COMPLETED]

    def drive(self):
        c, a = self.collab, self.agent
        if c is None or c.phase == Phase.E or not self.handover_complete or c.coordinator_id != a.agent_id:
            return False
        if self.check_success():
            return True
        if not self.dirty or self.awaiting:
            return False
        self.dirty = False
        self.phase(Phase.C, "ready tasks or invalidated allocation")
        a.store.counters["C"] += 1
        caps = {name: values for name, values in a.store.group_capabilities.items() if name not in self.disabled_members}
        result = a.matcher.solve(c.requirements, caps, a.known_contacts, a.store.ledger,
            now=self.sim.now, dispatch_agent_id=a.agent_id, completed=self.completed(), fixed=self.fixed(),
            collaboration_id=c.collaboration_id,
            requirement_version=c.requirement_version, plan_version=c.plan_version+1, id_factory=self.sim.new_id)
        self.log("local_matching", solver_status=result.status, reason=result.reason_codes,
                 deferred_task_ids=result.deferred_task_ids)
        if result.status in {"OPTIMAL", "FEASIBLE"}:
            self.commit(result.assignments, "local")
        else:
            self.last_failure = result.reason_codes
            self.awaiting = self.send(a.server_id, "MATCH_REQUEST", {"purpose": "expand",
                "requirements": primitive(c.requirements), "completed": self.completed(),
                "fixed": primitive(self.fixed()), "disabled_members": sorted(self.disabled_members),
                "requirement_version": c.requirement_version, "plan_version": c.plan_version})
            c.pending_requests[self.awaiting] = "server expansion"
            a.store.counters["server_requests"] += 1
            self.log("server_expansion_requested", reason=result.reason_codes)
        return True

    def match_failed(self, reasons, server=False):
        self.last_failure = reasons
        self.retry_count += 1
        self.log("matching_failed", reason=reasons, scope="server" if server else "local")
        if "SOLVER_TIMEOUT" in reasons:
            if self.collab.adjustment_policy.get("on_retry_limit") == "wait":
                return
            self.end("failed", "SOLVER_TIMEOUT: no feasible plan found within configured budget")
        elif self.retry_count >= self.collab.adjustment_policy.get("max_retries", 3):
            self.end("failed", "bounded matching retries exhausted: " + ",".join(reasons))
        elif self.collab.adjustment_policy.get("allow_revision", False):
            self.plan({"type": "matching_failed"})
        else:
            self.end("failed", "no feasible authorized plan: " + ",".join(reasons))

    def commit(self, assignments, source):
        c, a = self.collab, self.agent
        # Server responses may arrive after a dispatch prediction; always recheck
        # at actual delivery/executor admission, never start a task in the past.
        a.store.ledger.reserve(c.collaboration_id, assignments)
        if assignments:
            c.plan_version += 1
        for assignment in assignments:
            r = c.task_records[assignment.task_id]
            if r.assignment is not None:
                raise ValueError("attempted overwrite of a live assignment")
            assignment.plan_version = c.plan_version
            r.assignment = deepcopy(assignment)
            r.status, r.wait_reason = TaskStatus.ASSIGNED, "waiting_dispatch_or_ack"
            r.history.append({"assignment": primitive(assignment), "status": "ASSIGNED"})
            inputs = {dep: deepcopy(c.task_records[dep].result) for dep in r.requirement.dependencies}
            self.send(assignment.executor_id, "TASK_ASSIGN", {"assignment": assignment,
                "requirement": r.requirement, "inputs_available": True, "inputs": inputs},
                not_before=max(self.sim.now, assignment.planned_dispatch_time),
                expires_at=r.requirement.deadline-r.requirement.duration+1)
            member = c.member_states.get(assignment.executor_id)
            if member:
                member.assigned_tasks.append(r.requirement.task_id)
            self.log("assignment_committed", task_id=assignment.task_id, assignment_id=assignment.assignment_id,
                     executor_id=assignment.executor_id, planned_dispatch_time=assignment.planned_dispatch_time,
                     planned_start_time=assignment.planned_start_time, planned_end_time=assignment.planned_end_time, source=source)
        for r in c.task_records.values():
            if r.status == TaskStatus.PENDING:
                r.wait_reason = "dependency_result_not_received" if r.requirement.dependencies else "waiting_match"
        self.phase(Phase.A, "feasible plan committed; ACKs may still be queued")

    def release(self, aid):
        self.agent.store.ledger.release(aid)
        # Even collocated roles use the same message path.
        self.send(self.agent.server_id, "STATUS_UPDATE", {"release": [aid]})

    def void_unsent(self, record, reason):
        assignment = record.assignment
        if assignment is None or not self.agent.wrapper.invalidate_unsent(assignment.assignment_id):
            return False
        self.release(assignment.assignment_id)
        record.history[-1].update(status="CANCELLED", cancelled_at=self.sim.now, reason=reason)
        record.assignment = None
        record.status = TaskStatus.PENDING
        record.wait_reason = "invalidated_unsent_assignment"
        self.log("unsent_reservation_released", task_id=assignment.task_id, assignment_id=assignment.assignment_id, reason=reason)
        return True

    def observe_contact_change(self, event):
        c, a = self.collab, self.agent
        event_id = event.get("event_id")
        if event_id is not None:
            if event_id in a.store.processed_external_event_ids:
                return
            a.store.processed_external_event_ids.add(event_id)
            if c:
                c.processed_event_ids.add(event_id)
        a.known_contacts.replace(event["sender"], event["receiver"], event["intervals"])
        self.log("prediction_updated", reason=event.get("reason", "explicit external observation"), prediction_version=a.known_contacts.version)
        if c is None or c.phase == Phase.E or c.coordinator_id != a.agent_id:
            return
        if c.phase == Phase.C:
            self.dirty = True
        for record in c.task_records.values():
            assignment = record.assignment
            if assignment is None or assignment.executor_id != event["receiver"] or assignment.dispatch_agent_id != event["sender"]:
                continue
            if not a.wrapper.assignment_unsent(assignment.assignment_id):
                self.log("delivered_or_uncertain_task_retained", task_id=assignment.task_id, assignment_id=assignment.assignment_id,
                         reason="window change is not evidence of execution failure")
                continue
            opportunity = a.known_contacts.next_delivery(a.agent_id, assignment.executor_id, self.sim.now)
            if opportunity and max(opportunity[1], record.requirement.earliest_start) + record.requirement.duration <= record.requirement.deadline:
                # Revalidate this assignment against every other reservation.
                from .reservation_ledger import ReservationLedger
                ledger = ReservationLedger()
                ledger.entries = {k: deepcopy(v) for k, v in a.store.ledger.entries.items() if k != assignment.assignment_id}
                result = a.matcher.solve({record.requirement.task_id: record.requirement},
                    {assignment.executor_id: a.store.group_capabilities[assignment.executor_id]}, a.known_contacts, ledger,
                    now=self.sim.now, dispatch_agent_id=a.agent_id, collaboration_id=c.collaboration_id,
                    completed=self.completed(),
                    requirement_version=c.requirement_version, plan_version=c.plan_version+1, id_factory=self.sim.new_id)
                if result.status in {"OPTIMAL", "FEASIBLE"}:
                    self.void_unsent(record, "contact shifted but deadline remains feasible")
                    self.commit(result.assignments, "schedule_revalidation")
                    self.log("schedule_updated_without_replanning", task_id=record.requirement.task_id)
                    continue
            self.void_unsent(record, "contact change makes allocation infeasible")
            self.phase(Phase.C, "communication prediction invalidated unsent task")
            self.dirty = True

    def task_feedback(self, message):
        c, a, p = self.collab, self.agent, message.payload
        aid, task_id = p["assignment_id"], p["task_id"]
        r = c.task_records.get(task_id)
        history = next((h for h in r.history if h["assignment"]["assignment_id"] == aid), None) if r else None
        if history is None or history["assignment"]["executor_id"] != message.sender_id:
            self.log("unrecognized_task_feedback", message=message, assignment_id=aid, task_id=task_id)
            return
        history.setdefault("feedback", []).append({"type": message.message_type, "received_at": self.sim.now, **deepcopy(p)})
        if message.message_type in {"TASK_RESULT", "TASK_FAILED", "TASK_CANCEL_ACK"}:
            a.store.archived_results[aid] = deepcopy(p)
        if r.assignment is None or r.assignment.assignment_id != aid:
            self.log("historical_feedback_archived", task_id=task_id, assignment_id=aid)
            return
        if r.status in {TaskStatus.COMPLETED, TaskStatus.CANCELLED}:
            self.log("terminal_task_feedback_archived", task_id=task_id, assignment_id=aid)
            return
        if message.message_type == "TASK_ACK":
            if r.status == TaskStatus.ASSIGNED:
                r.status, r.wait_reason = TaskStatus.ACCEPTED, "waiting_start"
            self.log("task_ack_received", task_id=task_id, assignment_id=aid)
        elif message.message_type == "TASK_PROGRESS":
            if r.status not in {TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.CANCEL_PENDING}:
                r.status, r.wait_reason = TaskStatus.RUNNING, None
                r.actual_start_time = p.get("actual_start_time", r.actual_start_time)
        elif message.message_type == "TASK_RESULT":
            r.status, r.wait_reason = TaskStatus.COMPLETED, None
            r.actual_start_time, r.actual_end_time = p["actual_start_time"], p["actual_end_time"]
            r.result, r.result_received_at = deepcopy(p["result"]), self.sim.now
            history.update(status="COMPLETED", result=deepcopy(p["result"]), result_received_at=self.sim.now)
            self.release(aid)
            self.log("task_result_received", task_id=task_id, assignment_id=aid, actual_end_time=r.actual_end_time)
            self.dirty = c.phase != Phase.E
            self.check_success()
        elif message.message_type in {"TASK_REJECT", "TASK_FAILED"}:
            r.status = TaskStatus.FAILED
            history.update(status="FAILED", reason=p.get("reason"))
            self.release(aid)  # Explicit rejection/failure attests no ongoing execution.
            self.disabled_members.add(message.sender_id)
            self.log("task_failure_received", task_id=task_id, assignment_id=aid, reason=p.get("reason"))
            if c.phase != Phase.E:
                r.assignment = None
                r.status = TaskStatus.PENDING
                self.phase(Phase.C, "explicit execution failure or rejection")
                self.dirty = True
        elif message.message_type == "TASK_CANCEL_ACK" and p["stopped"]:
            r.status = TaskStatus.CANCELLED
            history["status"] = "CANCELLED"
            self.release(aid)
            self.log("task_cancel_ack_received", task_id=task_id, assignment_id=aid)

    def check_success(self):
        c = self.collab
        if c and c.phase != Phase.E and all(not r.requirement.required or
            (r.status == TaskStatus.COMPLETED and r.result_received_at is not None) for r in c.task_records.values()):
            self.end("success", "all required results received")
            return True
        return False

    def end(self, outcome, reason):
        c = self.collab
        if c is None or c.phase == Phase.E:
            return
        self.phase(Phase.E, reason)
        c.outcome, c.reason = outcome, reason
        self.log("collaboration_ended", outcome=outcome, reason=reason)
        members = set()
        for record in c.task_records.values():
            assignment = record.assignment
            if assignment is None:
                continue
            members.add(assignment.executor_id)
            if record.status in {TaskStatus.ASSIGNED, TaskStatus.ACCEPTED, TaskStatus.RUNNING}:
                if self.void_unsent(record, "collaboration ended"):
                    record.status = TaskStatus.CANCELLED
                else:
                    record.status = TaskStatus.CANCEL_PENDING
                    self.send(assignment.executor_id, "TASK_CANCEL", {"assignment_id": assignment.assignment_id, "task_id": assignment.task_id})
        for member in sorted(members | {c.initiator_id}):
            self.send(member, "COLLABORATION_END", {"outcome": outcome, "reason": reason})

    def timeout(self):
        c = self.collab
        if c is None or c.phase == Phase.E or c.coordinator_id != self.agent.agent_id:
            return
        # Completion deadlines concern execution, not result delivery. Never infer
        # failure merely because the reverse ACK/result contact has not opened.
        for record in c.task_records.values():
            assignment = record.assignment
            if (assignment and record.status == TaskStatus.ASSIGNED and self.sim.now > record.requirement.deadline
                    and self.agent.wrapper.assignment_unsent(assignment.assignment_id)):
                self.end("failed", "assignment could not be dispatched before deadline")
                return
