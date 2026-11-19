from copy import deepcopy


class ReservationLedger:
    """Atomic, idempotent interval reservations; resource rights are separate."""
    def __init__(self):
        self.entries = {}
        self.rights = {}

    def reserve(self, collaboration_id, assignments, release=()):
        staged = deepcopy(self.entries)
        for aid in release:
            staged.pop(aid, None)
        for assignment in assignments:
            row = {"collaboration_id": collaboration_id, "assignment_id": assignment.assignment_id,
                   "executor_id": assignment.executor_id, "start": assignment.planned_start_time,
                   "end": assignment.planned_end_time}
            if assignment.assignment_id in staged:
                if staged[assignment.assignment_id] != row:
                    raise ValueError("conflicting reservation retry")
                continue
            owner = self.rights.get(assignment.executor_id)
            if owner is not None and owner != collaboration_id:
                raise ValueError("resource belongs to another collaboration")
            if any(old["executor_id"] == row["executor_id"] and row["start"] < old["end"]
                   and old["start"] < row["end"] for old in staged.values()):
                raise ValueError("resource reservation overlap")
            staged[assignment.assignment_id] = row
        self.entries = staged
        for assignment in assignments:
            self.rights[assignment.executor_id] = collaboration_id

    def release(self, assignment_id):
        self.entries.pop(assignment_id, None)

    def intervals(self, agent_id, exclude=()):
        return [(r["start"], r["end"]) for aid, r in self.entries.items()
                if r["executor_id"] == agent_id and aid not in exclude]
