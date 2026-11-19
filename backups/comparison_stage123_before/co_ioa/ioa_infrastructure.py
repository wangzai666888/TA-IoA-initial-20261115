"""Real IoA HTTP/WebSocket infrastructure for the independent CO-IoA mechanism.

Single-process experiment harness: each service owns one AgentRuntime. Only the
clock/evaluator is shared; cross-node business messages cross real loopback sockets.
"""
import asyncio
import json
import secrets
import socket
import threading
import time
from urllib.parse import quote
import httpx
import uvicorn
from common.ioa_transport import TransportEnvelope
from im_client.communication.websocket_client import WebSocketClient
from im_client.co_ioa import create_co_ioa_agent
from im_server.co_ioa import create_co_ioa_server
from .models import primitive
from .planner import LLMPlanner


class LocalService:
    def __init__(self, app):
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.port = self.socket.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False, lifespan="on"))
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.socket]}, daemon=True)

    def start(self):
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            if not self.thread.is_alive() or time.monotonic() >= deadline:
                raise RuntimeError("local IoA service failed to start")
            time.sleep(0.01)

    def close(self):
        self.server.should_exit = True
        self.thread.join(timeout=5)
        self.socket.close()
        if self.thread.is_alive():
            raise RuntimeError("local IoA service failed to stop")


class IoAInfrastructure:
    def __init__(self, simulation):
        self.sim = simulation
        self.tokens = {name: secrets.token_urlsafe(32) for name in simulation.agents}
        self.control_key = secrets.token_urlsafe(32)
        self.server_app = create_co_ioa_server(self.tokens)
        self.relay = LocalService(self.server_app)
        self.nodes = {name: LocalService(create_co_ioa_agent(a.runtime, self.control_key))
                      for name, a in simulation.agents.items()}
        # A local control request may run the entire LLM planning cycle. Each
        # model attempt has its own timeout, and invalid plans can be retried.
        control_read_timeout = 10.0
        for agent in simulation.agents.values():
            planner = agent.planner
            if isinstance(planner, LLMPlanner):
                control_read_timeout = max(control_read_timeout,
                    planner.max_wall_seconds + 30.0)
        self.http = httpx.Client(trust_env=False,
            timeout=httpx.Timeout(10.0, read=control_read_timeout))
        self.loop = asyncio.new_event_loop()
        self.loop_thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.clients, self.listeners, self.pending = {}, {}, {}
        self.audit, self.errors = [], []
        self.http_calls = 0

    def submit(self, coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(timeout=15)

    async def connect_all(self):
        for name in self.sim.agents:
            uri = f"ws://127.0.0.1:{self.relay.port}/ws/{quote(name, safe='')}?token={self.tokens[name]}"
            client = WebSocketClient(uri, message_model=TransportEnvelope)
            await client.connect()
            if client.websocket is None:
                raise ConnectionError("IoA WebSocket connection failed")
            self.clients[name] = client
        for name, client in self.clients.items():
            self.listeners[name] = asyncio.create_task(self.listen(name, client))

    async def listen(self, name, client):
        try:
            while True:
                packet = await client.receive_message()
                if packet.receiver != name:
                    raise ValueError("wrong socket receiver")
                if packet.kind == "message":
                    data = json.loads(packet.content)
                    if data["sender_id"] != packet.sender or data["receiver_id"] != name or data["message_id"] != packet.message_id:
                        raise ValueError("business message differs from transport identity")
                    # Admission is local queueing only. Business processing is
                    # scheduled later by this Agent's HTTP control endpoint.
                    self.sim.agents[name].runtime.enqueue(data)
                    await client.send_message(TransportEnvelope(message_id=packet.message_id,
                        sender=name, receiver=packet.sender, kind="ingress_ack").model_dump_json())
                elif packet.kind == "receipt":
                    future = self.pending.get((name, packet.message_id))
                    if future and not future.done():
                        future.set_result(True)
                else:
                    raise RuntimeError("relay error: " + packet.content)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.errors.append(f"{name}: {type(exc).__name__}: {exc}")
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(RuntimeError(self.errors[-1]))

    async def exchange(self, message):
        key = (message.sender_id, message.message_id)
        future = self.loop.create_future()
        self.pending[key] = future
        try:
            packet = TransportEnvelope(message_id=message.message_id, sender=message.sender_id,
                receiver=message.receiver_id, content=json.dumps(primitive(message), ensure_ascii=False))
            await self.clients[message.sender_id].send_message(packet.model_dump_json())
            await asyncio.wait_for(future, timeout=10)
        finally:
            self.pending.pop(key, None)

    def deliver(self, message, arrival):
        if self.errors:
            raise RuntimeError(self.errors[-1])
        self.submit(self.exchange(message))
        self.call_agent(message.receiver_id, "receive", {"message_id": message.message_id, "arrival": arrival})
        self.audit.append({"sim_time": self.sim.now, "message_id": message.message_id,
            "sender": message.sender_id, "receiver": message.receiver_id,
            "message_type": message.message_type, "transport": "IoA WebSocketClient / ConnectionManager"})

    def call_agent(self, name, operation, payload=None):
        response = self.http.post(f"http://127.0.0.1:{self.nodes[name].port}/co-ioa/control",
            json={"operation": operation, "payload": payload or {}},
            headers={"Authorization": "Bearer " + self.control_key})
        response.raise_for_status()
        result = response.json()
        if result["agent_id"] != name:
            raise ValueError("wrong Agent service identity")
        self.http_calls += 1
        return result["changed"]

    def __enter__(self):
        self.loop_thread.start()
        try:
            self.relay.start()
            for node in self.nodes.values():
                node.start()
            self.submit(self.connect_all())
            self.sim.infrastructure = self
            self.sim.event_driven = True
            return self
        except BaseException:
            self.close()
            raise

    async def disconnect_all(self):
        for task in self.listeners.values():
            task.cancel()
        await asyncio.gather(*self.listeners.values(), return_exceptions=True)
        for client in self.clients.values():
            await client.close()

    def close(self):
        self.sim.infrastructure = None
        try:
            if self.loop.is_running():
                self.submit(self.disconnect_all())
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.loop_thread.join(timeout=5)
            self.loop.close()
            self.http.close()
            for node in reversed(list(self.nodes.values())):
                if node.thread.ident is not None:
                    node.close()
                else:
                    node.socket.close()
            if self.relay.thread.ident is not None:
                self.relay.close()
            else:
                self.relay.socket.close()

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            self.close()
        except Exception as cleanup_error:
            if exc_value is None:
                raise
            exc_value.add_note(
                f"IoA cleanup also failed: {type(cleanup_error).__name__}: {cleanup_error}")
        return False

    def snapshot(self):
        return {"backend": "ioa", "agent_http_calls": self.http_calls,
                "cross_node_messages": len(self.audit), "deliveries": list(self.audit),
                "relay_events": list(self.server_app.state.transport_audit), "errors": list(self.errors),
                "reuse": ["im_server.service.ConnectionManager", "im_server.service.create_server_app",
                    "im_client.service.create_agent_app", "im_client.communication.websocket_client.WebSocketClient"],
                "business_controller": "co_ioa.collaboration_controller.CollaborationController"}
