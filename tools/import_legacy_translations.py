"""One-off migration: pull the Ukrainian translation memory out of the old
standalone "Translation Tool" (mod_pipeline.py + translation_progress.db)
and into a CrimsonForge translation project.

Why this exists
----------------
The old tool matches its saved `ukr_text` to the game's *current* English
strings by numeric key, but it has no notion of "the game patched since I
last ran this" - so after a few updates, new/changed English lines are
just left untranslated with no way to tell which ones. CrimsonForge tracks
this natively (BaselineManager + TranslationProject.game_*), but it has no
importer for the old SQLite database.

What this script does
----------------------
1. Reads the CURRENT localizationstring_eng.paloc straight from the game's
   pack 0020, using CrimsonForge's own VFS/paloc reader (same code path the
   Translate tab uses) - so the key set is up to date with whatever patches
   have shipped.
2. For every key, looks it up in translation_progress.db:
   - Key found AND the stored English text still matches -> import the old
     Ukrainian translation as-is (status TRANSLATED, provider "legacy-import").
   - Key found but the English text changed -> leave PENDING, but stash the
     stale translation in `notes` as a reference for the translator/AI.
   - Key not found at all -> leave PENDING (new string from a later patch).
3. Applies a small table of manual text corrections for known-bad labels.
4. Writes the result to ~/.crimsonforge/autosave_project.json, which is
   exactly the file CrimsonForge's Translate tab restores on startup - so
   opening the app and loading the game picks this project up automatically.

Usage
-----
    python tools/import_legacy_translations.py [--game-path PATH] [--legacy-db PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import struct
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.vfs_manager import VfsManager  # noqa: E402
from core.paloc_parser import parse_paloc  # noqa: E402
from core.checksum_engine import pa_checksum  # noqa: E402
from translation.translation_project import TranslationProject  # noqa: E402
from translation.translation_state import StringStatus  # noqa: E402

DEFAULT_GAME_PATH = "D:/SteamLibrary/steamapps/common/Crimson Desert"
DEFAULT_LEGACY_DB = "F:/Python_Local/Translation Tool-300-2-1774990844/translation_progress.db"
SOURCE_PACK = "0020"
SOURCE_PALOC_NAME = "localizationstring_eng.paloc"
TARGET_LANG = "uk"

RECOVERY_DIR = Path.home() / ".crimsonforge"
PROJECT_PATH = RECOVERY_DIR / "autosave_project.json"
UI_STATE_PATH = RECOVERY_DIR / "autosave_ui_state.json"

# Known-bad labels to fix regardless of what the legacy DB says.
# Key 11054763203018883248 is the English "Save/Load Game" button; the
# literal translation ("Зберегти/завантажити гру") reads as clutter in the
# menu where it's actually used - shortened per user request.
MANUAL_OVERRIDES: dict[str, str] = {
    "11054763203018883248": "Зберегти гру",
}


def get_game_build_info(game_path: str) -> dict:
    """Mirrors TranslateTab._get_game_build_info() so imported projects carry
    the same build_id/fingerprint format the app expects."""
    info = {"build_id": "", "build_display": "", "fingerprint": ""}
    try:
        paver_path = os.path.join(game_path, "meta", "0.paver")
        version_text = ""
        if os.path.isfile(paver_path):
            with open(paver_path, "rb") as f:
                paver_data = f.read()
            if len(paver_data) >= 6:
                major, minor, patch = struct.unpack_from("<HHH", paver_data, 0)
                version_text = f"v{major}.{minor:02d}.{patch:02d}"

        papgt_path = os.path.join(game_path, "meta", "0.papgt")
        crc = 0
        size = 0
        if os.path.isfile(papgt_path):
            with open(papgt_path, "rb") as f:
                papgt_data = f.read()
            size = len(papgt_data)
            if len(papgt_data) > 12:
                crc = pa_checksum(papgt_data[12:])

        if version_text and crc:
            info["build_id"] = f"{version_text}|CRC:0x{crc:08X}"
            info["build_display"] = f"{version_text} | CRC 0x{crc:08X}"
        elif version_text:
            info["build_id"] = version_text
            info["build_display"] = version_text
        elif crc:
            info["build_id"] = f"CRC:0x{crc:08X}"
            info["build_display"] = f"CRC 0x{crc:08X}"

        if info["build_id"]:
            info["fingerprint"] = f"{info['build_id']}|SIZE:{size}"
        elif size:
            info["fingerprint"] = f"SIZE:{size}"
    except Exception:
        pass
    return info


def load_legacy_map(db_path: str) -> dict[str, dict]:
    """key -> {"eng_text": ..., "ukr_text": ..., "status": ...}"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT key, eng_text, ukr_text, status FROM entries "
            "WHERE TRIM(COALESCE(ukr_text, '')) != ''"
        ).fetchall()
        return {r["key"]: dict(r) for r in rows}
    finally:
        conn.close()


