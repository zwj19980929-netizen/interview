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
    backend = os.getenv("INTERVIEWER_FILE_STORAGE_BACKEND", "local").lower()
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
