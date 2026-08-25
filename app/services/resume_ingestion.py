import asyncio
import hashlib
import ipaddress
import os
import socket
import subprocess
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Tuple
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from pypdf import PdfReader

from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.sensitive_data import SensitiveDataProtector
from app.core.time import utc_now
from app.file_storage.interface import PrivateFileStorage
from app.file_storage.local import LocalPrivateFileAdapter
from app.file_storage.provider import private_file_storage
from app.persistence.interface import Persistence, new_work_item
from app.persistence.provider import persistence_for
from app.repositories.memory import InMemoryStore


PDF_CONTENT_TYPE = "application/pdf"
DEFAULT_MAX_BYTES = 10 * 1024 * 1024


class MalwareScanner(Protocol):
    def scan(self, content: bytes) -> None: ...


class SafeDevelopmentScanner:
    """Deterministic local guard; production requires an actual scanner command."""

    def scan(self, content: bytes) -> None:
        if b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE" in content:
            raise ApiError("FILE_MALWARE_DETECTED", "The uploaded file failed malware scanning.", status_code=422)


class CommandMalwareScanner:
    def __init__(self, command: str) -> None:
        self.command = command

    def scan(self, content: bytes) -> None:
        completed = subprocess.run(
            [self.command, "--no-summary", "-"],
            input=content,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=int(os.getenv("INTERVIEWER_FILE_SCAN_TIMEOUT_SECONDS", "30")),
            check=False,
        )
        if completed.returncode == 1:
            raise ApiError("FILE_MALWARE_DETECTED", "The uploaded file failed malware scanning.", status_code=422)
        if completed.returncode != 0:
            raise ApiError("FILE_SCAN_FAILED", "The malware scanner could not verify the file.", status_code=503)


