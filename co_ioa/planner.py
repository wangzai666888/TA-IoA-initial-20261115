import json
import sys
from copy import deepcopy
from dataclasses import dataclass, field
from .models import TaskRequirement, TaskStatus, primitive
from .errors import PlanningValidationError, ModelServiceError


@dataclass
class PlanningContext:
    objective: str
    hard_constraints: dict
    adjustment_policy: dict
    current_requirements: dict
    history: list = field(default_factory=list)
    execution_snapshot: dict = field(default_factory=dict)
    trigger_event: dict = field(default_factory=dict)
    failure_reasons: list = field(default_factory=list)
    initial_requirements: dict = field(default_factory=dict)
    simulation_time: int = 0
    agent_id: str = ""


@dataclass
class PlanningResult:
    requirements: dict[str, TaskRequirement]
    change_summary: str


def requirement_value(task, catalog):
    """Compare capability meaning and dependencies, not their spelling/order."""
    value = primitive(task)
    value["capability_description"] = catalog.resolve(task.capability_description)
    value["dependencies"] = sorted(task.dependencies)
    return value


def preserve_requirement_names(requirements, catalog, references):
    result = deepcopy(requirements)
    for key, task in result.items():
        old = references.get(key)
        if old is not None:
            if catalog.resolve(task.capability_description) == catalog.resolve(old.capability_description):
                task.capability_description = old.capability_description
            if set(task.dependencies) == set(old.dependencies):
                task.dependencies = deepcopy(old.dependencies)
    return result


def validate_requirements(requirements, catalog, context):
    if not requirements:
        raise ValueError("empty plan")
    old = context.current_requirements
    for key, task in requirements.items():
        if key != task.task_id or not isinstance(key, str) or not key:
            raise ValueError("invalid task identity")
        if any(type(getattr(task, k)) is not int for k in ("duration", "earliest_start", "deadline")):
            raise ValueError("integer task times required")
        if task.duration <= 0 or task.earliest_start < 0 or task.earliest_start + task.duration > task.deadline:
            raise ValueError("invalid task time constraints")
        if not isinstance(task.dependencies, list) or not isinstance(task.parameters, dict) or type(task.required) is not bool:
            raise ValueError("invalid task fields")
        if not isinstance(task.capability_description, str) or any(not isinstance(d, str) for d in task.dependencies):
            raise ValueError("capability and dependency IDs must be strings")
        if len(set(task.dependencies)) != len(task.dependencies):
            raise ValueError("duplicate dependencies")
        cap = catalog.resolve(task.capability_description)
        if cap is None or task.duration != catalog.templates[cap]["duration"]:
            raise ValueError("duration must equal capability template")
        hard_deadline = context.hard_constraints.get("deadline")
        if hard_deadline is not None and task.deadline > hard_deadline:
            raise ValueError("hard deadline cannot be extended")
        if any(dep not in requirements for dep in task.dependencies):
            raise ValueError("missing dependency")
        if key in old and requirement_value(old[key], catalog) != requirement_value(task, catalog):
            if not context.adjustment_policy.get("allow_revision", False):
                raise ValueError("requirement revision not permitted")
            allowed = context.adjustment_policy.get("mutable_fields", ["deadline", "earliest_start"])
            for field_name, value in requirement_value(old[key], catalog).items():
                if value != requirement_value(task, catalog)[field_name] and field_name not in allowed:
                    raise ValueError("requirement change outside authorized fields")
    for key, previous in old.items():
        snapshot = context.execution_snapshot.get(key, {})
        status = snapshot.get("status")
        if snapshot.get("assignment_sent") or status in {TaskStatus.COMPLETED, TaskStatus.RUNNING, TaskStatus.CANCEL_PENDING, TaskStatus.ACCEPTED}:
            if key not in requirements or requirement_value(previous, catalog) != requirement_value(requirements[key], catalog):
                raise ValueError("cannot redefine completed, accepted, running or cancel-pending task")
        if key not in requirements and not context.adjustment_policy.get("allow_remove", False):
            raise ValueError("task removal not permitted")
    if old and set(requirements) - set(old) and not context.adjustment_policy.get("allow_add", False):
        raise ValueError("task addition not permitted")
    # Fixed experiment specifications are input contracts. The LLM still has to
    # propose them; invalid proposals are sent back for correction, never replaced.
    specification = context.initial_requirements if not old else None
    if context.trigger_event.get("type") == "objective_update" and "requirements" in context.trigger_event:
        specification = {row["task_id"]: TaskRequirement(**row)
                         for row in context.trigger_event["requirements"]}
    if specification:
        missing, extra = set(specification) - set(requirements), set(requirements) - set(specification)
        if missing or extra:
            raise ValueError(f"task specification mismatch: missing={sorted(missing)}, extra={sorted(extra)}; "
                             "returning results is automatic feedback, not an additional analysis task")
        for key, expected in specification.items():
            before, after = requirement_value(expected, catalog), requirement_value(requirements[key], catalog)
            changed = [name for name in before if before[name] != after[name]]
            if changed:
                raise ValueError(f"task specification mismatch for {key}: fields {changed}; "
                                 "use the supplied initial or objective-update specification")
    visiting, done = set(), set()
    def visit(key):
        if key in visiting:
            raise ValueError("cyclic dependencies")
        if key in done:
            return
        visiting.add(key)
        for dep in requirements[key].dependencies:
            visit(dep)
        visiting.remove(key)
        done.add(key)
    for key in requirements:
        visit(key)


