from ortools.sat.python import cp_model
from .models import MatchResult, TaskAssignment
from .registry import next_available_contact


class Matcher:
    def __init__(self, catalog, time_limit=1.0):
        self.catalog = catalog
        self.time_limit = time_limit

    def solve(self, requirements, capabilities, schedule, ledger, *, now, dispatch_agent_id,
              collaboration_id="collab", requirement_version=1, plan_version=1,
              completed=(), fixed=(), previous=(), id_factory=None):
        completed = set(completed)
        fixed_tasks = {a.task_id for a in fixed}
        pending = [r for r in requirements.values() if r.task_id not in completed | fixed_tasks]
        deferred = [r.task_id for r in pending if any(dep not in completed for dep in r.dependencies)]
        ready = [r for r in pending if r.task_id not in deferred]
        if not ready:
            return MatchResult("FEASIBLE", deferred_task_ids=deferred, explanation="waiting for dependency results or fixed tasks")
        if self.time_limit <= 0:
            return MatchResult("UNKNOWN", reason_codes=["SOLVER_TIMEOUT"])
        model = cp_model.CpModel()
        horizon = max(r.deadline for r in requirements.values())
        intervals = {a: [] for a in capabilities}
        for agent in capabilities:
            for index, (start, end) in enumerate(ledger.intervals(agent)):
                intervals[agent].append(model.new_interval_var(start, end-start, end, f"fixed_{agent}_{index}"))
        variables, candidates, starts, ends, dispatches = {}, {}, {}, {}, {}
        previous_map = {a.task_id: a.executor_id for a in previous}
        retained = []
        for task in ready:
            candidates[task.task_id] = self.catalog.retrieve(capabilities, task.capability_description)
            if not candidates[task.task_id]:
                return MatchResult("INFEASIBLE", deferred_task_ids=deferred, reason_codes=["NO_CAPABILITY_MATCH"])
            starts[task.task_id] = model.new_int_var(0, horizon, "s_"+task.task_id)
            ends[task.task_id] = model.new_int_var(0, horizon, "e_"+task.task_id)
            model.add(starts[task.task_id] >= max(now, task.earliest_start))
            model.add(ends[task.task_id] == starts[task.task_id] + task.duration)
            model.add(ends[task.task_id] <= task.deadline)
            options = []
            for agent in candidates[task.task_id]:
                if ledger.rights.get(agent, collaboration_id) != collaboration_id:
                    continue
                opportunity = schedule.next_delivery(dispatch_agent_id, agent, now)
                if opportunity is None:
                    continue
                available = next_available_contact(schedule, agent, dispatch_agent_id, now, ledger.intervals(agent))
                if available is None:
                    continue
                sent, received = opportunity
                selected = model.new_bool_var(f"x_{task.task_id}_{agent}")
                model.add(starts[task.task_id] >= received).only_enforce_if(selected)
                model.add(starts[task.task_id] >= available).only_enforce_if(selected)
                interval = model.new_optional_interval_var(starts[task.task_id], task.duration,
                    ends[task.task_id], selected, f"i_{task.task_id}_{agent}")
                intervals[agent].append(interval)
                variables[task.task_id, agent] = selected
                dispatches[task.task_id, agent] = sent
                options.append(selected)
                if previous_map.get(task.task_id) == agent:
                    retained.append(selected)
            if not options:
                return MatchResult("INFEASIBLE", deferred_task_ids=deferred, reason_codes=["CONTACT_UNAVAILABLE"])
            model.add_exactly_one(options)
        for items in intervals.values():
            model.add_no_overlap(items)
        makespan = model.new_int_var(0, horizon, "makespan")
        model.add_max_equality(makespan, list(ends.values()))
        # A unit retention improvement dominates any possible makespan reduction.
        model.minimize(makespan - (horizon + 1) * sum(retained))
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = self.time_limit
        solver.parameters.num_search_workers = 1
        solver.parameters.random_seed = 0
        status = solver.solve(model)
        if status == cp_model.UNKNOWN:
            return MatchResult("UNKNOWN", deferred_task_ids=deferred, reason_codes=["SOLVER_TIMEOUT"])
        if status == cp_model.INFEASIBLE:
            # Diagnose without pretending a timeout proved infeasibility.
            deadline_impossible = any(not any(
                (op := schedule.next_delivery(dispatch_agent_id, a, now)) is not None
                and max(now, task.earliest_start, op[1]) + task.duration <= task.deadline
                for a in candidates[task.task_id]) for task in ready)
            return MatchResult("INFEASIBLE", deferred_task_ids=deferred,
                               reason_codes=["DEADLINE_CONFLICT" if deadline_impossible else "RESOURCE_CONFLICT"])
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            raise RuntimeError("invalid CP-SAT model: " + solver.response_stats())
        assignments = []
        for task in ready:
            agent = next(a for a in candidates[task.task_id]
                         if (task.task_id, a) in variables and solver.value(variables[task.task_id, a]))
            aid = id_factory("assignment") if id_factory else f"{collaboration_id}:{plan_version}:{task.task_id}"
            assignments.append(TaskAssignment(aid, task.task_id, agent, dispatch_agent_id,
                dispatches[task.task_id, agent], solver.value(starts[task.task_id]), solver.value(ends[task.task_id]),
                requirement_version, plan_version, task.parameters.get("valid_until")))
        return MatchResult("OPTIMAL" if status == cp_model.OPTIMAL else "FEASIBLE", assignments, deferred)