class SafePdfDownloader:
    def __init__(self, *, max_bytes: int = DEFAULT_MAX_BYTES, accept: str = PDF_CONTENT_TYPE) -> None:
        self.max_bytes = max_bytes
        self.accept = accept
        self.max_redirects = int(os.getenv("INTERVIEWER_PDF_URL_MAX_REDIRECTS", "3"))
        self.timeout = httpx.Timeout(
            connect=float(os.getenv("INTERVIEWER_PDF_URL_CONNECT_TIMEOUT_SECONDS", "5")),
            read=float(os.getenv("INTERVIEWER_PDF_URL_READ_TIMEOUT_SECONDS", "20")),
            write=5.0,
            pool=5.0,
        )

    async def download(self, source_url: str) -> Tuple[bytes, str]:
        current = source_url
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False, trust_env=False) as client:
            for hop in range(self.max_redirects + 1):
                await self._validate_url(current)
                try:
                    async with client.stream("GET", current, headers={"Accept": self.accept}) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            if hop >= self.max_redirects:
                                raise ApiError("PDF_URL_REDIRECT_LIMIT", "PDF URL exceeded the redirect limit.", status_code=422)
                            location = response.headers.get("location")
                            if not location:
                                raise ApiError("PDF_URL_REDIRECT_INVALID", "PDF URL returned an invalid redirect.", status_code=422)
                            current = urljoin(current, location)
                            continue
                        response.raise_for_status()
                        chunks: List[bytes] = []
                        size = 0
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > self.max_bytes:
                                raise ApiError("PDF_FILE_TOO_LARGE", "PDF exceeds the configured size limit.", status_code=413)
                            chunks.append(chunk)
                        return b"".join(chunks), current
                except ApiError:
                    raise
                except (httpx.TimeoutException, httpx.NetworkError) as exc:
                    raise ApiError("PDF_URL_DOWNLOAD_FAILED", "PDF URL could not be downloaded safely.", status_code=422) from exc
                except httpx.HTTPStatusError as exc:
                    raise ApiError("PDF_URL_HTTP_ERROR", "PDF URL returned a non-success response.", status_code=422) from exc
        raise ApiError("PDF_URL_DOWNLOAD_FAILED", "PDF URL could not be downloaded safely.", status_code=422)

    async def _validate_url(self, value: str) -> None:
        parsed = urlsplit(value)
        production = os.getenv("INTERVIEWER_RUNTIME_ENV", "development").lower() == "production"
        if parsed.scheme not in ({"https"} if production else {"http", "https"}):
            raise ApiError("PDF_URL_SCHEME_FORBIDDEN", "PDF URL must use HTTPS.", status_code=422)
        if parsed.username or parsed.password or not parsed.hostname:
            raise ApiError("PDF_URL_CREDENTIALS_FORBIDDEN", "Authenticated or malformed PDF URLs are not accepted.", status_code=422)
        try:
            addresses = await asyncio.to_thread(socket.getaddrinfo, parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise ApiError("PDF_URL_DNS_FAILED", "PDF URL host could not be resolved.", status_code=422) from exc
        for address in {item[4][0] for item in addresses}:
            ip = ipaddress.ip_address(address)
            if not ip.is_global:
                raise ApiError("PDF_URL_ADDRESS_FORBIDDEN", "PDF URL resolves to a non-public address.", status_code=422)


class ResumeIngestionService:
    """Owns receive/download -> quarantine -> verify -> scan -> store -> parse."""

    def __init__(
        self,
        store: InMemoryStore,
        *,
        persistence: Optional[Persistence] = None,
        storage: Optional[PrivateFileStorage] = None,
        scanner: Optional[MalwareScanner] = None,
        downloader: Optional[SafePdfDownloader] = None,
        quarantine_root: Optional[Path] = None,
    ) -> None:
        self.persistence = persistence or persistence_for(store)
        self.storage = storage or private_file_storage()
        self.max_bytes = int(os.getenv("INTERVIEWER_FILE_MAX_BYTES", str(DEFAULT_MAX_BYTES)))
        self.scanner = scanner or self._default_scanner()
        self.downloader = downloader or SafePdfDownloader(max_bytes=self.max_bytes)
        self.sensitive = SensitiveDataProtector()
        self.quarantine_root = (
            quarantine_root or Path(os.getenv("INTERVIEWER_FILE_QUARANTINE_ROOT", "data/file-quarantine"))
        ).resolve()
        self.quarantine_root.mkdir(parents=True, exist_ok=True)

    def queue_upload(
        self,
        candidate_id: str,
        *,
        file_name: str,
        content_type: str,
        content: bytes,
        idempotency_key: str = "",
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        self._validate_received_pdf(file_name, content_type, content)
        digest = "sha256:%s" % hashlib.sha256(content).hexdigest()
        return self._queue(
            candidate_id,
            file_name=file_name,
            source_type="local_upload",
            source_reference=None,
            content=content,
            content_hash=digest,
            idempotency_key=idempotency_key or digest,
            organization_id=organization_id,
        )

    def queue_url(
        self,
        candidate_id: str,
        *,
        source_url: str,
        file_name: str,
        idempotency_key: str,
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        self._validate_url_shape(source_url)
        if not idempotency_key.strip():
            raise ApiError("IDEMPOTENCY_KEY_REQUIRED", "URL imports require an Idempotency-Key header.")
        return self._queue(
            candidate_id,
            file_name=file_name or "resume.pdf",
            source_type="url_import",
            source_reference=self._redact_url(source_url),
            content=None,
            content_hash=None,
            idempotency_key=idempotency_key,
            organization_id=organization_id,
            source_url=source_url,
        )

    async def process(self, work_item_id: str, organization_id: str = "org_default") -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            work = transaction.outbox.start(work_item_id)
            resume = transaction.resume_documents.get(work["aggregate_id"])
            file_object = transaction.file_objects.get(resume["file_object_id"]) if resume else None
        if resume is None or file_object is None:
            raise RuntimeError("Resume ingestion aggregate disappeared.")
        content: bytes
        try:
            if work["payload"]["source_type"] == "url_import":
                source_url = self.sensitive.decrypt(work["payload"]["source_url_encrypted"])
                content, final_url = await self.downloader.download(source_url)
                self._write_quarantine(work["id"], content)
                file_object["source_reference"] = self._redact_url(final_url)
                file_object["source_url_hash"] = self._url_hash(final_url)
            else:
                content = self._read_quarantine(work["id"])
            self._validate_received_pdf(resume["file_name"], PDF_CONTENT_TYPE, content)
            self.scanner.scan(content)
            parsed_text, page_count = self._parse_pdf(content)
            checksum = "sha256:%s" % hashlib.sha256(content).hexdigest()
            stored = self.storage.store(
                organization_id=organization_id,
                object_id=file_object["id"],
                content=content,
                content_type=PDF_CONTENT_TYPE,
                checksum=checksum,
            )
            parsed_file_id = new_id("file")
            parsed_content = parsed_text.encode("utf-8")
            parsed_checksum = "sha256:%s" % hashlib.sha256(parsed_content).hexdigest()
            parsed_stored = self.storage.store(
                organization_id=organization_id,
                object_id=parsed_file_id,
                content=parsed_content,
                content_type="text/plain; charset=utf-8",
                checksum=parsed_checksum,
            )
            now = utc_now()
            with self.persistence.transaction(organization_id) as transaction:
                current_work = transaction.outbox.get(work_item_id)
                current_file = transaction.file_objects.get(file_object["id"])
                current_resume = transaction.resume_documents.get(resume["id"])
                current_file.update(
                    {
                        "status": "ready",
                        "storage_backend": stored.storage_backend,
                        "object_key": stored.object_key,
                        "checksum": stored.checksum,
                        "byte_count": stored.byte_count,
                        "scan_status": "clean",
                        "source_reference": file_object.get("source_reference"),
                        "source_url_hash": file_object.get("source_url_hash"),
                        "updated_at": now,
                    }
                )
                transaction.file_objects.add(
                    {
                        "id": parsed_file_id,
                        "organization_id": organization_id,
                        "purpose": "resume_parsed_text",
                        "status": "ready",
                        "storage_backend": parsed_stored.storage_backend,
                        "object_key": parsed_stored.object_key,
                        "content_type": parsed_stored.content_type,
                        "original_file_name": "%s.txt" % Path(resume["file_name"]).stem,
                        "checksum": parsed_stored.checksum,
                        "byte_count": parsed_stored.byte_count,
                        "scan_status": "derived_clean_source",
                        "source_type": "derived_from_pdf",
                        "source_reference": resume["id"],
                        "created_at": now,
                        "updated_at": now,
                    }
                )
                current_resume.update(
                    {
                        "status": "ready",
                        "file_hash": checksum,
                        "parsed_text_file_object_id": parsed_file_id,
                        "page_count": page_count,
                        "processing_error": None,
                        "updated_at": now,
                    }
                )
                transaction.file_objects.update(current_file, expected_version=current_file["version"])
                current_resume = transaction.resume_documents.update(
                    current_resume, expected_version=current_resume["version"]
                )
                transaction.outbox.complete(work_item_id, lease_token=current_work["lease_token"])
            self._discard_quarantine(work["id"])
            return current_resume
        except Exception as exc:
            self._mark_failed(work_item_id, resume["id"], file_object["id"], work["lease_token"], exc, organization_id)
            raise

    def get_job(self, work_item_id: str, organization_id: str = "org_default") -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            work = transaction.outbox.get(work_item_id)
            if work is None or work.get("kind") != "resume.ingest":
                raise ApiError("FILE_INGESTION_JOB_NOT_FOUND", "File ingestion job does not exist.", status_code=404)
            resume = transaction.resume_documents.get(work["aggregate_id"])
        return {"job": self._public_work(work), "resume_document": resume}

    def issue_content_access(
        self,
        candidate_id: str,
        resume_id: str,
        *,
        actor_id: str,
        organization_id: str = "org_default",
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            resume = transaction.resume_documents.get(resume_id)
            if resume is None or resume.get("candidate_profile_id") != candidate_id:
                raise ApiError("RESUME_DOCUMENT_NOT_FOUND", "Resume document does not exist.", status_code=404)
            file_object = transaction.file_objects.get(resume.get("file_object_id"))
            if file_object is None or file_object.get("status") != "ready" or not file_object.get("object_key"):
                raise ApiError("RESUME_FILE_NOT_READY", "Resume file is not ready for access.", status_code=409)
            grant = self.storage.issue_read_access(file_object["object_key"], expires_seconds=300)
            transaction.audit_events.add(
                {
                    "id": new_id("audit"),
                    "organization_id": organization_id,
                    "actor_id": actor_id,
                    "action": "resume.file.access_granted",
                    "resource_type": "resume_document",
                    "resource_id": resume_id,
                    "metadata": {"file_object_id": file_object["id"], "expires_seconds": 300},
                    "created_at": utc_now(),
                }
            )
        url = grant if grant.startswith(("http://", "https://")) else "/api/v1/private-files/%s" % grant
        return {"url": url, "expires_in_seconds": 300, "content_type": PDF_CONTENT_TYPE}

    def open_local_grant(self, token: str, organization_id: str = "org_default") -> Dict[str, Any]:
        if not isinstance(self.storage, LocalPrivateFileAdapter):
            raise ApiError("FILE_ACCESS_BACKEND_INVALID", "Signed local file access is unavailable.", status_code=404)
        payload = self.storage.signer.verify(token)
        object_key = str(payload["object_key"])
        with self.persistence.transaction(organization_id) as transaction:
            file_object = next(
                (item for item in transaction.file_objects.list() if item.get("object_key") == object_key),
                None,
            )
        if file_object is None or file_object.get("status") != "ready":
            raise ApiError("FILE_OBJECT_NOT_FOUND", "Private file object does not exist.", status_code=404)
        return {"content": self.storage.open(object_key), "content_type": file_object["content_type"]}

    def _queue(
        self,
        candidate_id: str,
        *,
        file_name: str,
        source_type: str,
        source_reference: Optional[str],
        content: Optional[bytes],
        content_hash: Optional[str],
        idempotency_key: str,
        organization_id: str,
        source_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self.persistence.transaction(organization_id) as transaction:
            candidate = transaction.candidate_profiles.get(candidate_id)
            if candidate is None:
                raise ApiError("CANDIDATE_PROFILE_NOT_FOUND", "Candidate profile does not exist.", status_code=404)
            existing_work = next(
                (item for item in transaction.outbox.list() if item.get("idempotency_key") == "resume.ingest:%s:%s" % (candidate_id, idempotency_key)),
                None,
            )
            if existing_work:
                existing_resume = transaction.resume_documents.get(existing_work["aggregate_id"])
                return {"job": self._public_work(existing_work), "resume_document": existing_resume}
            now = utc_now()
            versions = [item["resume_version"] for item in transaction.resume_documents.list() if item["candidate_profile_id"] == candidate_id]
            file_object = transaction.file_objects.add(
                {
                    "id": new_id("file"),
                    "organization_id": organization_id,
                    "purpose": "resume_pdf",
                    "status": "quarantined" if content is not None else "awaiting_download",
                    "storage_backend": None,
                    "object_key": None,
                    "content_type": PDF_CONTENT_TYPE,
                    "original_file_name": Path(file_name).name,
                    "checksum": content_hash,
                    "byte_count": len(content) if content is not None else None,
                    "scan_status": "pending",
                    "source_type": source_type,
                    "source_reference": source_reference,
                    "source_url_hash": self._url_hash(source_url) if source_url else None,
                    "created_at": now,
                    "updated_at": now,
                }
            )
            resume = transaction.resume_documents.add(
                {
                    "id": new_id("resume"),
                    "organization_id": organization_id,
                    "candidate_profile_id": candidate_id,
                    "resume_version": max(versions, default=0) + 1,
                    "file_object_id": file_object["id"],
                    "file_name": Path(file_name).name,
                    "content_type": PDF_CONTENT_TYPE,
                    "file_hash": content_hash,
                    "parsed_text_file_object_id": None,
                    "page_count": None,
                    "status": "processing",
                    "processing_error": None,
                    "created_at": now,
                    "updated_at": now,
                }
            )
            work = transaction.outbox.enqueue(
                new_work_item(
                    organization_id=organization_id,
                    kind="resume.ingest",
                    aggregate_id=resume["id"],
                    idempotency_key="resume.ingest:%s:%s" % (candidate_id, idempotency_key),
                    payload={
                        "source_type": source_type,
                        "source_url_encrypted": self.sensitive.encrypt(source_url) if source_url else None,
                        "resume_document_id": resume["id"],
                        "file_object_id": file_object["id"],
                    },
                )
            )
        if content is not None:
            try:
                self._write_quarantine(work["id"], content)
            except Exception:
                self._mark_failed(work["id"], resume["id"], file_object["id"], None, RuntimeError("quarantine write failed"), organization_id)
                raise
        return {"job": self._public_work(work), "resume_document": resume}

    def _mark_failed(
        self,
        work_id: str,
        resume_id: str,
        file_id: str,
        lease_token: Optional[str],
        error: Exception,
        organization_id: str,
    ) -> None:
        code = error.code if isinstance(error, ApiError) else "PDF_PROCESSING_FAILED"
        with self.persistence.transaction(organization_id) as transaction:
            work = transaction.outbox.get(work_id)
            resume = transaction.resume_documents.get(resume_id)
            file_object = transaction.file_objects.get(file_id)
            now = utc_now()
            if resume:
                resume.update({"status": "failed", "processing_error": {"code": code, "message": str(error)}, "updated_at": now})
                transaction.resume_documents.update(resume, expected_version=resume["version"])
            if file_object:
                file_object.update({"status": "failed", "scan_status": "failed", "updated_at": now})
                transaction.file_objects.update(file_object, expected_version=file_object["version"])
            if work and work["status"] == "running" and lease_token:
                transaction.outbox.fail(work_id, "%s: %s" % (code, error), lease_token=lease_token)

    def _validate_received_pdf(self, file_name: str, content_type: str, content: bytes) -> None:
        if len(content) > self.max_bytes:
            raise ApiError("PDF_FILE_TOO_LARGE", "PDF exceeds the configured size limit.", status_code=413)
        if not content.startswith(b"%PDF-"):
            raise ApiError("PDF_SIGNATURE_INVALID", "The uploaded file is not a valid PDF.", status_code=415)
        normalized = content_type.split(";", 1)[0].strip().lower()
        if normalized not in {PDF_CONTENT_TYPE, "application/octet-stream"}:
            raise ApiError("PDF_CONTENT_TYPE_INVALID", "Resume files must use application/pdf.", status_code=415)
        if Path(file_name).suffix.lower() != ".pdf":
            raise ApiError("PDF_FILE_NAME_INVALID", "Resume file name must end with .pdf.", status_code=415)

    def _parse_pdf(self, content: bytes) -> Tuple[str, int]:
        try:
            reader = PdfReader(BytesIO(content))
            if reader.is_encrypted:
                raise ApiError("PDF_ENCRYPTED", "Encrypted PDF resumes are not accepted.", status_code=422)
            text = "\n".join((page.extract_text() or "").strip() for page in reader.pages).strip()
        except ApiError:
            raise
        except Exception as exc:
            raise ApiError("PDF_PARSE_FAILED", "PDF content could not be parsed.", status_code=422) from exc
        if not text:
            raise ApiError("PDF_TEXT_EMPTY", "PDF contains no extractable text.", status_code=422)
        return text, len(reader.pages)

    def _write_quarantine(self, work_id: str, content: bytes) -> None:
        self._quarantine_path(work_id).write_bytes(content)

    def _read_quarantine(self, work_id: str) -> bytes:
        path = self._quarantine_path(work_id)
        if not path.exists():
            raise ApiError("PDF_QUARANTINE_MISSING", "Quarantined upload is unavailable for processing.", status_code=409)
        return path.read_bytes()

    def _discard_quarantine(self, work_id: str) -> None:
        path = self._quarantine_path(work_id)
        if path.exists():
            path.unlink()

    def _quarantine_path(self, work_id: str) -> Path:
        if not work_id.replace("_", "").replace("-", "").isalnum():
            raise ValueError("Invalid quarantine work identifier.")
        return self.quarantine_root / (work_id + ".upload")

    def _default_scanner(self) -> MalwareScanner:
        command = os.getenv("INTERVIEWER_FILE_SCANNER_COMMAND", "").strip()
        runtime = os.getenv("INTERVIEWER_RUNTIME_ENV", "development").lower()
        if command:
            return CommandMalwareScanner(command)
        if runtime == "production":
            raise RuntimeError("INTERVIEWER_FILE_SCANNER_COMMAND is required in production.")
        return SafeDevelopmentScanner()

    def _validate_url_shape(self, source_url: str) -> None:
        parsed = urlsplit(source_url)
        production = os.getenv("INTERVIEWER_RUNTIME_ENV", "development").lower() == "production"
        if parsed.scheme not in ({"https"} if production else {"http", "https"}) or not parsed.hostname:
            raise ApiError("PDF_URL_INVALID", "A public HTTPS PDF URL is required.", status_code=422)
        if parsed.username or parsed.password:
            raise ApiError("PDF_URL_CREDENTIALS_FORBIDDEN", "Authenticated PDF URLs are not accepted.", status_code=422)

    def _redact_url(self, source_url: str) -> str:
        parsed = urlsplit(source_url)
        return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))

    def _url_hash(self, source_url: str) -> str:
        return "sha256:%s" % hashlib.sha256(source_url.encode("utf-8")).hexdigest()

    def _public_work(self, work: Dict[str, Any]) -> Dict[str, Any]:
        return {key: value for key, value in work.items() if key not in {"lease_token", "payload"}}
