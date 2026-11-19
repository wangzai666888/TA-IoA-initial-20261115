"""Agent service lifecycle, independent of an agent's business state machine."""
from fastapi import FastAPI


def create_agent_app(*, lifespan=None):
    app = FastAPI(lifespan=lifespan)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    return app
