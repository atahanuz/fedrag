"""Start the GPU serving stack on the Colab VM (idempotent).

1. gateway (embedder + reranker + auth + LLM proxy) on :8000
2. vLLM OpenAI server for the LLM on 127.0.0.1:8001 (only reachable via the gateway)
3. Cloudflare quick tunnel -> public https URL for the gateway

Writes ``/content/fedrag_endpoint.json`` = {"url": ..., "api_key": ...}.
Every process is detached (``start_new_session``) and logs to /content/logs/.

    python launch.py            # start whatever is not running yet
    python launch.py --status   # print component status
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOGS = Path("/content/logs")
ENDPOINT_FILE = Path("/content/fedrag_endpoint.json")
KEY_FILE = Path("/content/fedrag_api_key")

LLM_MODEL = os.environ.get("FEDRAG_LLM_MODEL", "Qwen/Qwen3.8-27B-FP8")
SERVED_NAME = os.environ.get("FEDRAG_LLM_NAME", "qwen3.8-27b")
LLM_GPU_UTIL = os.environ.get("FEDRAG_LLM_GPU_UTIL", "0.62")
LLM_MAX_LEN = os.environ.get("FEDRAG_LLM_MAX_LEN", "65536")
USE_MTP = os.environ.get("FEDRAG_LLM_MTP", "1") == "1"
CLOUDFLARED = "/usr/local/bin/cloudflared"
GATEWAY_LOCAL = "http://127.0.0.1:8000"
TUNNEL_PATTERN = f"cloudflared tunnel --no-autoupdate --url {GATEWAY_LOCAL}"  # only our own tunnel


def _get(url: str, timeout: float = 3.0) -> int | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status
    except Exception:
        return None


def _running(pattern: str) -> bool:
    return subprocess.run(["pgrep", "-f", pattern], capture_output=True).returncode == 0


def _spawn(name: str, cmd: list[str], env: dict | None = None) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    log = open(LOGS / f"{name}.log", "a")
    log.write(f"\n===== {time.ctime()} : {' '.join(cmd)}\n")
    log.flush()
    subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True, cwd=str(HERE),
                     env={**os.environ, **(env or {})})
    print(f"started {name}", flush=True)


def api_key() -> str:
    if KEY_FILE.exists():
        return KEY_FILE.read_text().strip()
    key = "fedrag-" + secrets.token_urlsafe(24)
    KEY_FILE.write_text(key)
    os.chmod(KEY_FILE, 0o600)
    return key


def start_gateway(key: str) -> None:
    if _get("http://127.0.0.1:8000/health") == 200:
        print("gateway already up")
        return
    if not _running("uvicorn gateway:app"):
        _spawn("gateway", [sys.executable, "-m", "uvicorn", "gateway:app", "--host", "0.0.0.0", "--port", "8000",
                           "--log-level", "warning"], env={"FEDRAG_API_KEY": key})
    # embedder + reranker must be resident before vLLM sizes its KV cache
    for _ in range(180):
        if _get("http://127.0.0.1:8000/health") == 200:
            print("gateway ready")
            return
        time.sleep(2)
    raise SystemExit("gateway did not come up; see /content/logs/gateway.log")


def start_vllm() -> None:
    if _get("http://127.0.0.1:8001/health") == 200 or _running(f"vllm serve {LLM_MODEL}"):
        print("vLLM already running/starting")
        return
    cmd = [
        "vllm", "serve", LLM_MODEL,
        "--host", "127.0.0.1", "--port", "8001",
        "--served-model-name", SERVED_NAME,
        "--max-model-len", LLM_MAX_LEN,
        "--gpu-memory-utilization", LLM_GPU_UTIL,
        "--reasoning-parser", "qwen3",
        "--enable-auto-tool-choice", "--tool-call-parser", "qwen3_xml",
        "--language-model-only",
        "--max-num-seqs", "32",
    ]
    if USE_MTP:
        cmd += ["--speculative-config", json.dumps({"method": "mtp", "num_speculative_tokens": 3})]
    _spawn("vllm", cmd)


def start_tunnel(key: str) -> str:
    if not os.path.exists(CLOUDFLARED):
        subprocess.run(
            f"curl -sL -o {CLOUDFLARED} https://github.com/cloudflare/cloudflared/releases/latest/download/"
            f"cloudflared-linux-amd64 && chmod +x {CLOUDFLARED}", shell=True, check=True)
    log_path = LOGS / "tunnel.log"
    if not _running(TUNNEL_PATTERN):
        if log_path.exists():
            log_path.unlink()
        _spawn("tunnel", [CLOUDFLARED, "tunnel", "--no-autoupdate", "--url", GATEWAY_LOCAL])
    for _ in range(60):
        if log_path.exists():
            m = re.findall(r"https://[a-z0-9-]+\.trycloudflare\.com", log_path.read_text())
            if m:
                url = m[-1]
                ENDPOINT_FILE.write_text(json.dumps({"url": url, "api_key": key}))
                os.chmod(ENDPOINT_FILE, 0o600)
                return url
        time.sleep(1)
    raise SystemExit("tunnel URL not found; see /content/logs/tunnel.log")


def status() -> dict:
    out = {
        "gateway": _get("http://127.0.0.1:8000/health") == 200,
        "vllm": _get("http://127.0.0.1:8001/health") == 200,
        "vllm_process": _running(f"vllm serve {LLM_MODEL}"),
        "tunnel_process": _running(TUNNEL_PATTERN),
    }
    if ENDPOINT_FILE.exists():
        out["url"] = json.loads(ENDPOINT_FILE.read_text())["url"]
    try:
        smi = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu",
                              "--format=csv,noheader"], capture_output=True, text=True).stdout.strip()
        out["gpu"] = smi
    except Exception:
        pass
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--wait-llm", action="store_true", help="block until vLLM answers /health")
    args = ap.parse_args()
    if args.status:
        print(json.dumps(status()))
        return
    key = api_key()
    start_gateway(key)
    start_vllm()
    url = start_tunnel(key)
    print(f"ENDPOINT {url}")
    if args.wait_llm:
        for _ in range(600):
            if _get("http://127.0.0.1:8001/health") == 200:
                print("LLM ready")
                break
            time.sleep(2)
    print(json.dumps(status()))


if __name__ == "__main__":
    main()
