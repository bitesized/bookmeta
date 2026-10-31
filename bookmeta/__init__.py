"""Fetch, verify and write high-quality metadata for a KOReader EPUB library."""

import os
from pathlib import Path


def _load_env() -> None:
    """Minimal .env support (cwd, then the project dir, then ~/.config/bookmeta). Real env vars win."""
    for p in (Path.cwd() / ".env", Path(__file__).resolve().parent.parent / ".env", Path.home() / ".config" / "bookmeta" / ".env"):
        try:
            lines = p.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.removeprefix("export ").split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))


_load_env()
