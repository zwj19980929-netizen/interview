import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from app.core import runtime_release as release


@pytest.fixture
def source_root(tmp_path):
    (tmp_path / "app" / "core").mkdir(parents=True)
    (tmp_path / "app" / "core" / "sample.py").write_text("VALUE = 1\n")
    (tmp_path / "main.py").write_text("import app\n")
    return tmp_path


def test_frozen_snapshot_survives_source_edit_and_new_instance_observes_change(source_root):
    running = release.RuntimeRelease(source_root)
    before = running.snapshot("api")
    assert before["source"]["status"] == "available"
    assert before["source"]["file_count"] == 2
    (source_root / "app" / "core" / "sample.py").write_text("VALUE = 2\n")
    assert running.snapshot("api") == before
    replacement = release.RuntimeRelease(source_root).snapshot("api")
    assert replacement["source"]["sha256"] != before["source"]["sha256"]
    assert replacement["process"]["instance_id"] != before["process"]["instance_id"]


def test_new_interpreter_matches_current_manifest(source_root):
    running = release.RuntimeRelease(source_root)
    old = running.snapshot("api")
    (source_root / "app" / "core" / "sample.py").write_text("VALUE = 'new'\n")
    completed = subprocess.run(
        [sys.executable, "-B", "-m", "app.operations.runtime_release", "--root", str(source_root)],
        check=True, capture_output=True, text=True,
    )
    manifest = json.loads(completed.stdout)
    assert manifest == {"schema": "runtime-release.v1", "source": release.source_manifest(source_root)}
    assert manifest["source"] != old["source"]
    assert running.snapshot("api") == old


def test_caller_cannot_mutate_identity_or_source(source_root):
    running = release.RuntimeRelease(source_root)
    expected = running.snapshot("api")
    response = running.snapshot("api")
    response["source"]["sha256"] = "forged"
    response["process"]["instance_id"] = "forged"
    response["unexpected"] = "sensitive"
    assert running.snapshot("api") == expected


def test_reads_are_bootstrap_only_and_fork_changes_identity_without_reading(source_root, monkeypatch):
    running = release.RuntimeRelease(source_root)
    parent = running.snapshot("worker")

    def forbidden_read(*args, **kwargs):
        raise AssertionError("snapshot attempted a source read")

    monkeypatch.setattr(release, "_capture_source", forbidden_read)
    assert running.snapshot("worker") == parent
    monkeypatch.setattr(release.os, "getpid", lambda: parent["process"]["pid"] + 1)
    child = running.snapshot("worker")
    assert child["source"] == parent["source"]
    assert child["process"]["role"] == "worker_child"
    assert child["process"]["instance_id"] != parent["process"]["instance_id"]
    assert child["process"]["pid"] != parent["process"]["pid"]
    assert running.snapshot("worker") == child


def test_unrelated_files_and_frontend_do_not_enter_manifest(source_root):
    expected = release.source_manifest(source_root)
    for name in [".env", "data/candidate.py", "app/core/secret.env", "app/web/gyp.py",
                 "app/core/__pycache__/ignored.py", "app/node_modules/ignored.py"]:
        target = source_root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("synthetic-sensitive-do-not-return")
    result = release.source_manifest(source_root)
    assert result == expected
    assert "synthetic-sensitive" not in json.dumps(result)
    assert str(source_root) not in json.dumps(result)


def test_manifest_includes_relative_names_and_nested_python(source_root):
    original = release.source_manifest(source_root)
    nested = source_root / "app" / "domain"
    nested.mkdir()
    (nested / "extra.py").write_text("VALUE = 1\n")
    added = release.source_manifest(source_root)
    assert added["file_count"] == original["file_count"] + 1
    assert added["sha256"] != original["sha256"]
    (nested / "extra.py").rename(nested / "renamed.py")
    renamed = release.source_manifest(source_root)
    assert renamed["file_count"] == added["file_count"]
    assert renamed["sha256"] != added["sha256"]


@pytest.mark.parametrize("missing", ["app", "main.py"])
def test_missing_source_returns_unavailable_without_paths(tmp_path, missing):
    if missing != "app":
        (tmp_path / "app").mkdir()
    if missing != "main.py":
        (tmp_path / "main.py").write_text("pass\n")
    snapshot = release.RuntimeRelease(tmp_path).snapshot("api")
    assert snapshot["source"] == {"status": "unavailable", "sha256": None, "file_count": 0}
    assert str(tmp_path) not in json.dumps(snapshot)


