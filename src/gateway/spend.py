"""Hard spend cap shared across portfolio projects.

Append-only JSONL ledger (one line per paid call) guarded by a cross-process file lock. Before each paid
call we estimate the worst case (prompt tokens + max_tokens at list price) and refuse with
``BudgetExceeded`` if either the global or the project total would exceed its cap.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import portalocker


class BudgetExceeded(Exception):
    """Raised before a paid call when the worst-case estimate would break a spend cap."""


@dataclass
class _Totals:
    offset: int = 0
    global_usd: float = 0.0
    by_project: dict[str, float] = field(default_factory=dict)


class SpendLedger:
    def __init__(
        self, path: str | os.PathLike[str], project: str, project_cap_usd: float, global_cap_usd: float
    ):
        self.path = Path(path)
        self.project = project
        self.project_cap_usd = project_cap_usd
        self.global_cap_usd = global_cap_usd
        self._totals = _Totals()
        self._reserved = 0.0  # in-flight worst-case estimates from this process
        self._mu = threading.Lock()

    def _refresh(self) -> _Totals:
        """Incrementally read lines appended since the last refresh (other projects append too)."""
        if not self.path.exists():
            return self._totals
        with self.path.open("rb") as fh:
            portalocker.lock(fh, portalocker.LOCK_EX)  # msvcrt has no shared locks
            try:
                fh.seek(self._totals.offset)
                data = fh.read()
            finally:
                portalocker.unlock(fh)
        end = data.rfind(b"\n") + 1  # only consume complete lines
        for line in data[:end].splitlines():
            try:
                row = json.loads(line)
                usd = float(row.get("usd", 0.0))
            except (ValueError, TypeError):
                continue
            self._totals.global_usd += usd
            proj = str(row.get("project", ""))
            self._totals.by_project[proj] = self._totals.by_project.get(proj, 0.0) + usd
        self._totals.offset += end
        return self._totals

    def totals(self) -> tuple[float, float]:
        with self._mu:
            t = self._refresh()
            return t.global_usd, t.by_project.get(self.project, 0.0)

    def reserve(self, estimate_usd: float) -> None:
        """Check the caps including other in-flight reservations; raise BudgetExceeded on breach."""
        with self._mu:
            t = self._refresh()
            proj = t.by_project.get(self.project, 0.0)
            if t.global_usd + self._reserved + estimate_usd > self.global_cap_usd:
                raise BudgetExceeded(f"global cap {self.global_cap_usd} USD would be exceeded")
            if proj + self._reserved + estimate_usd > self.project_cap_usd:
                raise BudgetExceeded(f"project cap {self.project_cap_usd} USD would be exceeded")
            self._reserved += estimate_usd

    def release(self, estimate_usd: float) -> None:
        with self._mu:
            self._reserved = max(0.0, self._reserved - estimate_usd)

    def record(
        self,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        usd: float,
        note: str | None = None,
    ) -> None:
        row: dict[str, object] = {
            "ts": time.time(),
            "project": self.project,
            "provider": provider,
            "model": model,
            "input_tokens": int(input_tokens),
            "output_tokens": int(output_tokens),
            "usd": round(usd, 10),
        }
        if note:
            row["note"] = note
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(row) + "\n").encode("utf-8")
        with self.path.open("ab") as fh:
            portalocker.lock(fh, portalocker.LOCK_EX)
            try:
                fh.write(line)
                fh.flush()
            finally:
                portalocker.unlock(fh)


def estimate_tokens(text_chars: int) -> int:
    """Conservative token estimate: ~3 chars per token plus chat-template overhead."""
    return text_chars // 3 + 100
