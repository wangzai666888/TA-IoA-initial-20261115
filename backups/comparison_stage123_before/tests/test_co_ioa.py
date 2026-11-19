from copy import deepcopy
import json
from pathlib import Path
import pytest
from co_ioa.run import load_scenario
from co_ioa.event_loop import Simulation
from co_ioa.communication_simulator import ContactSchedule
from co_ioa.models import (TaskRequirement, TaskAssignment, TaskStatus, ContactWindow,
                           Message, CollaborationPhase, primitive)
from co_ioa.matcher import Matcher
from co_ioa.capability_catalog import CapabilityCatalog
from co_ioa.reservation_ledger import ReservationLedger
from co_ioa.registry import Registry
from co_ioa.planner import LLMPlanner, PlanningContext, validate_requirements
from co_ioa.scenarios import suite, window


def experiment(name, **changes):
    data = deepcopy(suite()[name])
    data.update(changes)
    sim = Simulation(data)
    return sim, sim.run()


def events(sim, kind):
    return [e for e in sim.events if e["event_type"] == kind]


def test_standard_delayed_contact_replaces_unsent_and_waits_for_result():
    sim, result = experiment("delayed_contact")
    assert result["outcome"] == "success"
    assert result["ended_at"] == 660
    assignments = events(sim, "assignment_committed")
    assert [(e["executor_id"], e["planned_dispatch_time"], e["planned_end_time"]) for e in assignments] == [
        ("AUV-1", 180, 540), ("AUV-2", 300, 660)]
    assert any(e["sim_time"] == 120 and e["phase_before"] == "active" and e["phase_after"] == "checking"
               for e in events(sim, "phase_changed"))
    assert len(events(sim, "unsent_assignment_voided")) == 1
    assert [(e["agent_id"], e["sim_time"]) for e in events(sim, "task_started")] == [("AUV-2", 300)]
    assert not result["unreleased_reservations"]
    assert result["tasks"]["inspect"]["history"][0]["status"] == "CANCELLED"


def test_baseline_execution_end_differs_from_result_receipt_and_ack():
    sim, result = experiment("baseline")
    task = result["tasks"]["inspect"]
    assert task["actual_start_time"] == 180
    assert task["actual_end_time"] == 540
    assert task["result_received_at"] == result["ended_at"] == 660
    assert events(sim, "task_ack_received")[0]["sim_time"] == 660
    assert events(sim, "coordinator_authority_active")[0]["sim_time"] == 0


def test_local_alternative_does_not_expand_at_120():
    sim, result = experiment("local_alternative")
    assert result["outcome"] == "success"
    assert not [e for e in events(sim, "server_expansion_requested") if e["sim_time"] == 120]
    assert events(sim, "assignment_committed")[-1]["source"] == "local"


def test_feasible_shift_stays_active_no_extra_planning_or_matching():
    sim, result = experiment("feasible_shift")
    assert result["outcome"] == "success"
    assert result["tasks"]["inspect"]["actual_start_time"] == 240
    assert events(sim, "schedule_updated_without_replanning")[0]["sim_time"] == 120
    assert not [e for e in events(sim, "phase_changed") if e["sim_time"] == 120]
    assert result["metrics"]["replanning_count"] == 0


def test_hard_deadline_does_not_get_extended_or_busy_loop():
    sim, result = experiment("hard_deadline_failure")
    assert result["outcome"] == "failed"
    assert not events(sim, "task_started")
    assert result["tasks"]["inspect"]["requirement"]["deadline"] == 900
    assert result["calls"]["C"] <= 3


def test_authorized_requirement_revision_changes_version_then_matches():
    sim, result = experiment("authorized_revision")
    assert result["outcome"] == "success"
    assert result["metrics"]["replanning_count"] == 1
    assert events(sim, "assignment_committed")[0]["requirement_version"] == 2
    assert result["tasks"]["inspect"]["actual_end_time"] == 1080


