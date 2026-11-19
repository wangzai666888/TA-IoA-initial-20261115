from copy import deepcopy
from .state_store import StateStore
from .wrapper import Wrapper
from .model_api import make_planner
from .matcher import Matcher
from .executor import CapabilityExecutor


class Agent:
    """Same modules for all platforms; roles and capabilities are configuration."""
    def __init__(self, sim, config, catalog, scenario):
        from .collaboration_controller import CollaborationController
        self.sim, self.config = sim, deepcopy(config)
        self.agent_id = config["id"]
        self.platform = config["platform"]
        self.roles = set(config.get("roles", []))
        self.capabilities = list(config.get("capabilities", []))
        self.server_id = scenario["server_id"]
        self.catalog = catalog
        from .methods import planning_schedule
        self.known_contacts = planning_schedule(sim.method, scenario)
        self.store = StateStore()
        self.planner = make_planner(sim, catalog, scenario)
        self.matcher = Matcher(catalog, scenario.get("solver_time_limit", 1.0),
            tie_break_seed=scenario.get("seed", 0) if sim.method.method_id == "co_ioa_no_forecast" else None)
        self.wrapper = Wrapper(self)
        self.executor = CapabilityExecutor(self)
        self.controller = CollaborationController(self, scenario)
        from .runtime import AgentRuntime
        self.runtime = AgentRuntime(self)
