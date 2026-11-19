from pydantic import ValidationError
import websockets
import asyncio

# import logging
import logging
from common.ioa_transport import TransportEnvelope
from websockets.client import WebSocketClientProtocol
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)


class WebSocketClient:
    def __init__(self, uri, message_model=TransportEnvelope):
        self.uri = uri  # "ws://localhost:8000/ws"
        self.websocket: WebSocketClientProtocol = None
        # Transport envelopes have no business coordination state.
        self.message_model = message_model

    async def connect(self):
        try:
            self.websocket: WebSocketClientProtocol = await websockets.connect(self.uri, ping_timeout=None)
        except Exception as e:
            print(f"Websocket connection error: {e}")

    async def send_message(self, message):
        max_retries = 3
        retries = 0

        while retries < max_retries:
            if self.websocket and self.websocket.open:
                try:
                    await self.websocket.send(message)
                    break

                except ConnectionClosed as e:
                    logger.error(e)
                    retries += 1
                    await asyncio.sleep(3)
                except (TypeError, Exception) as e:
                    raise
            else:
                retries += 1
                await asyncio.sleep(3)
                await self.connect()

        if retries >= max_retries:
            raise ConnectionError("Failed to send message after several attempts.")

    async def receive_message(self):
        max_retries = 3
        retries = 0

        while retries < max_retries:
            if self.websocket and self.websocket.open:
                try:
                    message = await self.websocket.recv()
                    message = self.message_model.model_validate_json(message)
                    return message
                except (ConnectionClosed, RuntimeError) as e:
                    logger.error(e)
                    retries += 1
                    await asyncio.sleep(3)
                except (ValidationError, Exception) as e:
                    raise

            else:
                retries += 1
                await asyncio.sleep(3)
                await self.connect()
                print("reconnect succeed!")

        if retries >= max_retries:
            raise ConnectionError("Failed to receive message after several attempts.")

    async def close(self):
        if self.websocket is not None:
            await self.websocket.close()
