from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import threading
from .agent import Agent
from .capability_catalog import CapabilityCatalog
from .communication_simulator import CommunicationSimulator
from .models import primitive, CollaborationPhase, TaskStatus


class Simulation:
    """Integer discrete-event clock with explicit priority and same-tick quiescence."""
    def __init__(self, scenario, *, method="co_ioa", formal_budget=None,
                 early_failure=False):
        from .methods import AdapterNotReadyError, require_runnable
        if method == 'ioa_original':
            raise AdapterNotReadyError('ioa_original runs only through its native IoA processes')
        self.method = require_runnable(method)
        self.formal_budget = formal_budget
        self.early_failure = early_failure
        self.formal_budget_state = {"first_request_started": None, "lock": threading.Lock()}
        self.scenario = deepcopy(scenario)
        self.now, self.sequence = 0, 0
        self.events = []
        self.model_calls = []
        self.planning_attempts = []
        self.message_counts = Counter(generated=0, delivered=0, expired=0)
        self.catalog = CapabilityCatalog(scenario["capability_templates"])
        ids = [node["id"] for node in scenario["agents"]]
        if len(set(ids)) != len(ids) or scenario["server_id"] not in ids:
            raise ValueError("invalid agent IDs/server")
        if not scenario.get("perfect_prediction", False) and "predicted_windows" not in scenario:
            raise ValueError("declare perfect_prediction or provide separate predictions")
        self.communication = CommunicationSimulator(self, scenario["windows"], scenario.get("latency", 0))
        self.agents = {node["id"]: Agent(self, node, self.catalog, scenario) for node in scenario["agents"]}
        if "SERVER" not in self.agents[scenario["server_id"]].roles:
            raise ValueError("configured server must have SERVER role")
        initiators = [a for a in self.agents.values() if "INITIATOR" in a.roles]
        if len(initiators) != 1:
            raise ValueError("single collaboration requires exactly one initiator")
        self.initiator_id = initiators[0].agent_id
        self.last_result = None
        self.infrastructure = None
        self.event_driven = False
        # Explicit initial observation, not a read of live remote Python state.
        for name, caps in scenario.get("initial_registry", {}).items():
            self.agents[scenario["server_id"]].store.registry.register(name, caps, 0, "scenario_initial_observation")
            self.log("initial_registry_observation", scenario["server_id"], member_id=name)

    def new_id(self, prefix):
        self.sequence += 1
        return f"{prefix}-{self.sequence:06d}"

    def call_agent(self, name, operation, payload=None):
        if self.infrastructure is not None:
            return self.infrastructure.call_agent(name, operation, payload)
        return self.agents[name].runtime.call(operation, payload)

    def log(self, kind, agent_id, *, message=None, **extra):
        collaboration = self.agents[agent_id].store.collaborations.get("mission") if agent_id in self.agents else None
        row = {"sim_time": self.now, "event_id": self.new_id("event"), "agent_id": agent_id,
               "collaboration_id": "mission", "task_id": None, "assignment_id": None,
               "event_type": kind, "phase_before": collaboration.phase if collaboration else None,
               "phase_after": collaboration.phase if collaboration else None,
               "requirement_version": collaboration.requirement_version if collaboration else None,
               "plan_version": collaboration.plan_version if collaboration else None,
               "reason": None, "message_id": None}
        if message:
            row.update(message_id=message.message_id, message_type=message.message_type,
                       sender_id=message.sender_id, receiver_id=message.receiver_id,
                       correlation_id=message.correlation_id,
                       collaboration_id=message.collaboration_id,
                       requirement_version=message.requirement_version, plan_version=message.plan_version)
        row.update(extra)
        self.events.append(primitive(row))

    def external_event(self, event):
        kind = event["type"]
        if kind == "contact_change":
            self.communication.truth.replace(event["sender"], event["receiver"], event["intervals"])
            self.log("physical_contact_changed", event["sender"], reason=event.get("reason"))
        elif kind == "prediction_update":
            for name in event["observers"]:
                self.call_agent(name, "observe", event)
        elif kind == "objective_update":
            self.call_agent(self.initiator_id, "objective_update", event["payload"])
        elif kind == "coordinator_failure":
            self.call_agent(event["agent_id"], "end", {"outcome": "failed",
                "reason": "UNSUPPORTED: coordinator failure election is not implemented"})
        else:
            raise ValueError("unsupported external event " + kind)

    def settle(self):
        # Arrivals -> local completions -> coordination/start -> outboxes. Newly
        # generated zero-delay messages return to the arrival priority first.
        for _ in range(self.scenario.get("same_time_limit", 1000)):
            changed = self.communication.deliver(self.now)
            for agent in self.agents.values():
                changed = self.call_agent(agent.agent_id, "complete") or changed
            for agent in self.agents.values():
                changed = self.call_agent(agent.agent_id, "coordinate") or changed
            for agent in self.agents.values():
                changed = self.call_agent(agent.agent_id, "flush") or changed
            if not changed:
                return
        raise RuntimeError("same-time event loop exceeded guard; possible retry cycle")

    def run(self):
        for agent in self.agents.values():
            self.call_agent(agent.agent_id, "initialize")
        # Run through the configured horizon even after E to collect background
        # cancellation/result traffic; E never fabricates device termination.
        horizon = self.scenario.get("max_time", 1200)
        second = 0
        while second <= horizon:
            self.now = second
            for event in self.scenario.get("events", []):
                if event["time"] == second:
                    self.external_event(event)
            self.settle()
            for agent in self.agents.values():
                self.call_agent(agent.agent_id, "timeout")
            if self.early_failure:
                from .external_evidence import evaluate_co
                certificate = evaluate_co(self, allow_early_failure=True)['terminal_certificate']
                if certificate is not None:
                    self.log('external_early_failure_certified', self.initiator_id,
                             certificate=certificate)
                    self.last_result = self.result()
                    return self.last_result
            second = self.next_clock_time(horizon) if self.event_driven else second + 1
        coordinator = self.coordinator()
        if coordinator.controller.collab.phase != CollaborationPhase.E:
            self.call_agent(coordinator.agent_id, "end", {"outcome": "failed",
                "reason": "experiment horizon reached; unknown remote work retained"})
            self.settle()
        self.last_result = self.result()
        return self.last_result

    def next_clock_time(self, horizon):
        """Clock scheduling only; these times are never injected into a planner."""
        times = [horizon, horizon + 1]
        times += [e["time"] for e in self.scenario.get("events", [])]
        times += [x for w in self.communication.truth.windows for x in (w.start, w.end)]
        times += [row[0] for row in self.communication.inflight]
        for agent in self.agents.values():
            for row in agent.wrapper.outbox.values():
                if row["status"] == "QUEUED":
                    times.append(row["not_before"])
                    message = row["message"]
                    opportunity = self.communication.truth.next_delivery(message.sender_id, message.receiver_id, self.now)
                    # timeout() runs after settling. Its newly queued messages
                    # are processed on the next integer tick, just as offline.
                    if row["not_before"] <= self.now and opportunity and opportunity[0] == self.now:
                        times.append(self.now + 1)
                    if row["message"].expires_at is not None:
                        times.append(row["message"].expires_at)
            for execution in agent.store.executions.values():
                if execution["status"] in {TaskStatus.ACCEPTED, TaskStatus.RUNNING}:
                    times.extend((execution["scheduled_start"], execution["scheduled_end"]))
                    if execution["assignment"].valid_until is not None:
                        times.append(execution["assignment"].valid_until)
            c = agent.controller.collab
            if c:
                times += [r.requirement.deadline + 1 for r in c.task_records.values()]
                if self.early_failure:
                    times += [r.requirement.deadline - r.requirement.duration + 1
                              for r in c.task_records.values() if r.requirement.required]
        return min(t for t in times if t > self.now)

    def coordinator(self):
        initiator = self.agents[self.initiator_id]
        name = initiator.controller.collab.coordinator_id
        return self.agents[name] if name else initiator

    def result(self):
        coordinator = self.coordinator()
        c = coordinator.controller.collab
        # End-of-run evaluation can inspect all stores. Controllers cannot.
        executions = {aid: primitive(e) for a in self.agents.values() for aid, e in a.store.executions.items()}
        tasks = {}
        on_time = 0
        required = 0
        for name, record in c.task_records.items():
            row = primitive(record)
            aid = record.assignment.assignment_id if record.assignment else None
            row["executor_view"] = executions.get(aid)
            tasks[name] = row
            if record.requirement.required:
                required += 1
                actuals = [e for e in executions.values() if e["assignment"]["task_id"] == name
                           and e["status"] == "COMPLETED" and e["actual_end_time"] <= record.requirement.deadline]
                on_time += bool(actuals)
        results_at = [r.result_received_at for r in c.task_records.values() if r.requirement.required]
        all_results = max(results_at) if results_at and all(t is not None for t in results_at) else None
        ended = next((r["sim_time"] for r in self.events if r["event_type"] == "collaboration_ended"
                      and r["agent_id"] == coordinator.agent_id), None)
        counters = Counter()
        for a in self.agents.values():
            counters.update(a.store.counters)
        solver_states = Counter(r["solver_status"] for r in self.events if "solver_status" in r)
        member_changes = [{"time": r["sim_time"], "member_id": r["executor_id"], "task_id": r["task_id"]}
                          for r in self.events if r["event_type"] == "assignment_committed"]
        pending = [aid for aid, e in executions.items() if e["status"] in {"RUNNING", "ACCEPTED", "CANCEL_PENDING"}]
        return {"scenario": self.scenario["name"], "seed": self.scenario.get("seed", 0),
            "planner": "LLMPlanner" if self.scenario.get("planner") == "llm" else "RuleBasedPlanner",
            "model_calls": deepcopy(self.model_calls), "planning_attempts": deepcopy(self.planning_attempts),
            "matcher": "OR-Tools CP-SAT", "perfect_prediction": self.scenario.get("perfect_prediction", False),
            "coordinator_id": coordinator.agent_id, "outcome": c.outcome, "reason": c.reason,
            "error_kind": c.error_kind, "evaluation_valid": c.outcome != "error",
            "ended_at": ended, "evaluated_until": self.now, "tasks": tasks, "executions": executions,
            "member_changes": member_changes, "calls": dict(counters), "messages": dict(self.message_counts),
            "solver_statuses": dict(solver_states),
            "unreleased_reservations": primitive(self.agents[self.scenario["server_id"]].store.ledger.entries),
            "coordinator_reservations": primitive(coordinator.store.ledger.entries),
            "pending_cleanup": pending,
            "pending_cancellation_acknowledgements": [r.assignment.assignment_id for r in c.task_records.values()
                if r.assignment and r.status == TaskStatus.CANCEL_PENDING],
            "metrics": {"on_time_execution_completion_rate": (on_time / required if required else 1.0) if c.outcome != "error" else None,
                "collaboration_success_rate": float(c.outcome == "success") if c.outcome != "error" else None,
                "all_required_results_received_at": all_results,
                "replanning_count": max(0, counters["P"]-1), "rematching_count": max(0, counters["C"]-1),
                "control_message_count": self.message_counts["generated"]}}

    def write(self, output):
        path = Path(output)
        path.mkdir(parents=True, exist_ok=True)
        with (path / "events.jsonl").open("w", encoding="utf-8", newline="\n") as stream:
            for row in self.events:
                stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
        for name, value in [("result.json", self.last_result or self.result()), ("scenario.json", self.scenario)]:
            (path / name).write_text(json.dumps(primitive(value), ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
