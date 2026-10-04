"""Bring up (or reconnect to) the GPU server on Google Colab with one command.

    python scripts/colab_up.py              # create/reuse session, set up, launch, write .env, keep alive
    python scripts/colab_up.py --no-keepalive
    python scripts/colab_up.py --status
    python scripts/colab_up.py --stop       # release the VM (stops billing)

Steps: create a high-RAM A100 session (``COLAB_HIGH_MEM=1 colab new --gpu A100``, see COLAB_GUIDE.md),
upload ``gpu_server/``, install vLLM and download the models, embed the corpus if the local index is
missing, start gateway + vLLM + Cloudflare tunnel, and write FEDRAG_GPU_URL / FEDRAG_API_KEY to ``.env``.

Colab reclaims a VM roughly 20 minutes after the last kernel execution, even while background servers
are busy. The keep-alive loop therefore runs a tiny status cell every few minutes, but only while the
gateway is actually being used: after ``--idle-minutes`` without requests it stops, and Colab releases
the VM.

The CLI (0.6.0) also never refreshes its runtime-proxy token, which expires after about an hour; the next
``colab exec`` then gets a 401, and the CLI deletes the session record and kills its keep-alive daemon
while the VM keeps running (an orphan, shown as ``[?]`` by ``colab sessions``). ``refresh_session``
re-adopts the assignment with a fresh token from the assignment listing; the keep-alive loop calls it
on every heartbeat so the token never expires.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NOISE = re.compile(r"new version of Colab CLI|colab update|pip install --upgrade|enable_update_check|^\s*$")


def colab(*args: str, env: dict | None = None, timeout: int = 900, check: bool = True) -> str:
    p = subprocess.run(["colab", *args], capture_output=True, text=True, timeout=timeout,
                       env={**os.environ, **(env or {})})
    out = "\n".join(line for line in (p.stdout + p.stderr).splitlines() if not NOISE.search(line))
    if check and p.returncode != 0:
        raise RuntimeError(f"colab {' '.join(args)} failed:\n{out}")
    return out


_REFRESH = r"""
import json, os, sys
from colab_cli.commands.session import spawn_keep_alive
from colab_cli.common import State
from colab_cli.state import SessionState

session = sys.argv[1]
st = State()
local = st.store.list()
assignments = st.client.list_assignments()
existing = st.store.get(session)
if existing is not None:
    target = next((a for a in assignments if a.endpoint == existing.endpoint), None)
else:  # adopt the single orphan (an assignment no local record points to)
    known = {s.endpoint for s in local.values()}
    orphans = [a for a in assignments if a.endpoint not in known]
    target = orphans[0] if len(orphans) == 1 else None
if target is None:
    print(json.dumps({"ok": False}))
    sys.exit(0)
rec = SessionState(
    name=session, token=target.runtime_proxy_info.token, url=target.runtime_proxy_info.url,
    endpoint=target.endpoint, variant=target.variant.name, accelerator=target.accelerator.value,
    kernel_id=existing.kernel_id if existing else None, session_id=existing.session_id if existing else None,
    keep_alive_pid=existing.keep_alive_pid if existing else None,
)
alive = False
if rec.keep_alive_pid:
    try:
        os.kill(rec.keep_alive_pid, 0)
        alive = True
    except OSError:
        pass
st.store.add(rec)
if not alive:
    rec.keep_alive_pid = spawn_keep_alive(target.endpoint, session, auth_provider=st.auth_provider,
                                          config_path=st.config_path)
    st.store.add(rec)
print(json.dumps({"ok": True, "adopted": existing is None, "endpoint": target.endpoint}))
"""


def _colab_python() -> str:
    """The interpreter that runs the `colab` executable (where the patched colab_cli is installed)."""
    exe = shutil.which("colab")
    if exe:
        first = open(exe, "rb").readline().decode(errors="replace").strip()
        if first.startswith("#!") and "python" in first:
            return first[2:].strip().split()[0]
    return sys.executable


def refresh_session(session: str) -> bool:
    """Give the CLI's record for ``session`` a fresh runtime-proxy token, re-adopting the assignment if
    the CLI dropped it, and make sure its keep-alive daemon runs. Uses the CLI's own state API."""
    p = subprocess.run([_colab_python(), "-c", _REFRESH, session], capture_output=True, text=True, timeout=120)
    m = re.search(r"\{.*\}", p.stdout)
    if not m:
        print(f"  session refresh failed: {(p.stderr or p.stdout)[-300:]}")
        return False
    res = json.loads(m.group(0))
    if res.get("adopted"):
        print(f"re-adopted orphaned assignment {res['endpoint']} as session {session}")
    return bool(res.get("ok"))


