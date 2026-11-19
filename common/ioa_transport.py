"""Infrastructure envelope: deliberately contains no coordination state enum."""
from typing import Literal
from pydantic import BaseModel, ConfigDict


class TransportEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message_id: str
    sender: str
    receiver: str
    kind: Literal["message", "ingress_ack", "receipt", "error"] = "message"
    content: str = ""
