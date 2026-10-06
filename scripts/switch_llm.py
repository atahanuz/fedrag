"""Switch the LLM that vLLM serves on the running Colab VM (embedder, reranker, gateway and tunnel stay up).

    python scripts/switch_llm.py gemma    # google/gemma-4-31B-it, FP8 on load
    python scripts/switch_llm.py qwen     # back to Qwen/Qwen3.8-27B-FP8 (the default)

Both presets get launch.py's default structured-output setting (no free whitespace in JSON). The choice is
written to /content/fedrag_llm.json on the VM, so the heartbeat's restarts serve the same
model, and FEDRAG_LLM_MODEL (the served name the clients ask for) is written to the local .env.
"""

from __future__ import annotations

import argparse
import re
import time
from pathlib import Path

from colab_keepalive import vm_exec
from colab_up import upload_code

ROOT = Path(__file__).resolve().parent.parent
PRESETS = {
    "qwen": {"model": "Qwen/Qwen3.8-27B-FP8", "name": "qwen3.8-27b", "tool_parser": "qwen3_xml",
             "reasoning_parser": "qwen3", "mtp": "1", "quant": ""},
    "gemma": {"model": "google/gemma-4-31B-it", "name": "gemma-4-31b", "tool_parser": "gemma4",
              "reasoning_parser": "gemma4", "mtp": "0", "quant": "fp8"},
}


def set_env(key: str, value: str) -> None:
    path = ROOT / ".env"
    text = path.read_text() if path.exists() else ""
    line = f"{key}={value}"
    text = re.sub(rf"^{key}=.*$", line, text, flags=re.M) if re.search(rf"^{key}=", text, re.M) else \
        text.rstrip("\n") + "\n" + line + "\n"
    path.write_text(text)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("preset", choices=sorted(PRESETS))
    ap.add_argument("-s", "--session", default="fedrag")
    ap.add_argument("--timeout", type=int, default=1800, help="seconds to wait for the model to load")
    args = ap.parse_args()
    choice = PRESETS[args.preset]
    upload_code(args.session)
    print(vm_exec(args.session, f"""
import json, subprocess
open('/content/fedrag_llm.json', 'w').write(json.dumps({choice!r}))
r = subprocess.run(['python', '/content/gpu_server/launch.py', '--restart-llm'], capture_output=True, text=True,
                   cwd='/content/gpu_server')
print(r.stdout[-800:], r.stderr[-800:])
""", timeout=600))
    t0 = time.time()
    while time.time() - t0 < args.timeout:
        out = vm_exec(args.session, """
import urllib.request, json, subprocess
try:
    print('MODELS', urllib.request.urlopen('http://127.0.0.1:8001/v1/models', timeout=5).read().decode())
except Exception as e:
    print('WAIT', type(e).__name__)
print(subprocess.run('tail -c 600 /content/logs/vllm.log', shell=True, capture_output=True, text=True).stdout)
""")
        if "MODELS" in out and choice["name"] in out:
            set_env("FEDRAG_LLM_MODEL", choice["name"])
            print(f"{choice['model']} is serving as {choice['name']} after {time.time() - t0:.0f}s; "
                  f".env updated (restart running clients such as `fedrag ui`)")
            return
        if ("Traceback" in out and "Error" in out) or "vllm serve: error" in out:
            raise SystemExit(f"vLLM failed to start:\n{out[-1500:]}")
        print(f"  loading ({time.time() - t0:.0f}s) {out.strip().splitlines()[-1][-160:] if out.strip() else ''}",
              flush=True)
        time.sleep(30)
    raise SystemExit("model did not come up in time; see /content/logs/vllm.log on the VM")


if __name__ == "__main__":
    main()
