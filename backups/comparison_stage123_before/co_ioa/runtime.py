"""Local mechanism adapter; HTTP and in-process modes share the same operations."""
from copy import deepcopy
from queue import Queue
from .models import Message


class AgentRuntime:
    def __init__(self, agent):
        self.agent = agent
        self.inbox = Queue()

    def enqueue(self, message):
        self.inbox.put(deepcopy(message))

    def call(self, operation, payload=None):
        a, payload = self.agent, payload or {}
        if operation == "initialize":
            a.controller.initialize()
        elif operation == "complete":
            return a.executor.complete(a.sim.now)
        elif operation == "coordinate":
            driven = a.controller.drive()
            return a.executor.start(a.sim.now) or driven
        elif operation == "flush":
            return a.wrapper.flush_outbox(a.sim.now)
        elif operation == "timeout":
            a.controller.timeout()
        elif operation == "receive":
            message = Message(**self.inbox.get_nowait())
            if message.message_id != payload["message_id"]:
                raise ValueError("inbox delivery order differs from transport")
            a.wrapper.receive(message, payload["arrival"])
        elif operation == "observe":
            a.controller.observe_contact_change(payload)
        elif operation == "objective_update":
            c = a.controller.collab
            a.wrapper.send(c.coordinator_id or a.agent_id, "OBJECTIVE_UPDATE", payload)
        elif operation == "end":
            a.controller.end(payload["outcome"], payload["reason"])
        else:
            raise ValueError("unknown local agent operation: " + operation)
        return False
