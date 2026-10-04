"""Offline tests for the Colab session helpers (no CLI calls, no network)."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import colab_keepalive as ck  # noqa: E402


def _jwt(exp: float) -> str:
    def b64(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{b64({'alg': 'ES256'})}.{b64({'aud': 'gpu-a100-hm-x', 'exp': int(exp), 'port': 8080})}.sig"


@pytest.fixture
def cli_state(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions.json"
    monkeypatch.setattr(ck, "SESSIONS_JSON", sessions)
    monkeypatch.setattr(ck, "STATE_DIR", tmp_path / "keepalive")
    return sessions


def test_token_seconds_left_reads_the_jwt_expiry(cli_state):
    cli_state.write_text(json.dumps({"s1": {"token": _jwt(time.time() + 1800), "endpoint": "ep-1"}}))
    assert 1790 < ck.token_seconds_left("s1") <= 1800
    assert ck.token_seconds_left("missing") == float("-inf")
    assert (ck.STATE_DIR / "s1.endpoint").read_text() == "ep-1"  # remembered for re-adoption


def test_unreadable_state_counts_as_expired(cli_state):
    cli_state.write_text('{"s1": {"token": "not-a-jwt", "endpoint": "ep-1"}}')
    assert ck.token_seconds_left("s1") == float("-inf")
    cli_state.write_text('{"s1": {"tok')  # torn write
    assert ck.token_seconds_left("s1") == float("-inf")


def test_ensure_token_refreshes_only_near_expiry(cli_state, monkeypatch):
    calls = []
    monkeypatch.setattr(ck, "refresh_session", lambda s: calls.append(s) or True)
    cli_state.write_text(json.dumps({"s1": {"token": _jwt(time.time() + 3000), "endpoint": "e"}}))
    assert ck.ensure_token("s1") and calls == []
    cli_state.write_text(json.dumps({"s1": {"token": _jwt(time.time() + 300), "endpoint": "e"}}))
    assert ck.ensure_token("s1") and calls == ["s1"]


def test_daemon_pid_ignores_recycled_pids(cli_state):
    sleeper = subprocess.Popen(["sleep", "30"])
    try:
        (ck.STATE_DIR / "s1.pid").parent.mkdir(parents=True, exist_ok=True)
        (ck.STATE_DIR / "s1.pid").write_text(str(sleeper.pid))
        assert ck.daemon_pid("s1") is None  # alive, but not a keep-alive process
        assert not ck.stop_daemon("s1") and sleeper.poll() is None
    finally:
        sleeper.kill()
    (ck.STATE_DIR / "s1.pid").write_text(str(os.getpid() + 10**6))
    assert ck.daemon_pid("s1") is None


# --------------------------------------------------------------------------- in-VM keeper policy

def _keeper(monkeypatch):
    sys.modules.setdefault("websocket", __import__("types").ModuleType("websocket"))  # only used to connect
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gpu_server"))
    import keeper

    return keeper


def test_keeper_holds_until_a_time(monkeypatch):
    keeper = _keeper(monkeypatch)
    assert keeper.wanted({"hold": {"until": time.time() + 60}}, {})[0]
    assert not keeper.wanted({"hold": {"until": 0}}, {})[0]


def test_keeper_holds_while_a_process_runs_plus_grace(monkeypatch):
    keeper = _keeper(monkeypatch)
    running = {"rc": 0}
    monkeypatch.setattr(keeper.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": running["rc"]})())
    memo, cfg = {}, {"hold": {"while": "train.py", "grace_minutes": 30}}
    assert keeper.wanted(cfg, memo)[0]
    running["rc"] = 1  # the job ended: still held during the grace period ...
    assert keeper.wanted(cfg, memo)[0]
    memo["seen"] -= 31 * 60  # ... and released after it
    assert not keeper.wanted(cfg, memo)[0]


def test_keeper_follows_gateway_idle_time(monkeypatch):
    keeper = _keeper(monkeypatch)
    idle = {"s": 10}
    monkeypatch.setattr(keeper, "_json", lambda url, **k: {"idle_seconds": idle["s"]})
    assert keeper.wanted({"idle_minutes": 60}, {})[0]
    idle["s"] = 61 * 60
    assert not keeper.wanted({"idle_minutes": 60}, {})[0]
    assert keeper.wanted({"idle_minutes": 0}, {})[0]  # 0: hold forever

    def down(url, **k):
        raise OSError("gateway restarting")

    monkeypatch.setattr(keeper, "_json", down)
    memo = {"used": time.time() - 5 * 60}  # last seen in use 5 min ago: keep holding while it restarts
    assert keeper.wanted({"idle_minutes": 60}, memo)[0]