class RuleBasedPlanner:
    def __init__(self, initial, revisions=None):
        self.initial = deepcopy(initial)
        self.revisions = deepcopy(revisions or [])
        self.revision_index = 0

    def plan(self, context):
        if not context.current_requirements:
            data = self.initial
        elif context.trigger_event.get("requirements") is not None:
            data = context.trigger_event["requirements"]
        elif self.revision_index < len(self.revisions):
            data = self.revisions[self.revision_index]
            self.revision_index += 1
        else:
            return PlanningResult(deepcopy(context.current_requirements), "no authorized change")
        tasks = {t["task_id"]: TaskRequirement(**deepcopy(t)) for t in data}
        if len(tasks) != len(data):
            raise ValueError("duplicate task IDs")
        return PlanningResult(tasks, "deterministic scenario plan")


class LLMPlanner:
    PROMPT = (
        'Return only JSON {"requirements": [...], "change_summary": "brief explanation"}. '
        'Each task has task_id (string), capability_description (string), earliest_start (integer), '
        'deadline (integer), duration (integer), dependencies (list of task IDs), parameters (object), '
        'required (boolean). All times are simulation SECONDS. '
        'For initial planning reproduce the supplied initial task specification semantically: '
        'preserve all task IDs, required flags, times, dependencies and parameters; add no tasks. '
        'Reporting the result is automatic executor feedback, NOT an extra analysis task. '
        'Capability templates describe allowed operations, not proof that a team member provides them. '
        'For objective_update use the complete requirements in trigger_event and preserve protected tasks. '
        'For matching_failed revise ONLY fields allowed by adjustment_policy, within hard constraints. '
        'Do not change completed, accepted, running, cancel-pending or already-sent tasks. '
        'Equivalent capability aliases have the same meaning. Do not assign executors or change task states. '
        'Produce the smallest compliant proposal; CP-SAT handles scheduling and executor selection.'
    )

    def __init__(self, catalog, transport=None, max_corrections=2, audit=None):
        self.catalog, self.transport, self.max_corrections = catalog, transport, max_corrections
        self.audit = audit if audit is not None else []

    @property
    def max_wall_seconds(self):
        return (self.max_corrections + 1) * getattr(self.transport, "request_budget_seconds", 120.0)

    def plan(self, context):
        if self.transport is None:
            raise RuntimeError("LLMPlanner is not configured; supply an authenticated transport using environment configuration")
        feedback = ""
        for attempt in range(self.max_corrections + 1):
            print(f"[LLM] {context.agent_id} t={context.simulation_time} "
                  f"{context.trigger_event.get('type', 'planning')} proposal {attempt + 1}", file=sys.stderr, flush=True)
            row = {"agent_id": context.agent_id, "sim_time": context.simulation_time,
                   "trigger": context.trigger_event.get("type"), "attempt": attempt + 1}
            self.audit.append(row)
            try:
                raw = self.transport(self.PROMPT, primitive(context), feedback)
            except ModelServiceError as exc:
                row.update(status="service_error", reason=str(exc))
                raise
            data = None
            try:
                data = json.loads(raw)
                items = data["requirements"]
                if not isinstance(items, list) or not isinstance(data["change_summary"], str):
                    raise ValueError("requirements must be an array and change_summary a string")
                row["proposed_requirements"] = deepcopy(items)
                row["change_summary"] = data["change_summary"]
                tasks = {t["task_id"]: TaskRequirement(**t) for t in items}
                if len(items) != len(tasks):
                    raise ValueError("duplicate task IDs")
                validate_requirements(tasks, self.catalog, context)
                tasks = preserve_requirement_names(tasks, self.catalog,
                    context.current_requirements or context.initial_requirements)
                row.update(status="accepted", accepted_requirements=primitive(tasks))
                return PlanningResult(tasks, data["change_summary"])
            except (ValueError, TypeError, KeyError) as exc:
                row.update(status="rejected", reason=str(exc))
                feedback = json.dumps({"error": str(exc), "rejected_requirements": row.get("proposed_requirements"),
                    "instruction": "Correct this proposal against the supplied specification and permissions. "
                                   "Return the full requirements array, not a patch."}, ensure_ascii=False)
                print(f"[LLM] proposal rejected: {exc}", file=sys.stderr, flush=True)
        raise PlanningValidationError("invalid LLM plan after bounded corrections: " + row["reason"])