def test_read_failure_is_unavailable_and_does_not_escape(source_root, monkeypatch):
    def failed_read(*args, **kwargs):
        raise PermissionError("secret-path-and-credential")

    monkeypatch.setattr(release.os, "read", failed_read)
    snapshot = release.RuntimeRelease(source_root).snapshot("api")
    assert snapshot["source"]["status"] == "unavailable"
    assert "secret-path-and-credential" not in json.dumps(snapshot)


@pytest.mark.parametrize("target", ["main.py", "app/core/sample.py", "app"])
def test_source_symlinks_never_read_external_target(source_root, tmp_path_factory, monkeypatch, target):
    outside = tmp_path_factory.mktemp("outside")
    external = outside / ("external.py" if target != "app" else "external")
    if target == "app":
        external.mkdir()
        (external / "secret.py").write_text("sensitive")
        original = source_root / "app"
        original.rename(source_root / "saved-app")
    else:
        external.write_text("sensitive")
        (source_root / target).unlink()
    (source_root / target).symlink_to(external, target_is_directory=target == "app")
    reads = []
    real_read = os.read

    def tracked_read(descriptor, count):
        result = real_read(descriptor, count)
        reads.append(result)
        return result

    monkeypatch.setattr(release.os, "read", tracked_read)
    assert release.source_manifest(source_root)["status"] == "unavailable"
    assert b"sensitive" not in reads


def test_linked_subdirectory_is_not_followed_or_silently_omitted(source_root, tmp_path_factory):
    outside = tmp_path_factory.mktemp("linked-code")
    (outside / "secret.py").write_text("must-not-read")
    (source_root / "app" / "linked").symlink_to(outside, target_is_directory=True)
    assert release.source_manifest(source_root) == {
        "status": "unavailable", "sha256": None, "file_count": 0,
    }


def test_special_python_file_does_not_block_or_enter_manifest(source_root):
    os.mkfifo(source_root / "app" / "named_pipe.py")
    assert release.source_manifest(source_root)["status"] == "unavailable"


def test_worker_command_is_read_only_inspection_and_has_no_business_task(monkeypatch):
    from celery.worker.control import Panel
    from app.workers.celery_app import celery_app

    state = SimpleNamespace()
    expected = release.runtime_release_snapshot("worker")
    assert Panel.meta["runtime_release"].type == "inspect"
    assert "runtime_release" not in celery_app.tasks
    assert Panel.data["runtime_release"](state) == expected


def test_worker_startup_logging_is_bounded_and_cannot_break_startup(monkeypatch):
    from app.workers import celery_app as worker

    messages = []
    logger = SimpleNamespace(info=lambda *args: messages.append(args))
    monkeypatch.setattr(worker, "logging", SimpleNamespace(getLogger=lambda name: logger))
    worker.log_runtime_release(sender=SimpleNamespace(credential="not-for-output"))
    assert len(messages) == 1
    assert messages[0][0] == "runtime_release %s"
    assert json.loads(messages[0][1]) == release.runtime_release_snapshot("worker")
    assert "not-for-output" not in messages[0][1]

    def unavailable_logger(*args):
        raise RuntimeError("private diagnostic details")

    logger.info = unavailable_logger
    worker.log_runtime_release()


def test_module_snapshot_is_frozen_without_exposing_paths_or_source():
    snapshot = release.runtime_release_snapshot("api")
    assert set(snapshot) == {"schema", "process", "source"}
    assert snapshot["schema"] == "runtime-release.v1"
    assert set(snapshot["process"]) == {"instance_id", "pid", "started_at", "role"}
    assert set(snapshot["source"]) == {"status", "sha256", "file_count"}
    assert snapshot["source"]["status"] == "available"
    assert len(snapshot["source"]["sha256"]) == 64
    assert snapshot["source"]["file_count"] > 0


def test_roles_are_bounded(source_root):
    running = release.RuntimeRelease(source_root)
    with pytest.raises(ValueError, match="unsupported runtime role"):
        running.snapshot("candidate-name-or-secret")


def test_offline_manifest_returns_nonzero_for_unavailable_source(tmp_path, capsys):
    from app.operations.runtime_release import main

    assert main(["--root", str(tmp_path)]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "schema": "runtime-release.v1",
        "source": {"status": "unavailable", "sha256": None, "file_count": 0},
    }
