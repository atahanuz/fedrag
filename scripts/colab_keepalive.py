"""Keep Colab sessions alive. Works for any session made with the `colab` CLI.

    python scripts/colab_keepalive.py -s NAME --keeper-while train.py   # VM holds itself while train.py runs
    python scripts/colab_keepalive.py -s NAME --keeper-hours 6          # ... or for 6 hours
    python scripts/colab_keepalive.py -s NAME --keeper-release          # let Colab reclaim it (~20 min)
    python scripts/colab_keepalive.py -s NAME            # client heartbeat in the foreground (Ctrl-C stops)
    python scripts/colab_keepalive.py -s NAME --detach   # same, in the background
    python scripts/colab_keepalive.py -s NAME --stop     # stop the background heartbeat
    python scripts/colab_keepalive.py -s NAME --status

Colab sessions driven by colab-cli die for two separate reasons (measured October 2026, see COLAB_GUIDE.md):

1. Idle reclaim. Colab releases a GPU VM 20-22 minutes after the last kernel-websocket traffic through
   the Colab proxy. None of these count: the CLI's own keep-alive daemon, HTTP requests through the
   proxy, a kernel kept busy by a never-ending cell, code run through the VM's local Jupyter server, or
   traffic through other tunnels. An open kernel websocket through the proxy does count, even with no
   code running and even when the VM opens it to itself through its public proxy URL (that is what
   gpu_server/keeper.py does in this repo).
2. Token expiry. The runtime-proxy token expires one hour after it is issued, and colab-cli 0.6.0
   never refreshes it. The next `colab exec` gets a 401, and the CLI deletes the session record while
   the VM keeps running (an orphan, shown as `[?]` by `colab sessions`). Every assignment listing
   returns a fresh token, so we swap one in before the old one expires (colab-cli >= 0.7 does the same).

Two ways to keep a session alive follow from that:

* The in-VM keeper (gpu_server/keeper.py, uploaded by --keeper-*) holds a websocket from the VM to its
  own kernel through the public proxy, so the VM survives this machine sleeping or going offline. It
  needs a token only to connect, and lets go when its policy says the VM is no longer in use.
* The client heartbeat runs a no-op cell through the proxy every 4 minutes and keeps the CLI's token
  fresh (and the keeper's, if one is installed). A machine that sleeps for more than ~20 minutes
  without a keeper loses the VM, so on macOS the heartbeat holds a `caffeinate` assertion.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

NOISE = re.compile(r"new version of Colab CLI|colab update|pip install --upgrade|enable_update_check|^\s*$")
SESSIONS_JSON = Path.home() / ".config" / "colab-cli" / "sessions.json"
STATE_DIR = Path.home() / ".cache" / "colab-keepalive"
TOKEN_MARGIN_S = 600  # refresh the proxy token when it has less than this left
KEEPER_SRC = Path(__file__).resolve().parent.parent / "gpu_server" / "keeper.py"
KEEPER_FILE = "/content/colab_proxy.json"  # read by keeper.py: proxy url, token and hold policy
RETRY_S = 60  # retry a failed heartbeat this soon (the VM survives ~20 min without one)


def colab(*args: str, env: dict | None = None, timeout: int = 900, check: bool = True) -> str:
    p = subprocess.run(["colab", *args], capture_output=True, text=True, timeout=timeout,
                       env={**os.environ, **(env or {})})
    out = "\n".join(line for line in (p.stdout + p.stderr).splitlines() if not NOISE.search(line))
    if check and p.returncode != 0:
        raise RuntimeError(f"colab {' '.join(args)} failed:\n{out}")
    return out


def _state_file(session: str, suffix: str) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / f"{session}.{suffix}"


def _stored(session: str) -> dict | None:
    """The CLI's record for ``session`` (read without its lock; a torn read just returns None)."""
    try:
        rec = json.loads(SESSIONS_JSON.read_text()).get(session)
    except (OSError, ValueError):
        return None
    if rec:  # remember the endpoint, so the session can be re-adopted if the CLI drops the record
        _state_file(session, "endpoint").write_text(rec["endpoint"])
    return rec


_REFRESH = r"""
import json, sys
from colab_cli.common import State
from colab_cli.state import SessionState

session, expected = sys.argv[1], sys.argv[2]
st = State()
assignments = st.client.list_assignments()
existing = st.store.get(session)
endpoint = existing.endpoint if existing else expected
target = next((a for a in assignments if a.endpoint == endpoint), None)
if target is None:
    print(json.dumps({"ok": False}))
    sys.exit(0)
info = target.runtime_proxy_info
kernel_id = existing.kernel_id if existing else None
if existing is None:  # reattach to the most recently active kernel, keeping its in-memory state
    try:
        import requests
        kernels = requests.get(info.url.rstrip("/") + "/api/kernels", timeout=20,
                               params={"authuser": "0", "colab-runtime-proxy-token": info.token}).json()
        kernel_id = max(kernels, key=lambda k: k.get("last_activity") or "")["id"] if kernels else None
    except Exception:
        pass
st.store.add(SessionState(
    name=session, token=info.token, url=info.url, endpoint=target.endpoint, variant=target.variant.name,
    accelerator=target.accelerator.value, kernel_id=kernel_id,
    session_id=existing.session_id if existing else None,
    keep_alive_pid=existing.keep_alive_pid if existing else None,
))
print(json.dumps({"ok": True, "adopted": existing is None, "endpoint": target.endpoint}))
"""