def vm_exec(session: str, code: str, timeout: int = 600, retries: int = 3) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(code)
        path = f.name
    try:
        for attempt in range(retries):
            try:
                return colab("exec", "-s", session, "-f", path, timeout=timeout)
            except (RuntimeError, subprocess.TimeoutExpired) as e:
                if attempt == retries - 1:
                    raise
                print(f"  exec retry ({str(e)[:120]})")
                if "appears to be lost" in str(e):
                    refresh_session(session)  # expired proxy token, not a dead VM
                time.sleep(10)  # first exec after READY may time out while the kernel boots
    finally:
        os.unlink(path)
    return ""


def session_exists(session: str) -> bool:
    return f"[{session}]" in colab("sessions", check=False)


def ensure_session(session: str) -> None:
    if session_exists(session) or refresh_session(session):
        print(f"reusing session {session}")
        return
    print(f"creating high-RAM A100 session {session} ...")
    print(colab("new", "--gpu", "A100", "-s", session, env={"COLAB_HIGH_MEM": "1"}, timeout=900))
    listing = colab("sessions", check=False)
    if "-hm-" not in listing:
        print("WARNING: session is not a high-RAM (-hm) VM: 40GB A100 instead of 80GB. "
              "Re-apply the COLAB_GUIDE.md patch or lower FEDRAG_LLM_GPU_UTIL.")


def upload_code(session: str) -> None:
    with tempfile.TemporaryDirectory() as td:
        tgz = Path(td) / "gpu_server.tgz"
        with tarfile.open(tgz, "w:gz") as tar:
            tar.add(ROOT / "gpu_server", arcname="gpu_server",
                    filter=lambda ti: None if "__pycache__" in ti.name else ti)
        colab("upload", "-s", session, str(tgz), "/content/gpu_server.tgz")
    vm_exec(session, "import subprocess; subprocess.run('cd /content && tar xzf gpu_server.tgz', shell=True)")


def run_detached(session: str, script: str, args: list[str], log: str) -> None:
    vm_exec(session, f"""
import subprocess, sys, os
os.makedirs('/content/logs', exist_ok=True)
p = subprocess.Popen([sys.executable, '-u', '/content/gpu_server/{script}', *{args!r}],
                     stdout=open('/content/logs/{log}', 'a'), stderr=subprocess.STDOUT,
                     start_new_session=True, cwd='/content/gpu_server')
print('pid', p.pid)
""")


def wait_for(session: str, log: str, marker: str, timeout_s: int, label: str) -> str:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        out = vm_exec(session, f"""
import subprocess
r = subprocess.run("tail -c 4000 /content/logs/{log} 2>/dev/null", shell=True, capture_output=True)
print(r.stdout.decode('utf-8', 'replace'))
""")
        if marker in out:
            return out
        if "Traceback" in out and "Error" in out.splitlines()[-1]:
            raise RuntimeError(f"{label} failed:\n{out[-2000:]}")
        print(f"  waiting for {label} ({time.time() - t0:.0f}s) ...")
        time.sleep(20)
    raise TimeoutError(f"{label} did not finish within {timeout_s}s")


def ensure_embeddings(session: str) -> None:
    sys.path.insert(0, str(ROOT))
    from fedrag import config

    ids_path, emb_path = config.INDEX_DIR / "ids.json", config.INDEX_DIR / "embeddings.npy"
    chunk_ids = [json.loads(line)["chunk_id"] for line in open(config.CORPUS_DIR / "chunks.jsonl")]
    if emb_path.exists() and ids_path.exists() and json.load(open(ids_path))["ids"] == chunk_ids:
        print("local embeddings match the corpus; skipping embedding job")
        return
    print("embedding the corpus on the GPU ...")
    subprocess.run([sys.executable, "-m", "fedrag.retrieval.index", "prepare"], cwd=ROOT, check=True)
    colab("upload", "-s", session, str(config.INDEX_DIR / "embed_input.jsonl"), "/content/embed_input.jsonl")
    run_detached(session, "embed_corpus.py", ["/content/embed_input.jsonl", "/content/index_out"], "embed.log")
    wait_for(session, "embed.log", "EMBED_DONE", 1800, "corpus embedding")
    colab("download", "-s", session, "/content/index_out/embeddings.npy", str(emb_path))
    colab("download", "-s", session, "/content/index_out/ids.json", str(ids_path))


