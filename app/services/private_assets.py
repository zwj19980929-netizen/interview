import base64
import hashlib
from typing import Tuple

from app.core.errors import ApiError
from app.services.resume_ingestion import SafePdfDownloader


class PrivateAssetImporter:
    def __init__(self, *, max_bytes: int = 20 * 1024 * 1024) -> None:
        self.downloader = SafePdfDownloader(max_bytes=max_bytes, accept="audio/*")
        self.max_bytes = max_bytes

    async def audio(self, uri: str, content_type: str) -> Tuple[bytes, str]:
        normalized = content_type.split(";", 1)[0].strip().lower()
        if not normalized.startswith("audio/"):
            raise ApiError("TTS_ASSET_TYPE_INVALID", "TTS asset must use an audio content type.", status_code=422)
        if uri.startswith("data:"):
            header, separator, encoded = uri.partition(",")
            if not separator or ";base64" not in header:
                raise ApiError("TTS_ASSET_URI_INVALID", "TTS data URI must be base64 encoded.", status_code=422)
            try:
                content = base64.b64decode(encoded, validate=True)
            except ValueError as exc:
                raise ApiError("TTS_ASSET_URI_INVALID", "TTS data URI is invalid.", status_code=422) from exc
        elif uri.startswith(("https://", "http://")):
            try:
                content, _ = await self.downloader.download(uri)
            except ApiError as exc:
                raise ApiError(
                    "TTS_ASSET_DOWNLOAD_FAILED",
                    "TTS provider asset could not be downloaded safely.",
                    status_code=502,
                    details={"source_code": exc.code},
                ) from exc
        else:
            raise ApiError(
                "TTS_ASSET_URI_UNMANAGED",
                "A real TTS provider must return an HTTPS or data audio asset.",
                status_code=502,
            )
        if not content or len(content) > self.max_bytes:
            raise ApiError("TTS_ASSET_SIZE_INVALID", "TTS asset is empty or exceeds the size limit.", status_code=422)
        return content, "sha256:%s" % hashlib.sha256(content).hexdigest()