def _colab_python() -> str:
    """The interpreter that runs the `colab` executable (where colab_cli is installed)."""
    exe = shutil.which("colab")
    if exe:
        first = open(exe, "rb").readline().decode(errors="replace").strip()
        if first.startswith("#!") and "python" in first:
            return first[2:].strip().split()[0]
    return sys.executable


def refresh_session(session: str) -> bool:
    """Give the CLI's record for ``session`` a fresh runtime-proxy token. If the CLI dropped the record
    while the VM lives on, re-adopt that VM (only the endpoint last seen under this name). Returns
    False if the session is gone. Uses the CLI's own state API."""
    _stored(session)
    endpoint_file = _state_file(session, "endpoint")
    expected = endpoint_file.read_text().strip() if endpoint_file.exists() else ""
    p = subprocess.run([_colab_python(), "-c", _REFRESH, session, expected],
                       capture_output=True, text=True, timeout=120)
    m = re.search(r"\{.*\}", p.stdout)
    if not m:
        raise RuntimeError(f"session refresh failed: {(p.stderr or p.stdout)[-300:]}")
    res = json.loads(m.group(0))
    if res.get("adopted"):
        print(f"re-adopted assignment {res['endpoint']} as session {session}", flush=True)
    return bool(res.get("ok"))


def token_seconds_left(session: str) -> float:
    """Seconds until the CLI's stored proxy token for ``session`` expires (-inf if there is none)."""
    rec = _stored(session)
    try:
        payload = rec["token"].split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["exp"] - time.time()
    except Exception:  # no record, or not a JWT
        return float("-inf")


def proxy_info(session: str, min_left_s: float = TOKEN_MARGIN_S) -> dict | None:
    """``{"url", "token"}`` of the session's runtime proxy, with at least ``min_left_s`` of validity."""
    if token_seconds_left(session) < min_left_s and not refresh_session(session):
        return None
    rec = _stored(session)
    return {"url": rec["url"], "token": rec["token"]} if rec else None


def ensure_token(session: str) -> bool:
    """Refresh the proxy token before it expires, so the CLI never gets a 401 and prunes the session."""
    if token_seconds_left(session) > TOKEN_MARGIN_S:
        return True
    return refresh_session(session)


def vm_exec(session: str, code: str, timeout: int = 600, retries: int = 3) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(code)
        path = f.name
    try:
        for attempt in range(retries):
            try:
                ensure_token(session)
                return colab("exec", "-s", session, "-f", path, "--timeout", str(timeout), timeout=timeout + 60)
            except (RuntimeError, subprocess.TimeoutExpired) as e:
                if attempt == retries - 1:
                    raise
                print(f"  exec retry ({str(e)[:120]})", flush=True)
                if "appears to be lost" in str(e):
                    refresh_session(session)  # a rejected token, not necessarily a dead VM
                time.sleep(10)  # the first exec after READY may time out while the kernel boots
    finally:
        os.unlink(path)
    return ""


_pushed: dict = {}


def push_keeper_config(session: str, policy: dict) -> None:
    """Upload the proxy URL, a token with most of its hour left, and the keeper's ``policy`` to the VM.
    Skips the upload when neither the token nor the policy changed since this process last sent them."""
    info = proxy_info(session, min_left_s=50 * 60)
    if info is None or _pushed.get(session) == (info["token"], policy):
        return
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "colab_proxy.json"
        f.write_text(json.dumps({**info, **policy}))
        colab("upload", "-s", session, str(f), KEEPER_FILE)
    _pushed[session] = (info["token"], policy)


_START_KEEPER = r"""
import os, subprocess, sys, time
os.makedirs("/content/logs", exist_ok=True)
subprocess.run(["pkill", "-f", "/content/colab_keeper.py"])
subprocess.Popen([sys.executable, "-u", "/content/colab_keeper.py"], stdout=open("/content/logs/keeper.log", "a"),
                 stderr=subprocess.STDOUT, start_new_session=True)
time.sleep(8)
print(open("/content/logs/keeper_state.json").read())
"""


def install_keeper(session: str, policy: dict) -> str:
    """Upload keeper.py with ``policy`` to the VM and (re)start it. The policy is also remembered here,
    so a client heartbeat for this session keeps the keeper's token fresh."""
    push_keeper_config(session, policy)
    ensure_token(session)
    colab("upload", "-s", session, str(KEEPER_SRC), "/content/colab_keeper.py")
    _state_file(session, "keeper").write_text(json.dumps(policy))
    return vm_exec(session, _START_KEEPER, timeout=120)


