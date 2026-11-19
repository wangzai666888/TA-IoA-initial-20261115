"""Local Agent service adapter; owns only this node's runtime."""
import secrets
from fastapi import HTTPException, Request
from im_client.service import create_agent_app


def create_co_ioa_agent(runtime, control_key):
    app = create_agent_app()
    app.state.runtime = runtime

    @app.post("/co-ioa/control")
    async def control(body: dict, request: Request):
        key = request.headers.get("authorization", "").removeprefix("Bearer ")
        if not secrets.compare_digest(key, control_key):
            raise HTTPException(403, "invalid local control key")
        try:
            result = runtime.call(body["operation"], body.get("payload"))
            return {"agent_id": runtime.agent.agent_id, "changed": bool(result)}
        except (ValueError, KeyError) as exc:
            raise HTTPException(409, str(exc)) from exc

    return app
