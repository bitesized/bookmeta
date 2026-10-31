"""User settings kept between runs (theme, colour palette, last library) in ~/.config/bookmeta/settings.json."""

from __future__ import annotations

import json
import os
from pathlib import Path

FILE = Path(os.environ.get("BOOKMETA_CONFIG", Path.home() / ".config" / "bookmeta" / "settings.json"))


def load() -> dict:
    try:
        d = json.loads(FILE.read_text())
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def save(**values) -> None:
    d = load()
    if all(d.get(k) == v for k, v in values.items()):
        return
    d.update(values)
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, FILE)
