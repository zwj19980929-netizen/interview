"""The shell doctor reads fake exact-container metadata; never real Docker."""

import json
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


_PROJECT = Path(__file__).resolve().parents[1]
_CONTAINER = "a" * 64
_SENSITIVE = "synthetic-secret-must-not-leak"


@pytest.fixture
def local_media(tmp_path):
    project = tmp_path / "project with spaces"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    for name in ["local-media.sh", "local-media-doctor.py"]:
        shutil.copyfile(_PROJECT / "scripts" / name, scripts / name)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "python3").symlink_to(sys.executable)
    (fake_bin / "dirname").symlink_to(shutil.which("dirname"))
    (fake_bin / "awk").symlink_to(shutil.which("awk"))
    (fake_bin / "mkdir").symlink_to(shutil.which("mkdir"))
    fake = '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
arguments = sys.argv[1:]
with open(os.environ["DOCTOR_TEST_TRACE"], "a") as output:
    output.write(json.dumps([name, *arguments]) + "\\n")
config = json.loads(os.environ["DOCTOR_TEST_SCENARIO"])
key = name
if name == "docker":
    if arguments[:1] == ["compose"]:
        key = "compose" if arguments[-3:] == ["ps", "-q", "livekit"] else "mutation"
    elif arguments[:1] == ["inspect"]:
        key = "inspect"
    else:
        key = "forbidden"
if key not in config:
    sys.exit(98)
