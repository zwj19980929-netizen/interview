import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from app.adapters.livekit_media import prepare_local_rtc_environment

# 必须在加载 LiveKit 原生 RTC 模块前处理本机代理；只在显式本地媒体模式生效。
prepare_local_rtc_environment()

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.realtime_bus import realtime_event_bus
from app.services.livekit_evidence_ingress import (
    shutdown_livekit_evidence_supervisors,
)
from app.services.interview_agent import (
    consume_interview_agent_event_bus,
    run_interview_deadline_watchdog,
    run_takeover_lease_watchdog,
)
from app.repositories.provider import get_store
from app.api.routes import router
from app.core.errors import (
    ApiError,
    api_error_handler,
    persistence_error_handler,
    provider_error_handler,
    request_validation_error_handler,
)
from app.core.auth import AuthAuditMiddleware
from app.core.access_log import install_sensitive_access_log_filter
from app.core.speech_diagnostics import configure_speech_diagnostics
from app.core.rate_limit import public_rate_limiter
from app.model_gateway.errors import ProviderError
from app.persistence.errors import PersistenceError


WEB_DIR = Path(__file__).resolve().parent / "web"
WEB_DIST_DIR = WEB_DIR / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    install_sensitive_access_log_filter()
    configure_speech_diagnostics()
    task = None
    takeover_watchdog = asyncio.create_task(
        run_takeover_lease_watchdog(
            get_store(),
            organization_id=os.getenv(
                "INTERVIEWER_ORGANIZATION_ID", "org_default"
            ),
        )
    )
    app.state.takeover_lease_watchdog = takeover_watchdog
    deadline_watchdog = asyncio.create_task(
        run_interview_deadline_watchdog(
            get_store(),
            organization_id=os.getenv(
                "INTERVIEWER_ORGANIZATION_ID", "org_default"
            ),
            interval_seconds=float(
                os.getenv("INTERVIEWER_INTERVIEW_DEADLINE_SECONDS", "15")
            ),
        )
    )
    app.state.interview_deadline_watchdog = deadline_watchdog
    event_bus = realtime_event_bus()
    if event_bus.enabled:
        task = asyncio.create_task(consume_interview_agent_event_bus())
        app.state.realtime_subscriber = task
    try:
        yield
    finally:
        takeover_watchdog.cancel()
        deadline_watchdog.cancel()
        await asyncio.gather(
            takeover_watchdog, deadline_watchdog, return_exceptions=True
        )
        if task is not None:
            task.cancel()
        await shutdown_livekit_evidence_supervisors()
        await event_bus.close()
        await public_rate_limiter.close()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Interviewer API",
        version="0.1.0",
        description="Real-time AI interviewer backend MVP.",
        lifespan=lifespan,
    )
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(ProviderError, provider_error_handler)
    app.add_exception_handler(PersistenceError, persistence_error_handler)
    app.add_exception_handler(RequestValidationError, request_validation_error_handler)
    app.add_middleware(AuthAuditMiddleware)
    app.include_router(router)
    media_dir = Path(os.getenv("INTERVIEWER_MEDIA_PATH", "data/media"))
    media_dir.mkdir(parents=True, exist_ok=True)

    @app.get("/web/styles.css", include_in_schema=False)
    async def legacy_web_stylesheet() -> FileResponse:
        return FileResponse(WEB_DIR / "styles.css")

    app.mount("/web/vendor", StaticFiles(directory=WEB_DIR / "vendor"), name="web-vendor")
    app.mount("/web", StaticFiles(directory=WEB_DIST_DIR, check_dir=False, html=True), name="web")

    @app.get("/", include_in_schema=False)
    async def web_console() -> FileResponse:
        return FileResponse(WEB_DIST_DIR / "index.html")

    return app


app = create_app()
