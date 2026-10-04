"""Central configuration. Values come from environment variables (or a ``.env`` file)."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(ROOT / ".env")

DOCS_DIR = Path(os.environ.get("FEDRAG_DOCS_DIR", ROOT / "federal_reserve"))
DATA_DIR = Path(os.environ.get("FEDRAG_DATA_DIR", ROOT / "data"))
CORPUS_DIR = DATA_DIR / "corpus"
INDEX_DIR = DATA_DIR / "index"

# GPU gateway (vLLM LLM + embedder + reranker), see gpu_server/ and scripts/colab_up.py
GPU_URL = os.environ.get("FEDRAG_GPU_URL", "").rstrip("/")
API_KEY = os.environ.get("FEDRAG_API_KEY", "")

# Any OpenAI-compatible endpoint can serve the agents; defaults to the gateway.
LLM_BASE_URL = os.environ.get("FEDRAG_LLM_BASE_URL", f"{GPU_URL}/v1" if GPU_URL else "")
LLM_API_KEY = os.environ.get("FEDRAG_LLM_API_KEY", API_KEY)
LLM_MODEL = os.environ.get("FEDRAG_LLM_MODEL", "qwen3.8-27b")
# The model's chat template accepts enable_thinking; set to 0 for non-Qwen backends.
LLM_SUPPORTS_THINKING_FLAG = os.environ.get("FEDRAG_LLM_THINKING_FLAG", "1") == "1"
LLM_TIMEOUT = float(os.environ.get("FEDRAG_LLM_TIMEOUT", "300"))
