from copy import deepcopy
from .models import Message, MessageStatus, primitive


class Wrapper:
    def __init__(self, agent):
        self.agent = agent
        self.outbox = {}

    def send(self, receiver_id, message_type, payload, *, collaboration_id="mission",
             requirement_version=None, plan_version=None, correlation_id=None,
             expires_at=None, not_before=None):
        sim = self.agent.sim
        mid = sim.new_id("message")
        message = Message(mid, collaboration_id, self.agent.agent_id, receiver_id, message_type,
                          sim.now, expires_at, requirement_version, plan_version, correlation_id, deepcopy(primitive(payload)))
        self.outbox[mid] = {"message": message, "status": MessageStatus.QUEUED,
                            "not_before": sim.now if not_before is None else not_before, "sent_at": None}
        sim.message_counts["generated"] += 1
        sim.log("message_queued", self.agent.agent_id, message=message)
        return mid

    def register(self):
        return self.send(self.agent.server_id, "REGISTER", {"capabilities": self.agent.capabilities}, collaboration_id=None)

    def report_status(self, payload):
        return self.send(self.agent.server_id, "STATUS_UPDATE", payload)

    def receive(self, message, now):
        store = self.agent.store
        if message.message_id in store.processed_message_ids:
            self.agent.sim.log("duplicate_message_ignored", self.agent.agent_id, message=message)
            return
        if message.expires_at is not None and now >= message.expires_at:
            return
        store.processed_message_ids.add(message.message_id)
        store.messages.append({**deepcopy(primitive(message)), "status": MessageStatus.DELIVERED})
        self.agent.controller.handle_message(deepcopy(message))

    def invalidate_unsent(self, assignment_id):
        for row in self.outbox.values():
            message = row["message"]
            if (message.message_type == "TASK_ASSIGN" and message.payload["assignment"]["assignment_id"] == assignment_id
                    and row["sent_at"] is None and row["status"] in {MessageStatus.QUEUED, MessageStatus.EXPIRED}):
                row["status"] = MessageStatus.EXPIRED
                self.agent.sim.log("unsent_assignment_voided", self.agent.agent_id,
                                   message=message, assignment_id=assignment_id)
                return True
        return False

    def assignment_unsent(self, assignment_id):
        return any(r["sent_at"] is None and r["status"] in {MessageStatus.QUEUED, MessageStatus.EXPIRED} and r["message"].message_type == "TASK_ASSIGN"
                   and r["message"].payload["assignment"]["assignment_id"] == assignment_id for r in self.outbox.values())

    def flush_outbox(self, now):
        changed = False
        for row in list(self.outbox.values()):
            if row["status"] != MessageStatus.QUEUED:
                continue
            message = row["message"]
            if message.expires_at is not None and now >= message.expires_at:
                row["status"] = MessageStatus.EXPIRED
                self.agent.sim.message_counts["expired"] += 1
                self.agent.sim.log("message_expired", self.agent.agent_id, message=message)
                if (message.message_type == "TASK_ASSIGN"
                        and self.agent.sim.method.method_id == "co_ioa_no_forecast"):
                    # Only a local, never-sent message can trigger this feedback.
                    self.agent.controller.unsent_assignment_expired(message)
                changed = True
            elif now >= row["not_before"]:
                changed = self.agent.sim.communication.transmit(self, row, now) or changed
        return changed