response = config[key]
sys.stdout.write(response.get("stdout", ""))
sys.stderr.write(response.get("stderr", ""))
sys.exit(response.get("status", 0))
'''
    for name in ["docker", "route", "ipconfig", "hostname"]:
        path = fake_bin / name
        path.write_text(fake)
        path.chmod(0o755)
    trace = tmp_path / "trace.jsonl"
    defaults = {
        "route": {"stdout": "route to: default\n  interface: en0\n"},
        "ipconfig": {"stdout": "192.168.0.108\n"},
        "hostname": {"status": 1},
        "compose": {"stdout": _CONTAINER + "\n"},
        "inspect": {"stdout": json.dumps(["--config", "/redacted/config", "--node-ip", "192.168.0.108"])},
    }

    def run(*, changes=None, override=None, action="doctor"):
        env = {**os.environ, "PATH": str(fake_bin), "DOCTOR_TEST_TRACE": str(trace),
               "DOCTOR_TEST_SCENARIO": json.dumps({**defaults, **(changes or {})})}
        env.pop("INTERVIEWER_LOCAL_LIVEKIT_NODE_IP", None)
        if override is not None:
            env["INTERVIEWER_LOCAL_LIVEKIT_NODE_IP"] = override
        result = subprocess.run(
            ["/bin/bash", str(scripts / "local-media.sh"), action], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10,
        )
        calls = [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
        if action == "doctor":
            docker_calls = [call for call in calls if call[0] == "docker"]
            compose = str(project / "infra/local-media/docker-compose.yml")
            assert all(call in [
                ["docker", "compose", "-f", compose, "ps", "-q", "livekit"],
                ["docker", "inspect", "--format", "{{json .Config.Cmd}}", _CONTAINER],
            ] for call in docker_calls), "Doctor must not inspect broad targets or mutate services"
            assert not (project / "data").exists()
        assert _SENSITIVE not in result.stdout + result.stderr
        return result, calls, project

    return run


def test_doctor_matches_current_mac_address_and_warns_about_media_not_signal_health(local_media):
    result, calls, _ = local_media()
    assert result.returncode == 0 and "LOCAL_MEDIA_DOCTOR_OK" in result.stdout
    assert "192.168.0.108" in result.stdout
    assert "信令健康不等于浏览器媒体健康" in result.stdout
    assert "bash scripts/local-media.sh up" in result.stdout
    assert ["ipconfig", "getifaddr", "en0"] in calls


def test_doctor_detects_stale_node_ip_without_restarting_or_leaking_command_secrets(local_media):
    result, calls, _ = local_media(changes={"inspect": {"stdout": json.dumps([
        "--config", "/redacted/config", "--keys", _SENSITIVE, "--node-ip", "192.168.0.104",
    ])}})
    assert result.returncode != 0 and "LOCAL_MEDIA_DOCTOR_IP_DRIFT" in result.stderr
    assert "192.168.0.104" in result.stderr and "192.168.0.108" in result.stderr
    assert "bash scripts/local-media.sh up" in result.stderr and "未修改任何服务" in result.stderr
    assert len([call for call in calls if call[0] == "docker"]) == 2


def test_doctor_explicit_override_is_the_comparison_target_and_skips_detection(local_media):
    result, calls, _ = local_media(override="192.168.0.104", changes={
        "inspect": {"stdout": json.dumps(["--node-ip=192.168.0.104"])},
    })
    assert result.returncode == 0 and "显式配置" in result.stdout
    assert all(call[0] == "docker" for call in calls)


def test_doctor_linux_address_fallback(local_media):
    result, calls, _ = local_media(changes={
        "route": {"status": 1}, "hostname": {"stdout": "192.168.0.108 172.16.0.1\n"},
    })
    assert result.returncode == 0 and ["hostname", "-I"] in calls
    assert not any(call[0] == "ipconfig" for call in calls)


@pytest.mark.parametrize("hostname", ["", "127.0.0.1", "unavailable"])
def test_doctor_fails_detection_instead_of_silently_accepting_loopback(local_media, hostname):
    result, calls, _ = local_media(changes={
        "route": {"status": 1, "stderr": _SENSITIVE}, "hostname": {"stdout": hostname},
    })
    assert result.returncode != 0 and "LOCAL_MEDIA_DOCTOR_DETECTION_FAILED" in result.stderr
    assert not any(call[0] == "docker" for call in calls)


@pytest.mark.parametrize("override", [_SENSITIVE, "0.0.0.0", "224.0.0.1"])
def test_doctor_invalid_override_is_rejected_without_echo_or_docker(local_media, override):
    result, calls, _ = local_media(override=override)
    assert result.returncode != 0 and "LOCAL_MEDIA_DOCTOR_DETECTION_FAILED" in result.stderr
    assert calls == []


@pytest.mark.parametrize("response, expected", [
    ({"stdout": ""}, "NOT_RUNNING"),
    ({"status": 1, "stderr": _SENSITIVE}, "DOCKER_UNAVAILABLE"),
    ({"stdout": _CONTAINER + "\n" + "b" * 64}, "CONTAINER_AMBIGUOUS"),
    ({"stdout": "--all"}, "CONTAINER_AMBIGUOUS"),
])
def test_doctor_requires_one_running_exact_container(local_media, response, expected):
    result, calls, _ = local_media(changes={"compose": response})
    assert result.returncode != 0 and "LOCAL_MEDIA_DOCTOR_" + expected in result.stderr
    assert not any(call[1] == "inspect" for call in calls if call[0] == "docker")


@pytest.mark.parametrize("command", [None, {}, "--node-ip 192.168.0.108", [], ["--node-ip"],
                                     ["--node-ip", _SENSITIVE], ["--node-ip", "192.168.0.108", "--node-ip=192.168.0.104"],
                                     ["--node-ip", 123]])
def test_doctor_does_not_guess_missing_invalid_or_ambiguous_node_ip(local_media, command):
    result, _, _ = local_media(changes={"inspect": {"stdout": json.dumps(command)}})
    assert result.returncode != 0 and "LOCAL_MEDIA_DOCTOR_NODE_IP_INVALID" in result.stderr


def test_doctor_inspect_failure_redacts_error_output(local_media):
    result, _, _ = local_media(changes={"inspect": {"status": 1, "stdout": _SENSITIVE, "stderr": _SENSITIVE}})
    assert result.returncode != 0 and "LOCAL_MEDIA_DOCTOR_INSPECT_FAILED" in result.stderr


def test_doctor_malformed_inspection_output_is_not_echoed(local_media):
    result, _, _ = local_media(changes={"inspect": {"stdout": _SENSITIVE}})
    assert result.returncode != 0 and "LOCAL_MEDIA_DOCTOR_NODE_IP_INVALID" in result.stderr


@pytest.mark.parametrize("failure", ["timeout", "unavailable"])
def test_doctor_subprocess_reads_are_bounded_and_fail_closed_without_raw_errors(monkeypatch, capsys, failure):
    spec = importlib.util.spec_from_file_location("local_media_doctor_test", _PROJECT / "scripts/local-media-doctor.py")
    doctor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(doctor)

    def failed_read(command, **kwargs):
        assert command == ["synthetic_read_only_command"]
        assert kwargs["timeout"] == 10 and kwargs["stderr"] == subprocess.DEVNULL
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 10, output=_SENSITIVE, stderr=_SENSITIVE)
        raise OSError(_SENSITIVE)

    monkeypatch.setattr(doctor.subprocess, "run", failed_read)
    assert doctor._read(["synthetic_read_only_command"]) is None
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


@pytest.mark.parametrize("action", ["up", "down"])
def test_existing_up_and_down_keep_their_original_docker_commands(local_media, action):
    result, calls, project = local_media(action=action, override="192.168.0.108", changes={"mutation": {}})
    assert result.returncode == 0
    expected = ["docker", "compose", "-f", str(project / "infra/local-media/docker-compose.yml")]
    assert calls == [expected + (["up", "-d"] if action == "up" else ["down"])]
    assert (project / "data/private-files").exists() is (action == "up")
