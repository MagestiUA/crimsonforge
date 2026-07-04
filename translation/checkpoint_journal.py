"""Lightweight per-row checkpoint journal for in-progress batch translations.

The main TranslationProject is a single big JSON file - full rewrites cost
real time regardless of how much actually changed (measured: ~4s for a
187k-entry / 165MB project) and would otherwise have to run once per
completed string during a batch to guarantee no lost work. This journal
instead does a single-row SQLite upsert per completed item (sub-millisecond,
independent of overall project size) during the run. The JSON file stays
the one authoritative format - nothing else in the app needs to change -
and gets rewritten only at natural pause points (batch complete, the
existing periodic autosave timer, explicit Save). If the app exits before
one of those happens (crash, force-quit), `merge_into` replays whatever the
journal has that isn't reflected in the freshly loaded project yet, so
translated work from mid-run is never lost.
"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from translation.translation_state import StringStatus
from utils.logger import get_logger

logger = get_logger("translation.checkpoint_journal")

JOURNAL_PATH = Path.home() / ".crimsonforge" / "checkpoint_journal.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS journal (
    key TEXT PRIMARY KEY,
    translated_text TEXT,
    status TEXT,
    ai_provider TEXT,
    ai_model TEXT,
    ai_tokens INTEGER,
    ai_cost REAL,
    manually_edited INTEGER,
    updated_at TEXT
)
"""


@contextmanager
def _connect():
    JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(JOURNAL_PATH), timeout=10)
    try:
        conn.execute(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def record(entry) -> None:
    """Upsert one completed translation into the journal.

    Safe to call after every single completed item in a batch - a
    single-row write, independent of how large the overall project is.
    """
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO journal (key, translated_text, status, ai_provider, "
                "ai_model, ai_tokens, ai_cost, manually_edited, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now')) "
                "ON CONFLICT(key) DO UPDATE SET "
                "translated_text=excluded.translated_text, status=excluded.status, "
                "ai_provider=excluded.ai_provider, ai_model=excluded.ai_model, "
                "ai_tokens=excluded.ai_tokens, ai_cost=excluded.ai_cost, "
                "manually_edited=excluded.manually_edited, updated_at=excluded.updated_at",
                (
                    entry.key, entry.translated_text, entry.status.value,
                    entry.ai_provider, entry.ai_model, entry.ai_tokens,
                    entry.ai_cost, int(entry.manually_edited),
                ),
            )
    except Exception as e:
        logger.error("Failed to write checkpoint journal entry for key %s: %s", entry.key, e)


def merge_into(project) -> int:
    """Apply any journal rows the given project doesn't already reflect.

    Call this right after loading a project, in case the app exited before
    its last journal entries made it into a full JSON save. Returns the
    number of entries recovered.
    """
    if not JOURNAL_PATH.is_file():
        return 0

    by_key = {e.key: e for e in project.entries}
    recovered = 0
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT key, translated_text, status, ai_provider, ai_model, "
                "ai_tokens, ai_cost, manually_edited FROM journal"
            ).fetchall()
    except Exception as e:
        logger.error("Failed to read checkpoint journal: %s", e)
        return 0

    for key, text, status, provider, model, tokens, cost, manually_edited in rows:
        entry = by_key.get(key)
        if entry is None:
            continue
        if entry.translated_text == text and entry.status.value == status:
            continue
        entry.translated_text = text
        entry.status = StringStatus(status)
        entry.ai_provider = provider or ""
        entry.ai_model = model or ""
        entry.ai_tokens = tokens or 0
        entry.ai_cost = cost or 0.0
        entry.manually_edited = bool(manually_edited)
        recovered += 1

    if recovered:
        project.mark_modified()
        logger.info("Recovered %d entries from checkpoint journal", recovered)
    return recovered


def clear() -> None:
    """Wipe the journal - call after a full JSON save succeeds, since every
    journalled change is by then already reflected in the canonical file."""
    try:
        if JOURNAL_PATH.is_file():
            JOURNAL_PATH.unlink()
    except Exception as e:
        logger.warning("Failed to clear checkpoint journal: %s", e)