def test_received_task_continues_disconnected_without_duplicate_assignment():
    sim, result = experiment("accepted_then_disconnected")
    assert result["outcome"] == "success"
    assert len(events(sim, "task_started")) == len(events(sim, "assignment_committed")) == 1
    assert events(sim, "delivered_or_uncertain_task_retained")


def test_unknown_window_does_not_telepathically_update_predictor():
    sim, result = experiment("unobserved_window_change")
    assert not events(sim, "prediction_updated")
    assert sim.agents["USV-1"].known_contacts.next_delivery("USV-1", "AUV-1", 120)[0] == 180
    assert not events(sim, "task_started")
    assert result["outcome"] == "failed"


def test_remote_server_request_queues_and_parallel_task_runs_during_checking():
    sim, result = experiment("remote_server_parallel")
    assert result["outcome"] == "success"
    assert result["coordinator_id"] == "COORD-1"  # UAV platform, not tied to USV.
    requests = [e for e in events(sim, "message_queued") if e["message_type"] == "MATCH_REQUEST" and e["sim_time"] == 120]
    assert len(requests) == 1
    received = [e for e in events(sim, "message_delivered") if e["message_id"] == requests[0]["message_id"]]
    assert received[0]["sim_time"] == 250
    assert result["tasks"]["parallel"]["actual_start_time"] == 0
    assert result["tasks"]["parallel"]["actual_end_time"] == 400
    assert any(e["event_type"] == "phase_changed" and e["sim_time"] == 120 and e["phase_after"] == "checking" for e in sim.events)


def test_dependency_only_scheduled_after_actual_result_arrival():
    sim, result = experiment("dependency")
    assert result["outcome"] == "success"
    assert result["tasks"]["analyse"]["actual_start_time"] == 660
    assert result["tasks"]["inspect"]["actual_end_time"] == 540
    assert any("analyse" in e.get("deferred_task_ids", []) for e in events(sim, "local_matching"))


def test_lost_ack_and_result_do_not_release_or_dispatch_duplicate_work():
    sim, result = experiment("lost_ack_result", max_time=800)
    assert result["outcome"] == "failed"
    assert len(events(sim, "task_started")) == 1
    assert result["tasks"]["inspect"]["result_received_at"] is None
    assert result["unreleased_reservations"]
    assert result["executions"][next(iter(result["executions"]))]["status"] == "COMPLETED"


def test_end_retains_running_uncancellable_member_and_reservation():
    sim, result = experiment("cancel_pending_cleanup")
    assert result["outcome"] == "cancelled"
    assert result["ended_at"] == 200
    assert result["pending_cleanup"]
    assert result["unreleased_reservations"]
    assert result["tasks"]["inspect"]["status"] == "CANCEL_PENDING"


def test_cancellation_ack_must_travel_reverse_window_before_release():
    data = deepcopy(suite()["cancel_pending_cleanup"])
    data["max_time"] = 800
    data["agents"][2]["uncancellable"] = False
    sim = Simulation(data)
    result = sim.run()
    assert events(sim, "task_cancelled")[0]["sim_time"] == 200
    assert events(sim, "task_cancel_ack_received")[0]["sim_time"] == 660
    assert not result["unreleased_reservations"]


def test_registry_next_contact_is_relative_to_dispatcher_and_idle_time():
    registry = Registry()
    registry.register("A", ["sonar"], 0, "A")
    schedule = ContactSchedule([window("S", "A", 10, 20), window("C", "A", 30, 40)])
    entry, metadata = registry.query("A", "C", 0, schedule, [(30, 35)])
    assert entry.next_available_contact_time == 35
    assert metadata["dispatch_agent_id"] == "C"
    assert registry.query("A", "X", 0, schedule)[0].next_available_contact_time is None


def test_half_open_windows_latency_and_directed_contacts():
    schedule = ContactSchedule([window("a", "b", 10, 20)], latency=3)
    assert schedule.next_delivery("a", "b", 17) == (17, 20)
    assert schedule.next_delivery("a", "b", 18) is None
    assert schedule.next_delivery("b", "a", 10) is None
    assert schedule.next_delivery("a", "a", 100) == (100, 100)
    assert ContactSchedule([window("a", "b", 10, 20)]).next_delivery("a", "b", 20) is None


