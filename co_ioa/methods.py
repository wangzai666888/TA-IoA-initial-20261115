"""Strategy identities are independent of socket/offline transport backends."""
from dataclasses import asdict, dataclass
from common_environment.contacts import ContactSchedule


class AdapterNotReadyError(ValueError):
    pass


@dataclass(frozen=True)
class MethodSpec:
    method_id: str
    strategy: str
    uses_forecast: bool | None
    runnable: bool
    missing_steps: tuple[int, ...] = ()
    policy_version: str = "unchanged"


METHODS = {
    "co_ioa": MethodSpec("co_ioa", "CO-IoA LLM planning + CP-SAT + CO controller", True, True),
    "ioa_original": MethodSpec("ioa_original", "upstream native CommunicationLayer", None, True),
    "co_ioa_no_forecast": MethodSpec("co_ioa_no_forecast", "CO-IoA without forecast-based planning", False, True, (), "v2"),
}


def method_spec(name):
    if name not in METHODS:
        raise ValueError("unknown method: " + str(name))
    return METHODS[name]


def require_runnable(name):
    spec = method_spec(name)
    if not spec.runnable:
        remaining = ", ".join(str(step) for step in spec.missing_steps)
        raise AdapterNotReadyError(
            f"ioa_original adapter not ready: steps {remaining} remain "
            "(native end-to-end mechanism and LLM trial). No CO-IoA fallback is allowed.")
    return spec


class NoForecastSchedule:
    """Planning assumption only: immediate dispatch plus constant link latency.

    No physical/predicted windows are stored or inspected. Actual transmission
    still uses CommunicationSimulator.truth. No arrival time is promised.
    """
    windows = ()
    version = 0

    def __init__(self, latency=0):
        self.latency = latency

    def next_delivery(self, sender, receiver, now):
        return now, now if sender == receiver else now + self.latency


def planning_schedule(spec, scenario):
    if not spec.uses_forecast:
        return NoForecastSchedule(scenario.get("latency", 0))
    return ContactSchedule(scenario.get("predicted_windows", scenario["windows"]), scenario.get("latency", 0))


def method_catalog():
    return [asdict(spec) for spec in METHODS.values()]
