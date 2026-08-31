import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from app.db import SessionLocal, create_db_and_tables
from app.routers import (
    act,
    agent_runs,
    atlas_settings,
    chat,
    codex_settings,
    functions,
    integrations,
    invocation_approvals,
    memory_facts,
    permission_requests,
    schedules,
    skill_generation_requests,
    skills,
    usage,
    web_apps,
)
from app.services.act_turn_dispatcher import act_turn_dispatcher
from app.services.atlas_lifecycle_service import atlas_lifecycle_service
from app.services.atlas_settings_service import build_default_atlas_settings_service
from app.services.codex_usage_service import codex_usage_service
from app.services.invocation_approval_service import InvocationApprovalService
from app.services.proposed_skill_service import ProposedSkillService
from app.services.scheduler_service import SchedulerService
from app.services.telegram_service import start_telegram_pollers, stop_telegram_pollers
from app.services.web_app_runtime_service import WebAppRuntimeConfig, WebAppRuntimeService

GOOGLE_OAUTH_CALLBACK_PATHS = (
    "/settings/integrations/google-calendar/oauth/callback",
    "/settings/integrations/gmail/oauth/callback",
)


class OAuthCallbackAccessLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not isinstance(record.args, tuple) or len(record.args) < 3:
            return True
        path = record.args[2]
        callback_path = next(
            (item for item in GOOGLE_OAUTH_CALLBACK_PATHS if isinstance(path, str) and path.startswith(f"{item}?")),
            None,
        )
        if callback_path is not None:
            args = list(record.args)
            args[2] = callback_path
            record.args = tuple(args)
        return True


logging.getLogger("uvicorn.access").addFilter(OAuthCallbackAccessLogFilter())


def stop_idle_web_app_instances(config: WebAppRuntimeConfig) -> None:
    maintenance_db = SessionLocal()
    try:
        WebAppRuntimeService(maintenance_db, config=config).stop_idle_instances()
    finally:
        maintenance_db.close()


async def web_app_runtime_maintenance(config: WebAppRuntimeConfig) -> None:
    while True:
        await asyncio.sleep(max(1, min(config.maintenance_interval_seconds, 60)))
        await asyncio.to_thread(stop_idle_web_app_instances, config)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    create_db_and_tables()
    recovery_db = SessionLocal()
    try:
        InvocationApprovalService(recovery_db).recover()
    finally:
        recovery_db.close()
    telegram_pollers = start_telegram_pollers()
    act_turn_dispatcher.start()
    atlas_db = SessionLocal()
    try:
        build_default_atlas_settings_service(atlas_db).startup()
    except Exception:
        # Atlas is optional. Its bounded, sanitized lifecycle error is exposed
        # through Settings and must never prevent Eidolon startup.
        atlas_db.rollback()
    finally:
        atlas_db.close()
    app.state.atlas_lifecycle_service = atlas_lifecycle_service
    scheduler_db = SessionLocal()
    ProposedSkillService(scheduler_db).sync_installed_from_filesystem()
    scheduler_service = SchedulerService(scheduler_db)
    scheduler_service.start()
    app.state.scheduler_service = scheduler_service
    codex_usage_service.start()
    app.state.codex_usage_service = codex_usage_service
    web_app_config = WebAppRuntimeConfig.from_env()
    web_app_db = SessionLocal()
    WebAppRuntimeService(web_app_db, config=web_app_config).recover_stale_instances()
    web_app_maintenance = asyncio.create_task(web_app_runtime_maintenance(web_app_config))
    try:
        yield
    finally:
        await asyncio.to_thread(stop_telegram_pollers, telegram_pollers)
        await asyncio.to_thread(act_turn_dispatcher.stop)
        web_app_maintenance.cancel()
        try:
            await web_app_maintenance
        except asyncio.CancelledError:
            pass
        WebAppRuntimeService(web_app_db, config=web_app_config).shutdown_all()
        web_app_db.close()
        codex_usage_service.stop()
        scheduler_service.shutdown()
        scheduler_db.close()
        atlas_lifecycle_service.stop()


app = FastAPI(
    title="Eidolon Backend",
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


@app.middleware("http")
async def enforce_web_app_gateway_origin(request: Request, call_next):
    config = WebAppRuntimeConfig.from_env()
    hostname = request.headers.get("host", "").partition(":")[0].lower().rstrip(".")
    is_gateway_origin = hostname.endswith(f".{config.gateway_domain.lower()}")
    internal_prefix = "/__web_app_gateway"
    if is_gateway_origin:
        original_path = request.scope.get("path", "/")
        request.scope["path"] = f"{internal_prefix}{original_path}"
        request.scope["raw_path"] = request.scope["path"].encode("utf-8")
    elif request.url.path.startswith(internal_prefix):
        return Response(status_code=404)
    return await call_next(request)

app.include_router(memory_facts.router)
app.include_router(act.router)
app.include_router(functions.router)
app.include_router(integrations.router)
app.include_router(atlas_settings.router)
app.include_router(skills.router)
app.include_router(agent_runs.router)
app.include_router(chat.router)
app.include_router(skill_generation_requests.router)
app.include_router(permission_requests.router)
app.include_router(invocation_approvals.router)
app.include_router(schedules.router)
app.include_router(usage.router)
app.include_router(codex_settings.router)
app.include_router(web_apps.router)
app.include_router(web_apps.gateway_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
