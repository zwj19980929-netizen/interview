import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import live_connections, router
from app.core.errors import ApiError, api_error_handler, persistence_error_handler, provider_error_handler
from app.core.auth import AuthAuditMiddleware
from app.core.rate_limit import public_rate_limiter
from app.model_gateway.errors import ProviderError
from app.persistence.errors import PersistenceError


WEB_DIR = Path(__file__).resolve().parent / "web"
WEB_DIST_DIR = WEB_DIR / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = None
    if live_connections.bus.enabled:
        task = asyncio.create_task(live_connections.consume_remote())
        app.state.realtime_subscriber = task
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
        await live_connections.bus.close()
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
    app.add_middleware(AuthAuditMiddleware)
    app.include_router(router)
    media_dir = Path(os.getenv("INTERVIEWER_MEDIA_PATH", "data/media"))
    media_dir.mkdir(parents=True, exist_ok=True)

    @app.get("/web/styles.css", include_in_schema=False)
    async def legacy_web_stylesheet() -> FileResponse:
        return FileResponse(WEB_DIR / "styles.css")

    app.mount("/web/assets", StaticFiles(directory=WEB_DIR / "assets"), name="web-assets")
    app.mount("/web/vendor", StaticFiles(directory=WEB_DIR / "vendor"), name="web-vendor")
    app.mount("/web", StaticFiles(directory=WEB_DIST_DIR, check_dir=False, html=True), name="web")

    @app.get("/", include_in_schema=False)
    async def web_console() -> FileResponse:
        return FileResponse(WEB_DIST_DIR / "index.html")

    return app


app = create_app()
