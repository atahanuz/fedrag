"""Keep this Colab VM alive from the inside while it is in use.

Colab releases a VM about 20 minutes after the last kernel-websocket traffic through its proxy.
Nothing that stays inside the VM counts, not even a busy kernel, but a websocket that the VM opens to
its own kernel through its *public* proxy URL does (measured 2026-10-04, see COLAB_GUIDE.md). The
keeper holds one such connection while its policy says the VM is in use, then closes it, and Colab
releases the VM about 20 minutes later. So the VM no longer depends on a laptop's heartbeat.

/content/colab_proxy.json holds the proxy ``url`` and a ``token`` (written by scripts/colab_up.py or
scripts/colab_keepalive.py and refreshed by their heartbeats) and the policy, re-read every 30 s:

    "idle_minutes": 60          (default) while the fedrag gateway served a request this recently; 0 = always
    "hold": {"while": "train.py", "grace_minutes": 30}   while a matching process runs, and a while after
    "hold": {"until": 1791200000}                        until this Unix time

A token expires an hour after it is issued, but it is only checked when connecting: an open connection
outlives it. A fresh token matters only if the connection drops and has to be reopened.

    python keeper.py            # run in the foreground (launch.py starts it detached)
    python keeper.py --status   # print the keeper state as JSON
"""

from __future__ import annotations

import argparse
import json
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import websocket

PROXY_FILE = Path("/content/colab_proxy.json")
STATE_FILE = Path("/content/logs/keeper_state.json")
KERNEL_FILE = Path("/content/logs/keeper_kernel")
JUPYTER = "http://localhost:8080"
GATEWAY_HEALTH = "http://127.0.0.1:8000/health"
CHECK_S = 30


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def _json(url: str, data: bytes | None = None, timeout: float = 10.0):
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def wanted(cfg: dict, memo: dict) -> tuple[bool, str]:
    """Whether the policy in ``cfg`` wants the VM held right now, and a short reason."""
    now, hold = time.time(), cfg.get("hold") or {}
    if "until" in hold:
        return now < hold["until"], f"until {time.strftime('%H:%M', time.gmtime(hold['until']))} UTC"
    if "while" in hold:
        if subprocess.run(["pgrep", "-f", hold["while"]], capture_output=True).returncode == 0:
            memo["seen"] = now
        grace = float(hold.get("grace_minutes", 30))
        return now - memo.setdefault("seen", now) < grace * 60, f"while '{hold['while']}' runs (+{grace:g} min)"
    try:
        memo["used"] = now - _json(GATEWAY_HEALTH, timeout=8)["idle_seconds"]
    except Exception:
        pass  # gateway down or restarting: idle time keeps growing from its last known value
    idle, limit = now - memo.setdefault("used", now), float(cfg.get("idle_minutes", 60))
    return limit <= 0 or idle < limit * 60, f"gateway idle {idle / 60:.0f} of {limit:g} min"


def kernel_id() -> str:
    """The keeper's own kernel, started once, so that its connection never shares the user's kernel."""
    mine = KERNEL_FILE.read_text().strip() if KERNEL_FILE.exists() else None
    if mine and mine in {k["id"] for k in _json(f"{JUPYTER}/api/kernels")}:
        return mine
    mine = _json(f"{JUPYTER}/api/kernels", data=b"{}", timeout=60)["id"]
    KERNEL_FILE.write_text(mine)
    return mine


class Link:
    """One kernel websocket from this VM to itself, through the public Colab proxy."""

    def __init__(self) -> None:
        self.app: websocket.WebSocketApp | None = None
        self.thread: threading.Thread | None = None
        self.opened_at: float | None = None
        self.last_error: str | None = None
        self.next_try = 0.0  # back off while connecting fails (an expired token, say)
        self.failures = 0

    @property
    def up(self) -> bool:
        return self.thread is not None and self.thread.is_alive() and self.opened_at is not None

    def open(self) -> None:
        if time.time() < self.next_try:
            return
        self.failures += 1  # reset by _on_open
        self.next_try = time.time() + min(30 * 2 ** self.failures, 600)
        proxy = json.loads(PROXY_FILE.read_text())
        token = proxy["token"]
        query = urllib.parse.urlencode({"session_id": uuid.uuid4().hex, "token": token,
                                        "colab-runtime-proxy-token": token, "authuser": "0"})
        url = proxy["url"].rstrip("/").replace("https://", "wss://", 1) + f"/api/kernels/{kernel_id()}/channels?{query}"
        self.opened_at = None
        self.app = websocket.WebSocketApp(
            url, header=[f"X-Colab-Runtime-Proxy-Token: {token}", "X-Colab-Client-Agent: colab-cli"],
            on_open=self._on_open, on_close=self._on_close, on_error=self._on_error)
        self.thread = threading.Thread(target=self.app.run_forever,
                                       kwargs={"ping_interval": 60, "ping_timeout": 20}, daemon=True)
        self.thread.start()

    def close(self) -> None:
        if self.app is not None:
            self.app.close()

    def _on_open(self, ws) -> None:
        self.opened_at, self.last_error, self.failures, self.next_try = time.time(), None, 0, 0.0
        log("self-connection open")

    def _on_close(self, ws, code, reason) -> None:
        if self.opened_at is not None:
            log(f"self-connection closed after {(time.time() - self.opened_at) / 60:.0f} min ({code} {reason or ''})")
        self.opened_at = None

    def _on_error(self, ws, err) -> None:
        self.last_error = repr(err)[:300]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()
    if args.status:
        print(STATE_FILE.read_text() if STATE_FILE.exists() else json.dumps({"keeper": "no state"}))
        return

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    link, memo = Link(), {}
    log("keeper started")
    while True:
        try:
            cfg = json.loads(PROXY_FILE.read_text())
        except (OSError, ValueError):
            cfg = {}  # not written yet: hold by the default policy, connect once it appears
        holding, why = wanted(cfg, memo)
        if holding and not link.up and (link.thread is None or not link.thread.is_alive()):
            try:
                link.open()
                for _ in range(20):  # give the handshake a moment, so the state below is accurate
                    if link.up or not (link.thread and link.thread.is_alive()):
                        break
                    time.sleep(0.5)
            except Exception as e:  # no proxy file yet, Jupyter not up, ...
                link.last_error = repr(e)[:300]
        elif not holding and link.up:
            log(f"no longer in use ({why}): closing the self-connection; Colab releases the VM in ~20 min")
            link.close()
        STATE_FILE.write_text(json.dumps({
            "holding": holding, "policy": why, "connected": link.up,
            "connected_since": round(time.time() - link.opened_at) if link.up else None,
            "last_error": link.last_error, "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }))
        time.sleep(CHECK_S)


if __name__ == "__main__":
    main()