def test_message_ttl_at_arrival_and_send_only_enqueues():
    sim = Simulation(suite()["baseline"])
    sender = sim.agents["UAV-1"]
    mid = sender.wrapper.send("USV-1", "REGISTER", {"capabilities": []}, expires_at=0)
    assert not sim.agents["USV-1"].store.messages
    sender.wrapper.flush_outbox(0)
    assert sender.wrapper.outbox[mid]["status"] == "EXPIRED"
    assert not sim.communication.inflight


def test_mutable_payload_not_shared_and_message_id_deduplication():
    sim = Simulation(suite()["baseline"])
    sender = sim.agents["UAV-1"]
    payload = {"capabilities": ["analysis"]}
    mid = sender.wrapper.send("USV-1", "REGISTER", payload)
    payload["capabilities"].append("sonar")
    sender.wrapper.flush_outbox(0)
    sim.communication.deliver(0)
    receiver = sim.agents["USV-1"]
    assert receiver.store.registry.capabilities["UAV-1"] == ["analysis"]
    receiver.wrapper.receive(deepcopy(sender.wrapper.outbox[mid]["message"]), 0)
    assert len(receiver.store.messages) == 1
    assert events(sim, "duplicate_message_ignored")


def test_solver_timeout_is_unknown_and_capability_filter_is_exact():
    catalog = CapabilityCatalog(suite()["baseline"]["capability_templates"])
    tasks = {"t": TaskRequirement("t", "sonar", 0, 900, 360)}
    args = dict(now=0, dispatch_agent_id="S")
    assert Matcher(catalog, time_limit=0).solve(tasks, {"A": ["sonar"]}, ContactSchedule(), ReservationLedger(), **args).status == "UNKNOWN"
    result = Matcher(catalog).solve(tasks, {"A": ["analysis"]}, ContactSchedule(), ReservationLedger(), **args)
    assert result.status == "INFEASIBLE" and result.reason_codes == ["NO_CAPABILITY_MATCH"]


def test_optional_intervals_prevent_overlap_but_allow_one_window_predispatch():
    catalog = CapabilityCatalog({"c": {"duration": 10}})
    tasks = {name: TaskRequirement(name, "c", 0, 30, 10) for name in ("one", "two")}
    result = Matcher(catalog).solve(tasks, {"A": ["c"]}, ContactSchedule([window("S", "A", 0, 1)]),
                                    ReservationLedger(), now=0, dispatch_agent_id="S")
    assert result.status == "OPTIMAL"
    a, b = sorted(result.assignments, key=lambda row: row.planned_start_time)
    assert a.planned_end_time <= b.planned_start_time
    assert a.planned_dispatch_time == b.planned_dispatch_time == 0


def test_ledger_conflict_is_atomic_and_retries_idempotent():
    ledger = ReservationLedger()
    a = TaskAssignment("a", "t", "E", "S", 0, 0, 10, 1, 1)
    b = TaskAssignment("b", "u", "E", "S", 0, 5, 15, 1, 1)
    ledger.reserve("c", [a])
    ledger.reserve("c", [a])
    before = deepcopy(ledger.entries)
    with pytest.raises(ValueError):
        ledger.reserve("c", [b])
    assert before == ledger.entries


def test_llm_interface_is_explicitly_unconfigured_and_bounded():
    catalog = CapabilityCatalog({"c": {"duration": 10}})
    context = PlanningContext("objective", {}, {}, {})
    with pytest.raises(RuntimeError, match="not configured"):
        LLMPlanner(catalog).plan(context)
    calls = []
    def invalid(*args):
        calls.append(args)
        return '{}'
    with pytest.raises(ValueError, match="bounded corrections"):
        LLMPlanner(catalog, invalid).plan(context)
    assert len(calls) == 3


