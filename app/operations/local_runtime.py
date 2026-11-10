"""Explicit development entry point shared by the API and media worker."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys


def local_runtime_command(role: str, environment: dict[str, str]) -> tuple[list[str], dict[str, str]]:
    if role not in {"api", "worker"}:
        raise ValueError("Unsupported local runtime role")
    runtime = environment.get("INTERVIEWER_RUNTIME_ENV", "development").strip().lower()
    if runtime != "development":
        raise ValueError("Local runtime is permitted only in development")
    inherited = dict(environment)
    inherited["INTERVIEWER_RUNTIME_ENV"] = "development"
    inherited["INTERVIEWER_LOCAL_MEDIA"] = "true"
    project = Path(__file__).absolute().parents[2]
    command = ([sys.executable, str(project / "main.py")] if role == "api" else
               [sys.executable, "-m", "celery", "-A", "app.workers.celery_app:celery_app",
                "worker", "--beat", "--loglevel=INFO"])
    return command, inherited


def main() -> None:
    parser = argparse.ArgumentParser(description="使用统一开发媒体配置启动 API 或 worker")
    parser.add_argument("role", choices=("api", "worker"))
    arguments = parser.parse_args()
    try:
        command, environment = local_runtime_command(arguments.role, dict(os.environ))
    except ValueError as exc:
        parser.error(str(exc))
    os.chdir(Path(__file__).absolute().parents[2])
    os.execve(command[0], command, environment)


if __name__ == "__main__":
    main()