def noop_beat(session: str) -> bool:
    keeper = _state_file(session, "keeper")
    if keeper.exists():
        push_keeper_config(session, json.loads(keeper.read_text()))
    vm_exec(session, "pass", timeout=60)
    return True


def _hold_awake() -> None:
    """macOS: prevent idle (and, on AC power, system) sleep for as long as this process lives."""
    if sys.platform == "darwin" and shutil.which("caffeinate"):
        subprocess.Popen(["caffeinate", "-i", "-s", "-w", str(os.getpid())])


def keep_alive(session: str, beat: Callable[[str], bool] = noop_beat, interval_s: int = 240,
               stay_awake: bool = True) -> None:
    """Call ``beat(session)`` every ``interval_s`` seconds until it returns False or the session is gone.

    ``beat`` must reach the kernel through the Colab proxy (``vm_exec`` does); HTTP requests or work on
    the VM do not reset Colab's idle timer. A failed beat is retried after a minute.
    """
    if stay_awake:
        _hold_awake()
    print(f"keep-alive for {session}: heartbeat every {interval_s}s", flush=True)
    while True:
        try:
            if not ensure_token(session):
                print(f"{time.strftime('%H:%M:%S')} session {session} is gone; keep-alive exits", flush=True)
                _state_file(session, "keeper").unlink(missing_ok=True)
                return
            if not beat(session):
                return
            time.sleep(interval_s)
        except Exception as e:  # network blips, a busy kernel, a transient proxy error
            print(f"{time.strftime('%H:%M:%S')} heartbeat failed: {str(e)[-300:]}", flush=True)
            time.sleep(min(RETRY_S, interval_s))


def daemon_pid(session: str) -> int | None:
    """PID of the background heartbeat for ``session``, if one is running."""
    try:
        pid = int(_state_file(session, "pid").read_text())
        cmd = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
    except (OSError, ValueError):
        return None
    return pid if "--keepalive" in cmd or "colab_keepalive" in cmd else None  # not a recycled PID


def detach(session: str, argv: list[str]) -> Path:
    """Re-run ``argv`` (a foreground keep-alive command line) in the background, logging to a file."""
    stop_daemon(session)
    log_path = _state_file(session, "log")
    p = subprocess.Popen([sys.executable, *argv], stdout=open(log_path, "a"), stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, start_new_session=True, env={**os.environ, "PYTHONUNBUFFERED": "1"})
    _state_file(session, "pid").write_text(str(p.pid))
    return log_path


def stop_daemon(session: str) -> bool:
    pid = daemon_pid(session)
    _state_file(session, "pid").unlink(missing_ok=True)
    if pid is None:
        return False
    os.killpg(pid, signal.SIGTERM)  # the daemon leads its own process group (start_new_session)
    return True


def describe(session: str) -> str:
    pid, left = daemon_pid(session), token_seconds_left(session)
    return (f"background heartbeat: {'pid ' + str(pid) if pid else 'not running'}; proxy token: "
            f"{'none' if left == float('-inf') else f'{left / 60:.0f} min left'}; "
            f"log: {_state_file(session, 'log')}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-s", "--session", required=True)
    ap.add_argument("--interval", type=int, default=240, help="seconds between heartbeats (Colab idles out at ~20 min)")
    ap.add_argument("--detach", action="store_true", help="run in the background")
    ap.add_argument("--stop", action="store_true", help="stop the background heartbeat")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--allow-sleep", action="store_true", help="do not hold a caffeinate assertion (macOS)")
    ap.add_argument("--keeper-while", metavar="PATTERN", help="VM keeps itself alive while `pgrep -f PATTERN` matches")
    ap.add_argument("--grace-minutes", type=float, default=30, help="with --keeper-while: hold this long after")
    ap.add_argument("--keeper-hours", type=float, help="VM keeps itself alive for this many hours")
    ap.add_argument("--keeper-release", action="store_true", help="tell the keeper to let go")
    args = ap.parse_args()

    if args.keeper_while or args.keeper_hours or args.keeper_release:
        policy = ({"hold": {"while": args.keeper_while, "grace_minutes": args.grace_minutes}} if args.keeper_while
                  else {"hold": {"until": time.time() + args.keeper_hours * 3600}} if args.keeper_hours
                  else {"hold": {"until": 0}})
        print(install_keeper(args.session, policy))
    elif args.stop:
        print("stopped" if stop_daemon(args.session) else "no background heartbeat running")
    elif args.status:
        print(describe(args.session))
    elif args.detach:
        log = detach(args.session, [a for a in sys.argv if a != "--detach"])
        print(f"heartbeat running in the background; log: {log}")
    else:
        keep_alive(args.session, interval_s=args.interval, stay_awake=not args.allow_sleep)


if __name__ == "__main__":
    main()
