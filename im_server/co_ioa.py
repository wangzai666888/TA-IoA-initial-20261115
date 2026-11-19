"""IoA relay mode: routes envelopes, never executes a coordination state machine."""
import secrets
from fastapi import WebSocket, WebSocketDisconnect
from common.ioa_transport import TransportEnvelope
from im_server.service import ConnectionManager, create_server_app


def create_co_ioa_server(tokens):
    app = create_server_app()
    manager = ConnectionManager()
    audit, pending = [], {}
    app.state.connection_manager = manager
    app.state.transport_audit = audit

    @app.get("/health")
    async def health():
        return {"status": "ok", "mode": "co_ioa_infrastructure"}

    @app.websocket("/ws/{agent_name}")
    async def endpoint(websocket: WebSocket, agent_name: str):
        supplied = websocket.query_params.get("token", "")
        if agent_name not in tokens or not secrets.compare_digest(supplied, tokens[agent_name]):
            await websocket.close(code=1008)
            return
        if agent_name in manager.agent_to_websocket:
            await websocket.close(code=1008)
            return
        await manager.connect(websocket, agent_name)
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    envelope = TransportEnvelope.model_validate_json(raw)
                    if envelope.sender != agent_name:
                        raise ValueError("sender does not match authenticated connection")
                    if envelope.kind == "message":
                        if envelope.receiver not in tokens:
                            raise ValueError("unknown receiver")
                        key = (envelope.sender, envelope.message_id)
                        previous = pending.get(key)
                        data = envelope.model_dump()
                        if previous and previous != data:
                            raise ValueError("message ID reused with different content")
                        pending[key] = data
                        await manager.send_personal_message(envelope.receiver, envelope)
                        audit.append({"event": "routed", **data})
                    elif envelope.kind == "ingress_ack":
                        key = (envelope.receiver, envelope.message_id)
                        original = pending.get(key)
                        if original is None or original["receiver"] != agent_name:
                            raise ValueError("unrecognized ingress acknowledgement")
                        await manager.send_personal_message(envelope.receiver, TransportEnvelope(
                            message_id=envelope.message_id, sender=agent_name, receiver=envelope.receiver,
                            kind="receipt"))
                        audit.append({"event": "ingress_ack", **envelope.model_dump()})
                    else:
                        raise ValueError("clients cannot submit transport receipts")
                except (ValueError, ConnectionError) as exc:
                    await websocket.send_text(TransportEnvelope(message_id="error", sender="relay",
                        receiver=agent_name, kind="error", content=str(exc)).model_dump_json())
        except WebSocketDisconnect:
            pass
        finally:
            await manager.disconnect(agent_name)

    return app
