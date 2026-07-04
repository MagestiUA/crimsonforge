"""One-off repair: restore original_text for entries corrupted by the old
(pre-1.28) change-detection bug.

Background
----------
Before the source/patch-target split (see CHANGELOG), "Patch to Game"
always wrote translated text back into the SAME file it read as the
source. Once a project had been patched at least once, a later "check
for game updates" pass could read that already-patched file back as if
it were fresh, pristine source text - silently overwriting
TranslationEntry.original_text with the entry's own previous
translation for every entry whose translation happened to still be
sitting in the game's "source language" slot at that moment.

BaselineManager (translation/baseline_manager.py) was never affected -
its whole design is "record a key's value once, never touch it again" -
so it holds the one surviving copy of genuinely pristine source text
for every key that's ever been loaded through the Translate tab.

This script diffs every entry's original_text against the protected
baseline and repairs only the entries where original_text looks like
corruption (identical to translated_text, or contains characters
outside the source language's expected script). It never touches
translated_text or status - the existing translation is presumably
still valid for the (unchanged, only mis-recorded) source text, so
resetting it to PENDING would just waste AI calls re-translating text
that was never actually en route to change.

Usage
-----
    python tools/repair_corrupted_original_text.py [--project PATH] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from translation.translation_project import TranslationProject  # noqa: E402
from translation.baseline_manager import BaselineManager  # noqa: E402

DEFAULT_PROJECT_PATH = Path.home() / ".crimsonforge" / "autosave_project.json"

# Corruption signature: original_text got overwritten with the entry's own
# translation, so it now contains characters that can't belong to the
# source language (e.g. Cyrillic for an English source).
_NON_LATIN_RE = re.compile(r"[Ѐ-ӿЀ-ӿ]")


def looks_like_corruption(original_text: str, translated_text: str) -> bool:
    if original_text and original_text == translated_text:
        return True
    return bool(_NON_LATIN_RE.search(original_text))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=str(DEFAULT_PROJECT_PATH))
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would change without saving.")
    args = parser.parse_args()

    project = TranslationProject()
    project.load(args.project)

    baseline_mgr = BaselineManager()
    baseline = baseline_mgr.load_baseline(project.source_file)
    if baseline is None:
        sys.exit(f"No baseline found for source file: {project.source_file}")

    repaired = 0
    missing_from_baseline = 0
    unrecognized_mismatch = []

    for entry in project.entries:
        true_source = baseline.get(entry.key)
        if true_source is None:
            missing_from_baseline += 1
            continue
        if entry.original_text == true_source:
            continue
        if looks_like_corruption(entry.original_text, entry.translated_text):
            if not args.dry_run:
                entry.original_text = true_source
            repaired += 1
        else:
            unrecognized_mismatch.append(
                (entry.key, entry.original_text[:60], true_source[:60])
            )

    print(f"Project:                 {args.project}")
    print(f"Total entries:           {len(project.entries)}")
    print(f"Repaired:                {repaired}")
    print(f"Missing from baseline:   {missing_from_baseline}")
    print(f"Unrecognized mismatches: {len(unrecognized_mismatch)}")
    for key, old, true in unrecognized_mismatch[:10]:
        print(f"  {key!r}: stored={old!r} baseline={true!r}")

    if args.dry_run:
        print("\nDry run - no changes saved.")
        return

    if repaired:
        project.save(args.project)
        print(f"\nSaved. {repaired} entries corrected (translations/status untouched).")
    else:
        print("\nNothing to repair.")


if __name__ == "__main__":
    main()
