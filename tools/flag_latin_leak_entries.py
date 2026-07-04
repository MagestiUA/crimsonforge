"""One-off quality pass: requeue "translated" entries whose translation
still contains stray Latin-script text for re-translation.

Two real sources of this:
1. Entries translated before the transliteration prompt fix (character/
   place names were previously left untranslated in the source script -
   see ai/prompt_manager.py rule 6), so an otherwise-Ukrainian sentence
   has a name sitting in it in Latin letters.
2. A batch response that silently echoed the source text back instead of
   translating it (rare, but not otherwise detectable after the fact).

Protected placeholder tokens (<br/>, {Key:...}, %s, etc.) legitimately
contain Latin letters and must not be flagged - this reuses the same
encode_for_translation() used by the live translation pipeline to strip
every recognized placeholder before checking for leftover Latin text, so
only genuine untranslated prose trips the check.

Flagged entries are requeued exactly like a detected source-text change:
reset to PENDING with the old (Latin-contaminated) translation stashed in
notes for reference, so nothing is lost and the next Auto-Translate-All
run picks them up automatically with the current (improved) pipeline.
Locked entries (auto-approved placeholders) are never touched.

Usage:
    python tools/flag_latin_leak_entries.py [--project PATH] [--dry-run]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.translation_tokenizer import encode_for_translation  # noqa: E402
from translation.translation_project import TranslationProject  # noqa: E402
from translation.translation_state import StringStatus  # noqa: E402

DEFAULT_PROJECT_PATH = Path.home() / ".crimsonforge" / "autosave_project.json"

# The sentinel format itself (see core/translation_tokenizer.py) spells out
# the Latin letters "CF" (⟦CF0⟧, ⟦/CF0⟧, ...) - strip sentinels first or
# every entry with any protected placeholder trips the Latin check on its
# own encoding artifact rather than real leaked text.
_SENTINEL_STRIP_RE = re.compile(r"⟦/?CF\d+⟧")
_LATIN_WORD_RE = re.compile(r"[A-Za-z]+")
# Roman numerals (item/blueprint tiers: "Blueprint II", "+III") are a
# legitimate Latin-script convention kept as-is in translated text, same
# as digits - not a translation failure.
_ROMAN_NUMERAL_RE = re.compile(r"^[IVXLCDM]+$", re.IGNORECASE)

_TRANSLATED_STATUSES = (StringStatus.TRANSLATED, StringStatus.REVIEWED, StringStatus.APPROVED)


def has_leaked_latin(translated_text: str) -> bool:
    encoded, _ = encode_for_translation(translated_text)
    stripped = _SENTINEL_STRIP_RE.sub("", encoded)
    words = _LATIN_WORD_RE.findall(stripped)
    return any(not _ROMAN_NUMERAL_RE.match(w) for w in words)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=str(DEFAULT_PROJECT_PATH))
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would change without saving.")
    args = parser.parse_args()

    project = TranslationProject()
    project.load(args.project)

    flagged = 0
    samples = []
    for entry in project.entries:
        if entry.locked or entry.status not in _TRANSLATED_STATUSES:
            continue
        if not entry.translated_text:
            continue
        if not has_leaked_latin(entry.translated_text):
            continue

        if len(samples) < 20:
            samples.append((entry.key, entry.translated_text[:80]))

        if not args.dry_run:
            old_translation = entry.translated_text
            stale_note = f"[Latin-leak requeue - previous translation: {old_translation!r}]"
            entry.notes = f"{entry.notes}\n{stale_note}".strip() if entry.notes else stale_note
            entry.revert_to_pending()
        flagged += 1

    print(f"Project:  {args.project}")
    print(f"Total entries: {len(project.entries)}")
    print(f"Flagged (translated but contain unprotected Latin text): {flagged}")
    for key, preview in samples:
        print(f"  {key!r}: {preview!r}")

    if args.dry_run:
        print("\nDry run - no changes saved.")
        return

    if flagged:
        project.save(args.project)
        print(f"\nSaved. {flagged} entries reset to PENDING (old translation kept in notes).")
    else:
        print("\nNothing to flag.")


if __name__ == "__main__":
    main()
