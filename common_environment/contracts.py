"""Public initial observations and an explicit future adapter boundary."""
from copy import deepcopy
from typing import Protocol


class MethodAdapter(Protocol):
    """Future native adapter must implement these at its own process boundary.

    No solver or CO controller may be supplied by the environment. A native
    adapter is not considered available merely because it implements a stub.
    """
    def initialize(self, initial_observation: dict) -> None: ...
    def receive(self, delivered_message: dict, at: int) -> None: ...
    def observe(self, delivered_event: dict, at: int) -> None: ...
    def drain_actions(self, at: int) -> list[dict]: ...
    def pending_local_work(self) -> bool: ...


def initial_observation(scenario, agent_id, *, include_forecast):
    own = next(a for a in scenario["agents"] if a["id"] == agent_id)
    result = {"sim_time": 0, "self": deepcopy(own), "server_id": scenario["server_id"],
              "capability_templates": deepcopy(scenario["capability_templates"])}
    if "INITIATOR" in own.get("roles", []):
        for key in ("objective", "requirements", "hard_constraints", "adjustment_policy", "revisions"):
            result[key] = deepcopy(scenario.get(key))
    if agent_id == scenario["server_id"]:
        result["initial_registry"] = deepcopy(scenario.get("initial_registry", {}))
    if include_forecast:
        if "predicted_windows" in scenario:
            result["predicted_windows"] = deepcopy(scenario["predicted_windows"])
        elif scenario.get("perfect_prediction", False):
            # Explicitly authorized t=0 prediction snapshot, never future truth.
            result["predicted_windows"] = deepcopy(scenario["windows"])
        else:
            raise ValueError("initial forecast was not declared")
    return result