def test_planner_rejects_cycles_shortened_duration_and_running_redefinition():
    catalog = CapabilityCatalog({"c": {"duration": 10}})
    tasks = {"a": TaskRequirement("a", "c", 0, 20, 10, ["b"]),
             "b": TaskRequirement("b", "c", 0, 20, 10, ["a"])}
    context = PlanningContext("o", {}, {}, {})
    with pytest.raises(ValueError, match="cyclic"):
        validate_requirements(tasks, catalog, context)
    tasks = {"a": TaskRequirement("a", "c", 0, 20, 5)}
    with pytest.raises(ValueError, match="template"):
        validate_requirements(tasks, catalog, context)
    old = {"a": TaskRequirement("a", "c", 0, 20, 10)}
    context = PlanningContext("o", {}, {"allow_revision": True}, old, execution_snapshot={"a": {"status": "RUNNING"}})
    with pytest.raises(ValueError, match="redefine"):
        validate_requirements({"a": TaskRequirement("a", "c", 0, 30, 10)}, catalog, context)


def test_old_results_archived_without_overwriting_current_assignment():
    sim, result = experiment("delayed_contact")
    controller = sim.coordinator().controller
    record = controller.collab.task_records["inspect"]
    old = record.history[0]["assignment"]
    message = Message("historical", "mission", old["executor_id"], "USV-1", "TASK_RESULT", 1000,
        None, 1, 1, None, {"assignment_id": old["assignment_id"], "task_id": "inspect",
                           "actual_start_time": 180, "actual_end_time": 540, "result": {"old": True}})
    current = deepcopy(primitive(record))
    controller.handle_message(message)
    assert record.assignment.assignment_id == current["assignment"]["assignment_id"]
    assert record.result == current["result"]
    assert old["assignment_id"] in sim.coordinator().store.archived_results


def test_old_ack_does_not_regress_completed_state_or_restart_executor():
    sim, result = experiment("baseline")
    execution = next(iter(sim.agents["AUV-1"].store.executions.values()))
    assignment = execution["assignment"]
    msg = Message("duplicate-task", "mission", "USV-1", "AUV-1", "TASK_ASSIGN", 0, None, 1, 1, None,
                  {"assignment": primitive(assignment), "requirement": primitive(execution["requirement"]), "inputs_available": True})
    sim.agents["AUV-1"].executor.accept(msg)
    assert len(events(sim, "task_started")) == 1
    ack = Message("late-ack", "mission", "AUV-1", "USV-1", "TASK_ACK", 0, None, 1, 1, None,
                  {"assignment_id": assignment.assignment_id, "task_id": "inspect"})
    sim.coordinator().controller.handle_message(ack)
    assert sim.coordinator().controller.collab.task_records["inspect"].status == TaskStatus.COMPLETED


def test_output_is_utf8_jsonl_with_required_fields_and_reproducible(tmp_path):
    sim, result = experiment("delayed_contact")
    sim.write(tmp_path)
    result2 = experiment("delayed_contact")[1]
    assert result == result2
    rows = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {"sim_time", "event_id", "agent_id", "collaboration_id", "task_id", "assignment_id", "event_type",
            "phase_before", "phase_after", "requirement_version", "plan_version", "reason", "message_id"} <= rows[0].keys()
    assert json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))["outcome"] == "success"


def test_saved_scenarios_match_deterministic_builders():
    for name, data in suite().items():
        assert load_scenario(name) == data


def test_executor_failure_rematches_without_touching_other_running_task():
    data = deepcopy(suite()["remote_server_parallel"])
    data["events"] = []
    data["agents"][2]["fail_tasks"] = ["inspect"]
    # Failure arrives at 660; an actual later dispatch opportunity permits replacement.
    data["windows"].append(window("COORD-1", "AUV-2", 660, 680))
    data["requirements"][0]["deadline"] = 1200
    data["hard_constraints"]["deadline"] = 1200
    sim = Simulation(data)
    result = sim.run()
    assert result["outcome"] == "success"
    assert len(events(sim, "task_failure_received")) == 1
    assert result["tasks"]["parallel"]["actual_end_time"] == 400
    assert result["tasks"]["inspect"]["actual_end_time"] == 1020
    assert result["tasks"]["inspect"]["result_received_at"] == 1080


