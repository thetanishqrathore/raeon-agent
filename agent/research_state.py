#!/usr/bin/env python3
"""Durable research scratchpad / ledger (P1.3).

The deep-research brief + running state (learnings, visited URLs, claims) live
in the model's message list today, so context compaction can silently discard
them mid-run — the agent forgets what it already found and re-treads ground
(audit bottleneck #2). This module holds that state in SQLite, OUTSIDE the
message list, keyed by session id, so it survives any number of compactions.

Deliberately decoupled from the compaction/prompt-cache hot path: it is a plain
store the agent reads/writes via the ``research_ledger`` tool. Nothing here
touches the 3-tier cached system prompt (which must never be re-rendered
mid-session) — the ledger is surfaced only via explicit tool reads, never
injected into the cached prompt. *(local-deep-researcher running-summary;
deepagents offload-to-disk; Co-STORM summary-as-context.)*

Pure storage + rendering; thread-safe; ``db_path`` injectable for tests.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.research_citations import canonicalize_url


_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS research_state ("
    "session_id TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at REAL NOT NULL)"
)

# Keep the ledger bounded so a long run can't grow it without limit.
_MAX_LEARNINGS = 200
_MAX_VISITED = 500
_MAX_CLAIMS = 200


def _empty_state() -> Dict[str, Any]:
    return {"brief": "", "learnings": [], "visited_urls": [], "claims": []}


class ResearchLedger:
    """SQLite-backed per-session research state. Survives context compaction."""

    def __init__(self, session_id: Optional[str], db_path: Optional[str] = None):
        self.session_id = (session_id or "default").strip() or "default"
        if db_path is None:
            try:
                from hermes_constants import get_hermes_home
                db_path = str(Path(get_hermes_home()) / "research_ledger.db")
            except Exception:
                db_path = str(Path.home() / ".hermes" / "research_ledger.db")
        self.db_path = str(db_path)
        self._lock = threading.RLock()
        self._ensure()

    # -- storage ----------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(self.db_path, timeout=10)

    def _ensure(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(_SCHEMA)

    @staticmethod
    def _parse_row(row) -> Dict[str, Any]:
        if not row:
            return _empty_state()
        try:
            data = json.loads(row[0])
            base = _empty_state()
            base.update({k: data.get(k, base[k]) for k in base})
            return base
        except (ValueError, TypeError):
            return _empty_state()

    def _load(self) -> Dict[str, Any]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT data FROM research_state WHERE session_id=?",
                (self.session_id,),
            ).fetchone()
        return self._parse_row(row)

    def _mutate(self, mutator) -> Any:
        """Atomically load→modify→save via ``mutator(data)``.

        The whole read-modify-write runs inside one ``BEGIN IMMEDIATE``
        transaction, so concurrent mutators — parallel tool calls (which each
        construct a fresh ledger instance), other threads, or other processes
        sharing the DB — serialize instead of silently dropping each other's
        writes. Returns whatever *mutator* returns.
        """
        # time.time() is fine here (agent runtime, not a workflow script).
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT data FROM research_state WHERE session_id=?",
                    (self.session_id,),
                ).fetchone()
                data = self._parse_row(row)
                result = mutator(data)
                conn.execute(
                    "INSERT INTO research_state (session_id, data, updated_at) "
                    "VALUES (?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
                    "data=excluded.data, updated_at=excluded.updated_at",
                    (self.session_id, json.dumps(data, ensure_ascii=False), time.time()),
                )
                conn.commit()
                return result
            except Exception:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise
            finally:
                conn.close()

    # -- mutations --------------------------------------------------------
    # All mutations go through _mutate() so the load→modify→save cycle is
    # atomic — parallel tool calls must not drop each other's writes.
    def set_brief(self, brief: str) -> None:
        def _apply(d: Dict[str, Any]) -> None:
            d["brief"] = (brief or "").strip()
        self._mutate(_apply)

    def add_learning(
        self, claim: str, source_url: str = "", date: str = "", confidence: str = ""
    ) -> None:
        claim = (claim or "").strip()
        if not claim:
            return

        def _apply(d: Dict[str, Any]) -> None:
            d["learnings"].append({
                "claim": claim,
                "source_url": (source_url or "").strip(),
                "date": (date or "").strip(),
                "confidence": (confidence or "").strip(),
            })
            d["learnings"] = d["learnings"][-_MAX_LEARNINGS:]
            # Adding a learning with a source implicitly marks it visited.
            if source_url:
                self._add_visited(d, source_url)

        self._mutate(_apply)

    def _add_visited(self, d: Dict[str, Any], url: str) -> bool:
        c = canonicalize_url(url)
        if not c:
            return False
        existing = {canonicalize_url(u) for u in d["visited_urls"]}
        if c in existing:
            return False
        d["visited_urls"].append(url.strip())
        d["visited_urls"] = d["visited_urls"][-_MAX_VISITED:]
        return True

    def add_visited_url(self, url: str) -> bool:
        """Record a visited URL. Returns True if it was new (not a dup)."""
        return bool(self._mutate(lambda d: self._add_visited(d, url)))

    def add_claim(self, claim: str, status: str = "", evidence_url: str = "") -> None:
        claim = (claim or "").strip()
        if not claim:
            return

        def _apply(d: Dict[str, Any]) -> None:
            d["claims"].append({
                "claim": claim,
                "status": (status or "").strip(),
                "evidence_url": (evidence_url or "").strip(),
            })
            d["claims"] = d["claims"][-_MAX_CLAIMS:]

        self._mutate(_apply)

    def clear(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM research_state WHERE session_id=?", (self.session_id,)
            )

    # -- reads ------------------------------------------------------------
    def get_state(self) -> Dict[str, Any]:
        return self._load()

    def is_visited(self, url: str) -> bool:
        c = canonicalize_url(url)
        return c in {canonicalize_url(u) for u in self._load()["visited_urls"]}

    def render_digest(self, max_learnings: int = 30, max_visited: int = 40) -> str:
        """Compact, model-readable snapshot — what to re-read after compaction."""
        d = self._load()
        lines = [f"RESEARCH LEDGER (session {self.session_id})"]
        if d["brief"]:
            lines.append(f"\nBRIEF:\n{d['brief']}")
        lv = d["learnings"]
        lines.append(f"\nLEARNINGS ({len(lv)}):")
        for it in lv[-max_learnings:]:
            src = f" — {it['source_url']}" if it.get("source_url") else ""
            dt = f" ({it['date']})" if it.get("date") else ""
            conf = f" [{it['confidence']}]" if it.get("confidence") else ""
            lines.append(f"- {it['claim']}{src}{dt}{conf}")
        cv = d["claims"]
        if cv:
            lines.append(f"\nCLAIMS ({len(cv)}):")
            for it in cv[-max_learnings:]:
                st = f" [{it['status']}]" if it.get("status") else ""
                ev = f" — {it['evidence_url']}" if it.get("evidence_url") else ""
                lines.append(f"- {it['claim']}{st}{ev}")
        vu = d["visited_urls"]
        lines.append(f"\nVISITED URLS ({len(vu)}):")
        for u in vu[-max_visited:]:
            lines.append(f"- {u}")
        if len(vu) > max_visited:
            lines.append(f"- … and {len(vu) - max_visited} more")
        return "\n".join(lines)
