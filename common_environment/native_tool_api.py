"""HTTP boundary for native IoA simulation tools; clock route is driver-only."""
import argparse
import json
import os
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from .native_tool import NativeSimulationTools
from .native_clock import NativeSimulationClock, ClockConflict
from .native_transport import NativeWindowRouter
from .evaluator import evaluate_native


class Submission(BaseModel):
    task_id: str
    assignment_id: str
    requested_start: int = 0
    inputs: dict[str, str] = Field(default_factory=dict)
    valid_until: int | None = None
    revision_index: int | None = None


class ClockAdvance(BaseModel):
    now: int


class Activity(BaseModel):
    activity_id: str
    listener: bool = False


class ToolWait(Activity):
    assignment_id: str


class ToolResume(ToolWait):
    aborted: bool = False


class NativeRequest(BaseModel):
    operation: str
    body: dict


class NativeMessage(BaseModel):
    message: dict


class NativeAck(BaseModel):
    transit_id: str


class NativeFault(BaseModel):
    reason: str


def create_app(environment: NativeSimulationTools, *, agent_tokens: dict[str, str], control_token: str,
               clock: NativeSimulationClock | None = None,
               router: NativeWindowRouter | None = None):
    if not control_token or not agent_tokens or any(not value for value in agent_tokens.values()):
        raise ValueError("nonempty control and agent tokens are required")
    if len(set(agent_tokens.values()) | {control_token}) != len(agent_tokens) + 1:
        raise ValueError("all access tokens must be distinct")
    if set(agent_tokens) - set(environment.agents):
        raise ValueError("token assigned to an unknown scenario agent")
    app = FastAPI(title="Native IoA physical tool boundary")
    if router is not None:
        if clock is None or router.clock is not clock:
            raise ValueError('native router and physical tools must share one clock')
        clock.router = router
    native_sockets = {}

    async def deliver_native(receiver, frame):
        socket = native_sockets.get(receiver)
        if socket is None:
            raise RuntimeError('native recipient WebSocket is not connected: ' + receiver)
        await socket.send_json(frame)

    if router is not None:
        router.deliver_ws = deliver_native
    tokens_to_agents = {token: agent_id for agent_id, token in agent_tokens.items()}

    def caller(authorization):
        value = authorization.removeprefix("Bearer ") if authorization else ""
        if value not in tokens_to_agents:
            raise HTTPException(403, "invalid agent token")
        return tokens_to_agents[value]

    def driver(authorization):
        if authorization != "Bearer " + control_token:
            raise HTTPException(403, "clock control requires driver token")

    @app.post("/tasks")
    async def submit(payload: Submission, authorization: str | None = Header(default=None)):
        agent_id = caller(authorization)
        if clock is not None and not clock.has_active(agent_id):
            raise HTTPException(409, "begin a native action before submitting physical work")
        try:
            return environment.submit(agent_id=agent_id, **payload.model_dump())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/tasks/{assignment_id}")
    async def status(assignment_id: str, authorization: str | None = Header(default=None)):
        agent_id = caller(authorization)
        try:
            return environment.status(agent_id=agent_id, assignment_id=assignment_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/tasks/{assignment_id}/cancel")
    async def cancel(assignment_id: str, authorization: str | None = Header(default=None)):
        agent_id = caller(authorization)
        try:
            return environment.cancel(agent_id=agent_id, assignment_id=assignment_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.websocket('/ws/{agent_id}')
    async def native_websocket(websocket: WebSocket, agent_id: str):
        token = websocket.headers.get('authorization', '').removeprefix('Bearer ')
        if router is None or tokens_to_agents.get(token) != agent_id:
            await websocket.close(code=1008)
            return
        if agent_id in native_sockets:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        native_sockets[agent_id] = websocket
        try:
            while True:
                # Native business sends use /transport/message so their enqueue
                # response is inside the sender's current decision lease.
                await websocket.receive_text()
                await websocket.close(code=1008)
                return
        except WebSocketDisconnect:
            pass
        finally:
            if native_sockets.get(agent_id) is websocket:
                del native_sockets[agent_id]

    @app.post('/register')
    async def register_native(payload: dict, authorization: str | None = Header(default=None)):
        agent_id = caller(authorization)
        if router is None or payload.get('name') != agent_id:
            raise HTTPException(403, 'invalid bootstrap registration')
        try:
            return await router.forward_http('register', payload)
        except Exception as exc:
            raise HTTPException(502, type(exc).__name__ + ': ' + str(exc)) from exc

    @app.post('/transport/request')
    async def native_request(payload: NativeRequest, authorization: str | None = Header(default=None)):
        agent_id = caller(authorization)
        if router is None or clock is None or not clock.has_active(agent_id):
            raise HTTPException(409, 'native Server request requires active decision lease')
        try:
            return {'ticket': router.submit_http(agent_id, payload.operation, payload.body)}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get('/transport/requests/{ticket}')
    async def native_request_status(ticket: str, authorization: str | None = Header(default=None)):
        agent_id = caller(authorization)
        if router is None:
            raise HTTPException(409, 'native router not configured')
        try:
            return router.request_status(agent_id, ticket)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post('/transport/message')
    async def native_message(payload: NativeMessage, authorization: str | None = Header(default=None)):
        agent_id = caller(authorization)
        if router is None or clock is None or not clock.has_active(agent_id):
            raise HTTPException(409, 'native message requires active decision lease')
        try:
            return {'transit_ids': [row.transit_id for row in router.enqueue_group(agent_id, payload.message)]}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post('/transport/ack')
    async def native_ack(payload: NativeAck, authorization: str | None = Header(default=None)):
        agent_id = caller(authorization)
        if router is None or clock is None or not clock.has_active(agent_id):
            raise HTTPException(409, 'native delivery acknowledgement needs active decision lease')
        try:
            router.ack(agent_id, payload.transit_id)
            return {'sim_time': environment.now}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post('/clock/fault')
    async def native_fault(payload: NativeFault, authorization: str | None = Header(default=None)):
        agent_id = caller(authorization)
        if clock is None:
            raise HTTPException(409, 'native clock is not configured')
        clock.fault = clock.fault or {'agent_id': agent_id, 'reason': payload.reason[:500]}
        return {'sim_time': environment.now}

    @app.post('/transport/settle')
    async def settle_native(authorization: str | None = Header(default=None)):
        driver(authorization)
        if router is None:
            raise HTTPException(409, 'native router not configured')
        await router.settle(environment.now)
        return clock.snapshot()

    @app.get('/evaluation')
    async def external_evaluation(authorization: str | None = Header(default=None)):
        driver(authorization)
        if router is None or clock is None:
            raise HTTPException(409, 'native evaluator needs the shared router and clock')
        return evaluate_native(environment, router, clock)

    @app.get('/transport/chat_record/{comm_id}')
    async def native_chat_record(comm_id: str, authorization: str | None = Header(default=None)):
        driver(authorization)
        if router is None or comm_id not in router.chat_records:
            raise HTTPException(404, 'unknown native session')
        return router.chat_records[comm_id]

    @app.post('/clock/activity/wait-message')
    async def activity_wait_message(payload: ToolWait, authorization: str | None = Header(default=None)):
        agent_id = caller(authorization)
        if clock is None:
            raise HTTPException(409, 'clock bridge not configured')
        try:
            return clock.wait_for_message(agent_id, payload.activity_id, payload.assignment_id)
        except (KeyError, ClockConflict) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post('/clock/activity/resume-message')
    async def activity_resume_message(payload: ToolResume, authorization: str | None = Header(default=None)):
        agent_id = caller(authorization)
        if clock is None:
            raise HTTPException(409, 'clock bridge not configured')
        try:
            return clock.resume_message(agent_id, payload.activity_id, payload.assignment_id,
                                        aborted=payload.aborted)
        except (KeyError, ClockConflict) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/clock/now")
    async def now(authorization: str | None = Header(default=None)):
        caller(authorization)
        return {"sim_time": environment.now}

    @app.post("/clock/activity/begin")
    async def activity_begin(payload: Activity, authorization: str | None = Header(default=None)):
        agent_id = caller(authorization)
        if clock is None:
            raise HTTPException(409, "clock bridge not configured")
        try:
            return clock.begin(agent_id, payload.activity_id, listener=payload.listener)
        except (ValueError, ClockConflict) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/clock/activity/wait")
    async def activity_wait(payload: ToolWait, authorization: str | None = Header(default=None)):
        agent_id = caller(authorization)
        if clock is None:
            raise HTTPException(409, "clock bridge not configured")
        try:
            return clock.wait_for_tool(agent_id, payload.activity_id, payload.assignment_id)
        except (KeyError, ClockConflict) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/clock/activity/resume")
    async def activity_resume(payload: ToolResume, authorization: str | None = Header(default=None)):
        agent_id = caller(authorization)
        if clock is None:
            raise HTTPException(409, "clock bridge not configured")
        try:
            return clock.resume(agent_id, payload.activity_id, payload.assignment_id,
                                aborted=payload.aborted)
        except (KeyError, ClockConflict) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/clock/activity/end")
    async def activity_end(payload: Activity, authorization: str | None = Header(default=None)):
        agent_id = caller(authorization)
        if clock is None:
            raise HTTPException(409, "clock bridge not configured")
        try:
            return clock.end(agent_id, payload.activity_id)
        except ClockConflict as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/clock/status")
    async def clock_status(authorization: str | None = Header(default=None)):
        driver(authorization)
        return clock.snapshot() if clock is not None else {"sim_time": environment.now}

    @app.post("/clock/step")
    async def step(authorization: str | None = Header(default=None)):
        driver(authorization)
        if clock is None:
            raise HTTPException(409, "clock bridge not configured")
        try:
            return clock.advance()
        except ClockConflict as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/clock/advance")
    async def advance(payload: ClockAdvance, authorization: str | None = Header(default=None)):
        driver(authorization)
        try:
            return clock.advance(payload.now) if clock is not None else environment.advance(payload.now)
        except ClockConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", type=Path, required=True)
    parser.add_argument("--agent-token-file", type=Path, required=True,
                        help="JSON mapping scenario agent IDs to nonempty secret tokens")
    parser.add_argument("--control-token-env", default="NATIVE_SIM_CONTROL_TOKEN")
    parser.add_argument('--upstream-server-url', required=True,
                        help='Original IoA Server, reachable only by this gateway')
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=17881)
    args = parser.parse_args()
    control_token = os.environ.get(args.control_token_env, "")
    scenario = json.loads(args.scenario.read_text(encoding="utf-8"))
    agent_tokens = json.loads(args.agent_token_file.read_text(encoding="utf-8"))
    environment = NativeSimulationTools(scenario)
    clock = NativeSimulationClock(environment)

    async def forward_http(operation, body):
        import httpx
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(args.upstream_server_url.rstrip('/') + '/' + operation, json=body)
            response.raise_for_status()
            return response.json()

    router = NativeWindowRouter(environment, clock=clock, forward_http=forward_http,
                                deliver_ws=None)
    app = create_app(environment, agent_tokens=agent_tokens, control_token=control_token,
                     clock=clock, router=router)
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
