"""Bring up (or reconnect to) the GPU server on Google Colab with one command.

    python scripts/colab_up.py                      # create/reuse session, set up, launch, write .env, keep alive
    python scripts/colab_up.py --status
    python scripts/colab_up.py --stop               # stop the heartbeat and release the VM (stops billing)
    python scripts/colab_up.py --keepalive-only     # (re)start just the heartbeat; add --foreground to watch it

Steps: create a high-RAM A100 session (``COLAB_HIGH_MEM=1 colab new --gpu A100``, see COLAB_GUIDE.md),
upload ``gpu_server/``, install vLLM and download the models, embed the corpus if the local index is
missing, start gateway + vLLM + Cloudflare tunnel, and write FEDRAG_GPU_URL / FEDRAG_API_KEY to ``.env``.

Colab releases a GPU VM about 20 minutes after the last kernel-websocket traffic through its proxy, no
matter how busy the VM itself is, and colab-cli 0.6.0 loses the session when its proxy token expires
after an hour (details in colab_keepalive.py and COLAB_GUIDE.md). So launch.py also starts
gpu_server/keeper.py, which holds a websocket from the VM to itself through the proxy while the gateway
is in use, and this script uploads the proxy URL and a token for it. After bring-up, a background
heartbeat also runs the status cell every 4 minutes: it restarts crashed components, keeps both tokens
fresh, and rewrites .env if the tunnel URL changes. After ``--idle-minutes`` without gateway requests both
let go, and Colab releases the VM about 20 minutes later.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

from colab_keepalive import (colab, describe, detach, ensure_token, keep_alive, push_keeper_config,
                             refresh_session, stop_daemon, vm_exec)

ROOT = Path(__file__).resolve().parent.parent


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


def colab_s(session: str, command: str, *args: str) -> str:
    """A `colab` command against ``session``, with a proxy token that is still valid."""
    ensure_token(session)
    return colab(command, "-s", session, *args)


def push_proxy(session: str, idle_minutes: int) -> None:
    """Hand keeper.py on the VM the proxy URL, a fresh token and the idle cutoff (when they changed)."""
    push_keeper_config(session, {"idle_minutes": idle_minutes})


def upload_code(session: str) -> None:
    with tempfile.TemporaryDirectory() as td:
        tgz = Path(td) / "gpu_server.tgz"
        with tarfile.open(tgz, "w:gz") as tar:
            tar.add(ROOT / "gpu_server", arcname="gpu_server",
                    filter=lambda ti: None if "__pycache__" in ti.name else ti)
        colab_s(session, "upload", str(tgz), "/content/gpu_server.tgz")
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
    """Embed the chunks that have no vector yet; vectors of unchanged chunks are reused (matched by text hash)."""
    sys.path.insert(0, str(ROOT))
    from fedrag import config

    ids_path, emb_path = config.INDEX_DIR / "ids.json", config.INDEX_DIR / "embeddings.npy"
    chunk_ids = [json.loads(line)["chunk_id"] for line in open(config.CORPUS_DIR / "chunks.jsonl")]
    if emb_path.exists() and ids_path.exists() and json.load(open(ids_path))["ids"] == chunk_ids:
        print("local embeddings match the corpus; skipping embedding job")
        return
    py = [sys.executable, "-m", "fedrag.retrieval.index"]
    subprocess.run(py + ["prepare"], cwd=ROOT, check=True)
    todo = config.INDEX_DIR / "embed_input.jsonl"
    new_dir = config.INDEX_DIR / "new"
    if sum(1 for _ in open(todo)):
        print("embedding the new chunks on the GPU ...")
        colab_s(session, "upload", str(todo), "/content/embed_input.jsonl")
        vm_exec(session, "import os, shutil; shutil.rmtree('/content/index_out', ignore_errors=True); "
                         "os.path.exists('/content/logs/embed.log') and os.remove('/content/logs/embed.log')")
        run_detached(session, "embed_corpus.py", ["/content/embed_input.jsonl", "/content/index_out"], "embed.log")
        wait_for(session, "embed.log", "EMBED_DONE", 1800, "corpus embedding")
        new_dir.mkdir(parents=True, exist_ok=True)
        colab_s(session, "download", "/content/index_out/embeddings.npy", str(new_dir / "embeddings.npy"))
        colab_s(session, "download", "/content/index_out/ids.json", str(new_dir / "ids.json"))
    subprocess.run(py + ["merge", "--new", str(new_dir)], cwd=ROOT, check=True)


def write_env(session: str) -> dict:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "endpoint.json"
        colab_s(session, "download", "/content/fedrag_endpoint.json", str(p))
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
    out = vm_exec(session, STATUS_CELL, timeout=600)  # healing can restart the gateway (minutes)
    m = re.search(r"\{.*\}", out, re.S)
    return json.loads(m.group(0)) if m else {"raw": out}


def env_url() -> str | None:
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("FEDRAG_GPU_URL="):
                return line.split("=", 1)[1].strip()
    return None


def heartbeat(idle_minutes: int):
    """The status cell doubles as the heartbeat: it is kernel traffic through the Colab proxy, which is
    what Colab's idle timer counts, and it restarts crashed components. Each beat also hands the VM's
    keeper a fresh token."""

    def beat(session: str) -> bool:
        try:
            push_proxy(session, idle_minutes)
        except Exception as e:  # the keeper still has its last token; the status cell matters more
            print(f"  could not update the keeper's token: {str(e)[-200:]}", flush=True)
        st = status(session)
        if st.get("url") and st["url"] != env_url():
            print(f"  tunnel URL is now {st['url']}; updating .env", flush=True)
            write_env(session)
        idle = st.get("idle_seconds")
        keeper = st.get("keeper") or {}
        print(f"{time.strftime('%H:%M:%S')} gateway={st.get('gateway')} llm={st.get('vllm')} "
              f"tunnel={st.get('tunnel_ok', st.get('tunnel_process'))} keeper={keeper.get('connected')} "
              f"idle={idle}s gpu={st.get('gpu')}", flush=True)
        if idle_minutes and idle is not None and idle > idle_minutes * 60:
            print(f"gateway idle for more than {idle_minutes} minutes: the heartbeat stops, and Colab releases "
                  f"the VM in about 20 minutes (or run --stop now)", flush=True)
            return False
        return True

    return beat


def start_keepalive(args: argparse.Namespace) -> None:
    if args.foreground:
        keep_alive(args.session, heartbeat(args.idle_minutes), args.interval)
        return
    log = detach(args.session, [str(Path(__file__).resolve()), "--keepalive-only", "--foreground",
                                "-s", args.session, "--interval", str(args.interval),
                                "--idle-minutes", str(args.idle_minutes)])
    print(f"heartbeat running in the background (log: {log}); `--stop` ends it and releases the VM")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-s", "--session", default="fedrag")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--stop", action="store_true", help="stop the heartbeat and release the VM")
    ap.add_argument("--keepalive-only", action="store_true", help="(re)start just the heartbeat")
    ap.add_argument("--foreground", action="store_true", help="run the heartbeat in this terminal")
    ap.add_argument("--no-keepalive", action="store_true")
    ap.add_argument("--interval", type=int, default=240, help="heartbeat seconds (Colab idles out at ~20 min)")
    ap.add_argument("--idle-minutes", type=int, default=60,
                    help="stop the heartbeat after this long without gateway requests (0: never)")
    args = ap.parse_args()

    if args.stop:
        stop_daemon(args.session)
        if ensure_token(args.session):  # re-adopts the VM if the CLI dropped its record
            print(colab("stop", "-s", args.session, check=False))
        else:
            print(f"session {args.session} is not running")
        return
    if args.status:
        if not ensure_token(args.session):
            print(f"session {args.session} is not running")
            return
        print(json.dumps(status(args.session), indent=2))
        print(describe(args.session))
        return
    if args.keepalive_only:
        start_keepalive(args)
        return

    t0 = time.time()
    ensure_session(args.session)
    upload_code(args.session)
    push_proxy(args.session, args.idle_minutes)
    run_detached(args.session, "setup_vm.py", [], "setup.log")
    wait_for(args.session, "setup.log", "SETUP_DONE", 1800, "VM setup (vLLM install + model download)")
    ensure_embeddings(args.session)
    run_detached(args.session, "launch.py", ["--wait-llm"], "launch.log")
    wait_for(args.session, "launch.log", "LLM ready", 1500, "serving stack (gateway + vLLM + tunnel)")
    ep = write_env(args.session)
    print(f"\nGPU server ready in {time.time() - t0:.0f}s at {ep['url']} (credentials written to .env)")
    print("Try:  .venv/bin/python -m fedrag status")
    if not args.no_keepalive:
        start_keepalive(args)


if __name__ == "__main__":
    main()
