from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.db import SessionLocal, create_db_and_tables
from app.routers import (
    agent_runs,
    chat,
    codex_settings,
    memory_facts,
    permission_requests,
    schedules,
    skill_generation_requests,
    skills,
    tools,
    usage,
)
from app.services.codex_usage_service import codex_usage_service
from app.services.scheduler_service import SchedulerService


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    create_db_and_tables()
    scheduler_db = SessionLocal()
    scheduler_service = SchedulerService(scheduler_db)
    scheduler_service.start()
    app.state.scheduler_service = scheduler_service
    codex_usage_service.start()
    app.state.codex_usage_service = codex_usage_service
    try:
        yield
    finally:
        codex_usage_service.stop()
        scheduler_service.shutdown()
        scheduler_db.close()


app = FastAPI(
    title="Personal Agent Backend",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(memory_facts.router)
app.include_router(skills.router)
app.include_router(tools.router)
app.include_router(agent_runs.router)
app.include_router(chat.router)
app.include_router(skill_generation_requests.router)
app.include_router(permission_requests.router)
app.include_router(schedules.router)
app.include_router(usage.router)
app.include_router(codex_settings.router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