def write_env(session: str) -> dict:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "endpoint.json"
        colab("download", "-s", session, "/content/fedrag_endpoint.json", str(p))
        ep = json.loads(p.read_text())
    env_path = ROOT / ".env"
    keep = []
    if env_path.exists():
        keep = [line for line in env_path.read_text().splitlines()
                if line.strip() and not line.startswith(("FEDRAG_GPU_URL=", "FEDRAG_API_KEY=", "# Written by"))]
    env_path.write_text("\n".join(["# Written by scripts/colab_up.py - GPU gateway on Colab (do not commit)",
                                   f"FEDRAG_GPU_URL={ep['url']}", f"FEDRAG_API_KEY={ep['api_key']}", *keep]) + "\n")
    os.chmod(env_path, 0o600)
    return ep


STATUS_CELL = """
import json, subprocess
print(subprocess.run(['python', '/content/gpu_server/launch.py', '--status', '--heal'], capture_output=True, text=True).stdout)
"""


def status(session: str) -> dict:
    out = vm_exec(session, STATUS_CELL)
    m = re.search(r"\{.*\}", out, re.S)
    return json.loads(m.group(0)) if m else {"raw": out}


def keepalive(session: str, interval_s: int, idle_minutes: int) -> None:
    """Heartbeat while the gateway is in use; re-sync .env if the tunnel URL changes."""
    print(f"keep-alive: status cell every {interval_s}s; stops after {idle_minutes} idle minutes (Ctrl-C to stop)")
    url = None
    while True:
        try:
            if not refresh_session(session):  # keeps the proxy token fresh (it expires after ~1 hour)
                print("session is gone; exiting keep-alive (run colab_up.py again)")
                return
            st = status(session)
        except Exception as e:
            if not session_exists(session) and not refresh_session(session):
                print("session is gone; exiting keep-alive (run colab_up.py again)")
                return
            print(f"  status failed: {e}")
            time.sleep(interval_s)
            continue
        if st.get("url") and st["url"] != url:
            if url is not None:
                print(f"  tunnel URL changed -> {st['url']}; updating .env")
                write_env(session)
            url = st["url"]
        idle = st.get("idle_seconds")
        print(f"  {time.strftime('%H:%M:%S')} gateway={st.get('gateway')} llm={st.get('vllm')} "
              f"tunnel={st.get('tunnel_ok', st.get('tunnel_process'))} idle={idle}s gpu={st.get('gpu')}")
        if idle is not None and idle > idle_minutes * 60:
            print(f"gateway idle for more than {idle_minutes} minutes: stopping keep-alive; Colab will "
                  f"release the VM in ~20 minutes (or run --stop now)")
            return
        time.sleep(interval_s)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-s", "--session", default="fedrag")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--stop", action="store_true")
    ap.add_argument("--keepalive-only", action="store_true", help="only run the heartbeat loop")
    ap.add_argument("--no-keepalive", action="store_true")
    ap.add_argument("--interval", type=int, default=240, help="heartbeat seconds (Colab idles out at ~20 min)")
    ap.add_argument("--idle-minutes", type=int, default=45)
    args = ap.parse_args()

    if args.stop:
        print(colab("stop", "-s", args.session, check=False))
        return
    if args.status:
        refresh_session(args.session)
        print(json.dumps(status(args.session), indent=2))
        return
    if args.keepalive_only:
        keepalive(args.session, args.interval, args.idle_minutes)
        return

    t0 = time.time()
    ensure_session(args.session)
    upload_code(args.session)
    run_detached(args.session, "setup_vm.py", [], "setup.log")
    wait_for(args.session, "setup.log", "SETUP_DONE", 1800, "VM setup (vLLM install + model download)")
    ensure_embeddings(args.session)
    run_detached(args.session, "launch.py", ["--wait-llm"], "launch.log")
    wait_for(args.session, "launch.log", "LLM ready", 1500, "serving stack (gateway + vLLM + tunnel)")
    ep = write_env(args.session)
    print(f"\nGPU server ready in {time.time() - t0:.0f}s at {ep['url']} (credentials written to .env)")
    print("Try:  .venv/bin/python -m fedrag status")
    if not args.no_keepalive:
        keepalive(args.session, args.interval, args.idle_minutes)


if __name__ == "__main__":
    main()