def test_nonzero_latency_ttl_expires_before_arrival_not_after_action():
    data = deepcopy(suite()["baseline"])
    data["latency"] = 3
    sim = Simulation(data)
    sender = sim.agents["UAV-1"]
    sender.wrapper.send("USV-1", "REGISTER", {"capabilities": ["analysis"]}, expires_at=3)
    sender.wrapper.flush_outbox(0)
    sim.now = 3
    sim.communication.deliver(3)
    assert not sim.agents["USV-1"].store.messages
    assert sim.message_counts["expired"] == 1


def test_late_assignment_rejected_without_starting_in_past():
    sim = Simulation(suite()["baseline"])
    sim.now = 800
    a = TaskAssignment("late", "inspect", "AUV-1", "USV-1", 180, 180, 540, 1, 1)
    task = sim.scenario["requirements"][0]
    message = Message("late-message", "mission", "USV-1", "AUV-1", "TASK_ASSIGN", 0, None, 1, 1, None,
                      {"assignment": primitive(a), "requirement": task, "inputs_available": True})
    sim.agents["AUV-1"].executor.accept(message)
    assert not sim.agents["AUV-1"].store.executions
    assert events(sim, "task_rejected")[0]["reason"] == "late_delivery_or_execution_deadline"


def test_delayed_handover_keeps_initiator_authority_until_ack_received():
    data = deepcopy(suite()["baseline"])
    data["windows"] = [w for w in data["windows"] if not (w["sender_id"] == "USV-1" and w["receiver_id"] == "UAV-1")]
    data["windows"].append(window("USV-1", "UAV-1", 20, 1000))
    sim = Simulation(data)
    sim.run()
    assert events(sim, "handover_acknowledged")[0]["sim_time"] == 20
    assert not [e for e in events(sim, "assignment_committed") if e["sim_time"] < 20]


def test_completed_task_does_not_regress_when_old_failure_arrives():
    sim, result = experiment("baseline")
    record = sim.coordinator().controller.collab.task_records["inspect"]
    message = Message("out-of-order-failure", "mission", "AUV-1", "USV-1", "TASK_FAILED", 100, None,
        1, 1, None, {"assignment_id": record.assignment.assignment_id, "task_id": "inspect", "reason": "old"})
    sim.coordinator().controller.handle_message(message)
    assert record.status == TaskStatus.COMPLETED


def test_objective_addition_enters_planning_while_existing_work_continues():
    sim, result = experiment("objective_revision_parallel")
    assert result["outcome"] == "success"
    assert result["tasks"]["parallel"]["actual_start_time"] == 0
    assert result["tasks"]["parallel"]["actual_end_time"] == 400
    assert result["tasks"]["extra"]["actual_start_time"] == 400
    assert any(e["sim_time"] == 120 and e["phase_after"] == "planning" for e in events(sim, "phase_changed"))
    assert result["metrics"]["replanning_count"] == 1


def test_explicit_external_observation_deduplicated_locally():
    sim = Simulation(suite()["baseline"])
    event = {"event_id": "observation-1", "sender": "USV-1", "receiver": "AUV-1", "intervals": [[720, 740]]}
    node = sim.agents["USV-1"]
    node.controller.observe_contact_change(event)
    node.controller.observe_contact_change(event)
    assert node.known_contacts.version == 1
    assert sim.agents["AUV-1"].known_contacts.version == 0


def test_positive_latency_full_collaboration_keeps_execution_and_delivery_separate():
    sim, result = experiment("baseline", latency=2)
    assert result["outcome"] == "success"
    task = result["tasks"]["inspect"]
    assert task["actual_start_time"] == 182
    assert task["actual_end_time"] == 542
    assert task["result_received_at"] == 662
