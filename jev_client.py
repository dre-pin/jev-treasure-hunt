"""Jev client setup: .env loading and a TypeSafe client for the Jev model."""

import os
from pathlib import Path

from typesafe_sdk import TypeSafeClient

MODEL = "jev-latest"
ENV_FILE = Path(__file__).with_name(".env")


def load_dotenv(path: Path = ENV_FILE) -> None:
    """Populate os.environ from .env next to this file without overriding existing vars."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def make_client() -> TypeSafeClient:
    """Load .env (for TYPESAFE_API_KEY) and return a client pointed at the Jev model."""
    load_dotenv()
    return TypeSafeClient(model=MODEL)
