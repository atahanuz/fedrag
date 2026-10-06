"""The GPU gateway's LLM proxy keeps slow non-streaming calls alive through the tunnel (no GPU needed)."""

from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gpu_server"))
sys.modules.setdefault("models", types.SimpleNamespace(EMBED_MODEL="e", RERANK_MODEL="r", Embedder=object,
                                                       Reranker=object))
gateway = pytest.importorskip("gateway")


class SlowUpstream:
    def __init__(self, delay: float, status: int = 200):
        self.delay, self.status, self.calls = delay, status, 0

    async def request(self, method, path, content=None, headers=None):
        self.calls += 1
        await asyncio.sleep(self.delay)
        return types.SimpleNamespace(content=json.dumps({"ok": True, "path": path}).encode(),
                                     status_code=self.status, headers={"content-type": "application/json"})


def client(monkeypatch, delay: float, status: int = 200):
    monkeypatch.setattr(gateway, "API_KEY", "")
    monkeypatch.setattr(gateway, "PAD_AFTER", 0.2)
    monkeypatch.setattr(gateway, "PAD_EVERY", 0.1)
    up = SlowUpstream(delay, status)
    monkeypatch.setitem(gateway.state, "http", up)
    return TestClient(gateway.app), up


def test_fast_calls_keep_their_status(monkeypatch):
    c, _ = client(monkeypatch, 0.0, status=400)
    r = c.post("/v1/chat/completions", json={"model": "m"})
    assert r.status_code == 400 and r.json()["ok"] is True


def test_slow_calls_are_padded_with_whitespace_then_the_body(monkeypatch):
    c, up = client(monkeypatch, 0.6)
    r = c.post("/v1/chat/completions", json={"model": "m"})
    assert r.status_code == 200 and up.calls == 1
    assert r.text.startswith(" ") and r.json() == {"ok": True, "path": "/v1/chat/completions"}
