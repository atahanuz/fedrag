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