def read_current_english_entries(game_path: str) -> list[tuple[str, str]]:
    vfs = VfsManager(game_path)
    pamt = vfs.load_pamt(SOURCE_PACK)
    entry = next(
        (e for e in pamt.file_entries if e.path.lower().endswith(SOURCE_PALOC_NAME)),
        None,
    )
    if entry is None:
        raise FileNotFoundError(
            f"{SOURCE_PALOC_NAME} not found in pack {SOURCE_PACK} under {game_path}"
        )
    raw = vfs.read_entry_data(entry)
    parsed = parse_paloc(raw)
    return [(e.key, e.value) for e in parsed if not e.key.startswith(("@", "#"))]


def build_project(game_path: str, legacy_db: str) -> tuple[TranslationProject, dict]:
    build_info = get_game_build_info(game_path)
    current_entries = read_current_english_entries(game_path)
    legacy = load_legacy_map(legacy_db)

    project = TranslationProject()
    project.create_from_paloc(
        current_entries,
        source_lang="en",
        target_lang=TARGET_LANG,
        source_file=SOURCE_PALOC_NAME,
        game_build_id=build_info["build_id"],
        game_build_display=build_info["build_display"],
        game_fingerprint=build_info["fingerprint"],
    )

    stats = {"total": len(project.entries), "imported": 0, "stale_source": 0,
              "overridden": 0, "pending_new": 0, "auto_locked": 0}

    for entry in project.entries:
        if entry.locked:
            stats["auto_locked"] += 1
            continue

        override = MANUAL_OVERRIDES.get(entry.key)
        if override:
            entry.set_translated(override, provider="manual-fix", model="")
            entry.set_approved()
            stats["overridden"] += 1
            continue

        legacy_row = legacy.get(entry.key)
        if legacy_row is None:
            stats["pending_new"] += 1
            continue

        if legacy_row["eng_text"] == entry.original_text:
            entry.set_translated(
                legacy_row["ukr_text"], provider="legacy-import",
                model="translation_progress.db",
            )
            stats["imported"] += 1
        else:
            entry.notes = (
                "legacy translation (stale, source text changed): "
                + legacy_row["ukr_text"]
            )
            stats["stale_source"] += 1

    return project, stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-path", default=DEFAULT_GAME_PATH)
    parser.add_argument("--legacy-db", default=DEFAULT_LEGACY_DB)
    args = parser.parse_args()

    if not os.path.isdir(args.game_path):
        sys.exit(f"Game path not found: {args.game_path}")
    if not os.path.isfile(args.legacy_db):
        sys.exit(f"Legacy database not found: {args.legacy_db}")

    RECOVERY_DIR.mkdir(parents=True, exist_ok=True)
    if PROJECT_PATH.is_file():
        backup_path = PROJECT_PATH.with_suffix(
            f".backup-{datetime.now():%Y%m%d-%H%M%S}.json"
        )
        PROJECT_PATH.replace(backup_path)
        print(f"Existing autosave project backed up to {backup_path}")

    project, stats = build_project(args.game_path, args.legacy_db)
    project.save(str(PROJECT_PATH))

    UI_STATE_PATH.write_text(
        json.dumps({
            "target_lang": TARGET_LANG,
            "provider_id": "ollama",
            "game_fingerprint": project.game_fingerprint,
        }, indent=2),
        encoding="utf-8",
    )

    print(f"Project saved to {PROJECT_PATH}")
    print(f"Game build: {project.game_build_display or '(unknown)'}")
    print(f"Total strings:        {stats['total']:,}")
    print(f"Imported from legacy: {stats['imported']:,}")
    print(f"Manually corrected:   {stats['overridden']:,}")
    print(f"Stale (source changed, needs re-translation): {stats['stale_source']:,}")
    print(f"New / never translated (needs translation):   {stats['pending_new']:,}")
    print(f"Auto-locked (empty/placeholder):               {stats['auto_locked']:,}")
    print()
    print("Open CrimsonForge, load the game, and go to the Translate tab - "
          "this project loads automatically. Only the 'stale' and 'new' "
          "entries above are left pending for AI translation.")


if __name__ == "__main__":
    main()
