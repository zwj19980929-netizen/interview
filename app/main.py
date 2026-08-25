import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.core.errors import ApiError, api_error_handler, persistence_error_handler, provider_error_handler
from app.model_gateway.errors import ProviderError
from app.persistence.errors import PersistenceError


WEB_DIR = Path(__file__).resolve().parent / "web"


def create_app() -> FastAPI:
    app = FastAPI(
        title="Interviewer API",
        version="0.1.0",
        description="Real-time AI interviewer backend MVP.",
    )
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(ProviderError, provider_error_handler)
    app.add_exception_handler(PersistenceError, persistence_error_handler)
    app.include_router(router)
    app.mount("/web", StaticFiles(directory=WEB_DIR), name="web")
    media_dir = Path(os.getenv("INTERVIEWER_MEDIA_PATH", "data/media"))
    media_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/media", StaticFiles(directory=media_dir), name="media")

    @app.get("/", include_in_schema=False)
    async def web_console() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    return app


app = create_app()
