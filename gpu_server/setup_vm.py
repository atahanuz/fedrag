"""One-time setup of a fresh Colab GPU VM (run detached; logs to /content/logs/setup.log).

* installs vLLM and removes the mismatched preinstalled torchaudio (see COLAB_GUIDE.md)
* downloads the LLM, embedding and reranking weights
Prints SETUP_DONE when finished.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

MODELS = [
    os.environ.get("FEDRAG_LLM_MODEL", "Qwen/Qwen3.8-27B-FP8"),
    os.environ.get("FEDRAG_EMBED_MODEL", "Qwen/Qwen3-Embedding-8B"),
    os.environ.get("FEDRAG_RERANK_MODEL", "Qwen/Qwen3-Reranker-4B"),
]


def sh(cmd: str) -> int:
    print(f"$ {cmd}", flush=True)
    return subprocess.run(cmd, shell=True).returncode


def main() -> None:
    t0 = time.time()
    have_vllm = subprocess.run([sys.executable, "-c", "import vllm"], capture_output=True).returncode == 0
    if not have_vllm:
        sh(f"{sys.executable} -m pip install -q -U vllm")
    # vLLM pulls a newer torch; the preinstalled torchaudio then breaks `import transformers`.
    sh(f"{sys.executable} -m pip uninstall -y -q torchaudio")
    sh(f"{sys.executable} -m pip install -q fastapi uvicorn httpx sentence-transformers")

    from huggingface_hub import snapshot_download

    for repo in MODELS:
        t = time.time()
        snapshot_download(repo, allow_patterns=["*.json", "*.safetensors", "*.txt", "*.model", "*.jinja", "*.py",
                                                "tokenizer*", "*.tiktoken", "1_Pooling/*"])
        print(f"downloaded {repo} in {time.time() - t:.0f}s", flush=True)
    print(f"SETUP_DONE in {time.time() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
