import os
from typing import Optional

from app.file_storage.aliyun_oss import AliyunOssFileAdapter
from app.file_storage.interface import PrivateFileStorage
from app.file_storage.local import LocalPrivateFileAdapter


_storage: Optional[PrivateFileStorage] = None


def private_file_storage() -> PrivateFileStorage:
    global _storage
    if _storage is not None:
        return _storage
    runtime = os.getenv("INTERVIEWER_RUNTIME_ENV", "development").strip().lower()
    local_media = os.getenv("INTERVIEWER_LOCAL_MEDIA", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    # 单一开发开关明确选择本地私有存储，不再要求再配第二个后端参数。
    backend = (
        "local"
        if local_media and runtime == "development"
        else os.getenv("INTERVIEWER_FILE_STORAGE_BACKEND", "local").strip().lower()
    )
    if backend == "local":
        _storage = LocalPrivateFileAdapter()
    elif backend in {"aliyun", "aliyun_oss", "oss"}:
        _storage = AliyunOssFileAdapter()
    else:
        raise RuntimeError("Unsupported INTERVIEWER_FILE_STORAGE_BACKEND: %s" % backend)
    return _storage


def reset_private_file_storage_for_tests() -> None:
    global _storage
    _storage = None
