import asyncio
import hmac
import logging
import os
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlparse
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

from .planning import ModelPlanner
from .store import Conflict, Store
from .tools import CatalogClient
from .workflow import Orchestrator, Submission

log = logging.getLogger(__name__)
DEMO_TOKEN = "opspilot-local-demo"


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "reject", "cancel"]


def create_app(path: Path, token: str, *, demo: bool = False, auto_worker: bool = True,
               planner: ModelPlanner | None = None) -> FastAPI:
    if not demo and len(token) < 32:
        raise ValueError("Set a random OPERATOR_TOKEN of at least 32 characters.")
    store = Store(path)
    workflow = Orchestrator(store, CatalogClient(), planner)

    async def work():
        while True:
            try:
                await workflow.tick()
            except Exception as error:
                log.error("worker_unexpected kind=%s", type(error).__name__)
            await asyncio.sleep(0.5)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        task = asyncio.create_task(work()) if auto_worker else None
        yield
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="OpsPilot", lifespan=lifespan)

    def authorize(authorization: Annotated[str | None, Header()] = None):
        if not authorization or not hmac.compare_digest(authorization.encode(), f"Bearer {token}".encode()):
            raise HTTPException(401, "operator_required")

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/config")
    def config():
        return {"demo": demo and token == DEMO_TOKEN}

    api = APIRouter(prefix="/api", dependencies=[Depends(authorize)])

    @api.get("/runs")
    def runs():
        return store.list()

    @api.post("/runs", status_code=202)
    def submit(submission: Submission, key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=100)] = None):
        if submission.fail_first and not demo:
            raise HTTPException(422, "simulation_requires_demo")
        try:
            return store.submit(submission.model_dump(mode="json"), key)
        except Conflict as error:
            raise HTTPException(409, str(error)) from error

    @api.get("/runs/{run_id}")
    def get(run_id: UUID):
        run = store.get(str(run_id))
        if run is None:
            raise HTTPException(404, "run_not_found")
        return run

    @api.post("/runs/{run_id}/decision")
    def decide(run_id: UUID, value: Decision):
        try:
            run = store.decision(str(run_id), value.decision)
        except Conflict as error:
            raise HTTPException(409, str(error)) from error
        if run is None:
            raise HTTPException(404, "run_not_found")
        return run

    @api.post("/worker/tick")
    async def tick():
        return await workflow.tick() or {"status": "idle"}

    @app.middleware("http")
    async def security(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        if not request.url.path.startswith("/api/"):
            response.headers["Content-Security-Policy"] = "default-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        return response

    app.include_router(api)
    app.mount("/", StaticFiles(directory=Path(__file__).with_name("static"), html=True), name="dashboard")
    return app


def from_env():
    demo = os.getenv("DEMO_MODE") == "true"
    token = os.getenv("OPERATOR_TOKEN") or (DEMO_TOKEN if demo else "")
    provider = os.getenv("PLANNER_PROVIDER", "rules")
    planner = None
    if provider == "openai":
        endpoint = os.getenv("PLANNER_ENDPOINT", "https://api.openai.com/v1/chat/completions")
        parsed = urlparse(endpoint)
        key, model = os.getenv("PLANNER_API_KEY"), os.getenv("PLANNER_MODEL")
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or not key or not model:
            raise ValueError("Configure a planner key, model and HTTPS endpoint without URL credentials.")
        planner = ModelPlanner(endpoint, key, model)
    elif provider != "rules":
        raise ValueError("PLANNER_PROVIDER must be rules or openai.")
    return create_app(Path(os.getenv("DATABASE_PATH", "data/runs.sqlite")), token, demo=demo, planner=planner)
