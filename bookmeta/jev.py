"""Minimal async client for Jev (TypeSafe System One): choice / noul / score in one batched call."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx

BASE_URL = os.environ.get("JEVMCP_BASE_URL", "https://api.typesafe.ai/v1/systemone").rstrip("/")
MODEL = os.environ.get("JEVMCP_MODEL", "jev-latest")


def resolve_key() -> str | None:
    """Same precedence as jev-mcp-server: env var, then its stored key file, then Claude's MCP config."""
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if key:
        return key
    try:
        key = (Path.home() / ".config" / "jev-mcp" / "key").read_text().strip()
        if key:
            return key
    except OSError:
        pass
    try:
        cfg = json.loads((Path.home() / ".claude.json").read_text())
    except (OSError, ValueError):
        return None

    def walk(o):
        if isinstance(o, dict):
            if o.get("command") == "uvx" and "jev-mcp-server" in o.get("args", []):
                return (o.get("env") or {}).get("TYPESAFE_API_KEY")
            for v in o.values():
                if found := walk(v):
                    return found
        return None

    return walk(cfg)


class JevError(RuntimeError):
    pass


class Jev:
    def __init__(self, client: httpx.AsyncClient, max_concurrent: int = 4):
        self.client = client
        self.key = resolve_key()
        self.sem = asyncio.Semaphore(max_concurrent)
        self.calls = 0

    @property
    def available(self) -> bool:
        return bool(self.key)

    async def ask(self, questions: dict) -> dict:
        """Send several typed questions at once; returns {name: answer}."""
        if not self.key:
            raise JevError("No TypeSafe API key (set TYPESAFE_API_KEY)")
        body = {"model": MODEL, "state": {}, "questions": questions}
        async with self.sem:
            for attempt in range(3):
                try:
                    r = await self.client.post(BASE_URL, json=body, headers={"Authorization": f"Bearer {self.key}"}, timeout=30)
                except httpx.HTTPError as e:
                    if attempt == 2:
                        raise JevError(f"connection failed: {type(e).__name__}") from None
                    continue
                if r.status_code in (429, 503, 529) and attempt < 2:
                    await asyncio.sleep(0.5 * 2**attempt)
                    continue
                if r.is_error:
                    raise JevError(f"HTTP {r.status_code}: {r.text[:200]}")
                self.calls += 1
                return r.json().get("answers", {})
        raise JevError("unavailable after retries")

    @staticmethod
    def choice_q(question: str, options: dict[str, str], context: str = "") -> dict:
        return {"type": "choice", "criteria": options, "instructions": {"question": question, "context": context}}

    @staticmethod
    def noul_q(question: str, context: str = "") -> dict:
        return {"type": "noul", "instructions": {"question": question, "context": context}}

    @staticmethod
    def probs(answer: dict, options) -> dict[str, float]:
        p = answer.get("probabilities") or {}
        if set(p) != set(options):
            raise JevError("invalid choice response")
        return p

    @staticmethod
    def degree(answer: dict) -> float:
        v = answer.get("noul")
        if not isinstance(v, (int, float)) or not 0 <= v <= 1:
            raise JevError("invalid noul response")
        return float(v)
