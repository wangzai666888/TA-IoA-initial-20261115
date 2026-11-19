"""Local capture capabilities. No team selection, waypoint planner or grant policy."""
from copy import deepcopy


class CaptureLocalTools:
    def __init__(self, environment, *, verify_delivery=None):
        self.environment = environment
        self.verify_delivery = verify_delivery

    def for_agent(self, agent_id):
        self.environment._require_agent(agent_id)
        return _BoundAgentTools(self, agent_id)

    def register_delivered_authorization(self, receipt):
        """Called only by a transport adapter with a real delivery ACK."""
        if self.verify_delivery is None or not self.verify_delivery(deepcopy(receipt)):
            raise PermissionError('authorization requires verified transport delivery')
        self.environment.register_authorization_delivery(deepcopy(receipt))

    def advance_one_second(self):
        self.environment.advance_one_second()

    def finish_current_time(self):
        self.environment.finish_current_time()


class _BoundAgentTools:
    def __init__(self, boundary, agent_id):
        self._boundary, self.agent_id = boundary, agent_id

    def self_state(self):
        return self._boundary.environment.self_state(self.agent_id)

    def observe(self):
        return self._boundary.environment.observe(self.agent_id)

    def move_to(self, x, y):
        return self._boundary.environment.set_waypoint(self.agent_id, (x, y))

    def try_tag(self, authorization_id):
        return self._boundary.environment.try_tag(self.agent_id, authorization_id)
