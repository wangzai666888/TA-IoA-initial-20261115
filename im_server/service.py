"""IoA server infrastructure shared by independent coordination mechanisms."""
from fastapi import FastAPI, WebSocket
from starlette.middleware.cors import CORSMiddleware


def create_server_app(*, lifespan=None):
    app = FastAPI(lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"])
    return app


class ConnectionManager:
    def __init__(self):
        self.agent_to_websocket: dict[str, WebSocket] = {}

    async def connect(self, websocket: WebSocket, agent_name: str):
        if agent_name in self.agent_to_websocket:
            raise ValueError("agent already connected")
        await websocket.accept()
        self.agent_to_websocket[agent_name] = websocket

    async def disconnect(self, agent_name: str):
        self.agent_to_websocket.pop(agent_name, None)

    async def send_personal_message(self, receiver: str, message):
        websocket = self.agent_to_websocket.get(receiver)
        if websocket is None:
            raise ConnectionError(f"agent is not connected: {receiver}")
        await websocket.send_text(message.model_dump_json())
