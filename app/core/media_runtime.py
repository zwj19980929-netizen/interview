"""Frozen, secret-free media configuration identity for API/worker comparison.

The digest detects routing, mode and storage-root drift, not credential-value
differences. No provider call or filesystem write is performed by this module.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path

from app.adapters.livekit_media import LiveKitConfiguration


class MediaRuntime:
    def __init__(self, configuration: LiveKitConfiguration | None = None,
                 *, private_file_root: str | None = None):
        configuration = configuration or LiveKitConfiguration.from_environment()
        root = Path(private_file_root or os.getenv("INTERVIEWER_PRIVATE_FILE_ROOT", "data/private-files"))
        # Resolution is lexical only: bootstrap must not require storage access.
        root = os.path.abspath(os.path.normpath(str(root)))
        facts = {
            "runtime_environment": configuration.runtime_environment,
            "local_media_enabled": configuration.local_media_enabled,
            "recording_storage_backend": configuration.recording_storage_backend,
            "private_file_storage_backend": configuration.private_file_storage_backend,
            "url": configuration.url,
            "egress_url": configuration.egress_url,
            "local_egress_root": configuration.local_egress_root,
            "private_file_root": root,
            "storage_endpoint": configuration.storage_endpoint,
            "storage_bucket": configuration.storage_bucket,
            "storage_region": configuration.storage_region,
            "storage_sse_algorithm": configuration.storage_sse_algorithm,
            "storage_kms_key_id": configuration.storage_kms_key_id,
            "api_key_configured": bool(configuration.api_key),
            "api_secret_configured": bool(configuration.api_secret),
            "storage_access_key_configured": bool(configuration.storage_access_key),
            "storage_secret_configured": bool(configuration.storage_secret),
        }
        digest = hashlib.sha256(b"interviewer-media-configuration.v1\0" + json.dumps(
            facts, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")).hexdigest()
        self._snapshot = {
            "schema": "media-runtime.v1",
            "mode": "local" if configuration.recording_storage_backend == "local" else "aliyun_oss",
            "configuration_sha256": digest,
            "recording_ready": configuration.recording_ready(),
            "issues": configuration.recording_readiness_issues(),
        }

    def snapshot(self) -> dict:
        return deepcopy(self._snapshot)


_MEDIA_RUNTIME = MediaRuntime()


def media_runtime_snapshot() -> dict:
    return _MEDIA_RUNTIME.snapshot()
