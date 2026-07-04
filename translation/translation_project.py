"""Translation project manager - load, save, and manage translation projects.

A translation project is a SQLite-backed file that stores all translation
entries, their states, and metadata. Projects can be saved and reopened
later. Legacy single-JSON-file projects (pre-1.28) are migrated to SQLite
in place, transparently, the first time they're loaded.
"""

import json
import os
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from translation.translation_state import TranslationEntry, StringStatus
from utils.logger import get_logger

logger = get_logger("translation.project")


class TranslationProject:
    """Manages a translation project with persistent state."""

    def __init__(self):
        self._entries: list[TranslationEntry] = []
        self._index_map: dict[int, TranslationEntry] = {}
        self._source_lang: str = ""
        self._target_lang: str = ""
        self._source_file: str = ""
        self._project_file: str = ""
        self._modified: bool = False
        # Guards save() - the periodic AutosaveManager timer (UI thread) and
        # a batch translation's per-chunk checkpoint (worker thread) can both
        # try to write the same file at once. Without this, two concurrent
        # writers racing on the same "<path>.tmp" produce WinError 32 on
        # Windows (os.replace fails while the other writer still has the
        # tmp file open).
        self._save_lock = threading.Lock()
        self._created_at: str = ""
        self._updated_at: str = ""
        self._game_build_id: str = ""
        self._game_build_display: str = ""
        self._game_fingerprint: str = ""
        self._update_history: list[dict] = []
        self._last_sync_summary: dict = {}

    def create_from_paloc(
        self,
        entries: list[tuple[str, str]],
        source_lang: str,
        target_lang: str,
        source_file: str,
        game_build_id: str = "",
        game_build_display: str = "",
        game_fingerprint: str = "",
    ) -> None:
        """Create a new project from paloc key-value pairs.

        Args:
            entries: List of (key, value) tuples from paloc parser.
            source_lang: Source language code.
            target_lang: Target language code.
            source_file: Path to the source paloc file.
        """
        self._entries = []
        for i, (key, value) in enumerate(entries):
            entry = TranslationEntry(
                index=i,
                key=key,
                original_text=value,
            )
            # Auto-lock untranslatable entries: empty text, developer
            # placeholders (PHM_, PHW_, PHF_, TODO, TBD).  These keep
            # their original value as the "translation" and are marked
            # APPROVED + locked so they cannot be edited or sent to AI.
            stripped = value.strip()
            if (not stripped
                    or stripped.startswith(("PHM_", "PHW_", "PHF_", "TODO", "TBD"))):
                entry.translated_text = value
                entry.status = StringStatus.APPROVED
                entry.locked = True
                entry.notes = "auto-locked: untranslatable"
            if game_build_id:
                entry.game_introduced_version = game_build_id
                entry.game_last_seen_version = game_build_id
                entry.record_game_event(game_build_id, "baseline")
            self._entries.append(entry)
        self._source_lang = source_lang
        self._target_lang = target_lang
        self._source_file = source_file
        self._game_build_id = game_build_id
        self._game_build_display = game_build_display or game_build_id
        self._game_fingerprint = game_fingerprint
        self._created_at = datetime.now().isoformat()
        self._updated_at = self._created_at
        self._modified = True
        self._rebuild_index_map()
        logger.info("Created project: %d entries, %s -> %s", len(self._entries), source_lang, target_lang)

    def save(self, path: str = "") -> str:
        """Save the project to its SQLite-backed file.

        Args:
            path: File path to save to. Uses existing path if empty.

        Returns:
            Path the project was saved to.
        """
        with self._save_lock:
            if path:
                self._project_file = path
            if not self._project_file:
                raise ValueError(
                    "No project file path specified. Use save(path) to set the save location."
                )

            self._updated_at = datetime.now().isoformat()

            meta = {
                "source_lang": self._source_lang,
                "target_lang": self._target_lang,
                "source_file": self._source_file,
                "game_build_id": self._game_build_id,
                "game_build_display": self._game_build_display,
                "game_fingerprint": self._game_fingerprint,
                "created_at": self._created_at,
                "updated_at": self._updated_at,
                "update_history": self._update_history,
                "last_sync_summary": self._last_sync_summary,
            }

            from translation import translation_db
            translation_db.save_all(self._project_file, meta, self._entries)

            self._modified = False

        return self._project_file

    def load(self, path: str) -> None:
        """Load a project, migrating a legacy JSON file to SQLite in place
        the first time it's opened."""
        if not os.path.exists(path):
            raise FileNotFoundError(
                f"Project file not found: {path}. "
                f"Check that the file exists and has not been moved."
            )

        from translation import translation_db

        if translation_db.is_sqlite_file(path):
            meta, self._entries = translation_db.load_all(path)
        else:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._entries = [
                TranslationEntry.from_dict(d) for d in data.get("entries", [])
            ]
            meta = {
                "source_lang": data.get("source_lang", ""),
                "target_lang": data.get("target_lang", ""),
                "source_file": data.get("source_file", ""),
                "game_build_id": data.get("game_build_id", ""),
                "game_build_display": data.get("game_build_display", ""),
                "game_fingerprint": data.get("game_fingerprint", ""),
                "created_at": data.get("created_at", ""),
                "updated_at": data.get("updated_at", ""),
                "update_history": data.get("update_history", []),
                "last_sync_summary": data.get("last_sync_summary", {}),
            }
            # Keep the original JSON as a backup and build the new SQLite
            # file at a temp path first - the on-disk project is never left
            # in a half-migrated state if this fails partway.
            backup_path = path + ".legacy-json-backup"
            if not os.path.exists(backup_path):
                shutil.copy2(path, backup_path)
            tmp_db_path = path + ".migrating.tmp"
            if os.path.exists(tmp_db_path):
                os.remove(tmp_db_path)
            translation_db.save_all(tmp_db_path, meta, self._entries)
            os.replace(tmp_db_path, path)
            logger.info(
                "Migrated legacy JSON project to SQLite in place: %s (backup: %s)",
                path, backup_path,
            )

        self._source_lang = meta.get("source_lang", "")
        self._target_lang = meta.get("target_lang", "")
        self._source_file = meta.get("source_file", "")
        self._game_build_id = meta.get("game_build_id", "")
        self._game_build_display = meta.get("game_build_display", self._game_build_id)
        self._game_fingerprint = meta.get("game_fingerprint", "")
        self._update_history = list(meta.get("update_history", []))
        self._last_sync_summary = dict(meta.get("last_sync_summary", {}))
        self._created_at = meta.get("created_at", "")
        self._updated_at = meta.get("updated_at", "")
        self._project_file = path

        self._modified = False
        self._rebuild_index_map()
        logger.info("Project loaded: %s (%d entries)", path, len(self._entries))

    def persist_entry(self, entry: TranslationEntry) -> None:
        """Immediately persist a single entry's current state to disk.

        Used during a running translation batch so completed work survives
        a crash without waiting for the next full save() - a single SQLite
        row upsert, independent of overall project size. No-op until the
        project has been saved at least once (no path to write to yet).
        """
        if not self._project_file:
            return
        from translation import translation_db
        translation_db.upsert_entry(self._project_file, entry)

    def _compute_stats(self) -> dict:
        stats = {s.value: 0 for s in StringStatus}
        for e in self._entries:
            stats[e.status.value] += 1
        stats["total"] = len(self._entries)
        return stats

    @property
    def entries(self) -> list[TranslationEntry]:
        return self._entries

    @property
    def source_lang(self) -> str:
        return self._source_lang

    @property
    def target_lang(self) -> str:
        return self._target_lang

    @target_lang.setter
    def target_lang(self, value: str):
        self._target_lang = value
        self._modified = True

    @property
    def source_file(self) -> str:
        return self._source_file

    @property
    def project_file(self) -> str:
        return self._project_file

    @property
    def modified(self) -> bool:
        return self._modified

    @modified.setter
    def modified(self, value: bool):
        self._modified = value

    @property
    def entry_count(self) -> int:
        return len(self._entries)

    def get_entry(self, index: int) -> Optional[TranslationEntry]:
        """Get entry by its .index property (NOT list position).
        Uses O(1) hash map lookup for 102K+ entries.
        """
        return self._index_map.get(index)

    def _rebuild_index_map(self):
        """Rebuild the index→entry hash map after entries change."""
        self._index_map = {e.index: e for e in self._entries}

    def get_pending_entries(self) -> list[TranslationEntry]:
        return [e for e in self._entries if e.status == StringStatus.PENDING]

    def get_entries_by_status(self, status: StringStatus) -> list[TranslationEntry]:
        return [e for e in self._entries if e.status == status]

    def get_stats(self) -> dict:
        return self._compute_stats()

    def mark_modified(self):
        self._modified = True

    @property
    def game_build_id(self) -> str:
        return self._game_build_id

    @property
    def game_build_display(self) -> str:
        return self._game_build_display

    @property
    def game_fingerprint(self) -> str:
        return self._game_fingerprint

    @property
    def update_history(self) -> list[dict]:
        return list(self._update_history)

    @property
    def last_sync_summary(self) -> dict:
        return dict(self._last_sync_summary)

    def set_game_build(self, build_id: str, build_display: str = "", fingerprint: str = "") -> None:
        self._game_build_id = build_id
        self._game_build_display = build_display or build_id
        self._game_fingerprint = fingerprint
        self._modified = True

    def record_sync_summary(self, summary: dict) -> None:
        if not summary:
            return
        self._last_sync_summary = dict(summary)
        version = summary.get("version", "")
        if version:
            normalized = {
                "version": version,
                "display": summary.get("display", version),
                "new": int(summary.get("new", 0)),
                "changed": int(summary.get("changed", 0)),
                "removed": int(summary.get("removed", 0)),
            }
            existing = next(
                (item for item in self._update_history if item.get("version") == version),
                None,
            )
            has_real_changes = any(
                normalized.get(key, 0) > 0 for key in ("new", "changed", "removed")
            )
            if existing is None:
                self._update_history.append(normalized)
            elif has_real_changes or not any(
                int(existing.get(key, 0)) > 0 for key in ("new", "changed", "removed")
            ):
                existing.update(normalized)
        self._modified = True
