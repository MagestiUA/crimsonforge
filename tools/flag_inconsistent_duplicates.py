"""One-off quality pass: find entries that share identical source text but
disagree on translation, and requeue all of them for re-translation.

Root cause: `_dedup_and_translate` only deduplicates WITHIN a single batch
run - it translates each unique source text once and stamps the result
across every entry sharing it, but that guarantee only holds for entries
processed together. Entries translated in different sessions (different
providers, different points in time, before/after prompt fixes) can drift
apart even though their source text is byte-identical, e.g. one instance
of "Steal" correctly translated as "Вкрасти" while another instance
became "Вкрасти та їхати" (a hallucinated addition not present in the
source) from a separate run.

NOT every disagreement is a bug, though: the legacy-import translation
memory (from the old standalone tool, see tools/import_legacy_translations.py)
frequently reuses one generic English label for several distinct game
concepts and deliberately gives each a different, context-appropriate
Ukrainian translation - e.g. "Currency" is correctly "Валюта" in one
place and "Група предметів — злиток" in another, because a human
translator knew which specific item group each key represented. Treating
that as an inconsistency bug and forcing every instance to the same
generic AI-guessed translation would destroy real, curated work.

So this only flags disagreement that involves an AI-sourced translation
(anything with ai_provider != "legacy-import"):
  - If legacy-import itself disagrees internally, the group is left
    alone entirely (trusted source, deliberate context-specific choices).
  - If the AI-sourced entries disagree among themselves, those are flagged.
  - If the AI-sourced entries agree with each other but contradict a
    unanimous legacy-import translation, legacy is trusted and the
    AI-sourced entries are flagged.

Flagged entries are reset to PENDING (old text stashed in notes) so the
next Auto-Translate-All run translates the source text fresh and - via
the existing dedup logic - stamps every entry in that subset with the
same, single translation.

Usage:
    python tools/flag_inconsistent_duplicates.py [--project PATH] [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if sys.stdout.encoding is None or sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from translation.translation_project import TranslationProject  # noqa: E402
from translation.translation_state import TranslationEntry, StringStatus  # noqa: E402

DEFAULT_PROJECT_PATH = Path.home() / ".crimsonforge" / "autosave_project.json"

_TRANSLATED_STATUSES = (StringStatus.TRANSLATED, StringStatus.REVIEWED, StringStatus.APPROVED)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=str(DEFAULT_PROJECT_PATH))
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would change without saving.")
    args = parser.parse_args()

    project = TranslationProject()
    project.load(args.project)

    groups: dict[str, list[TranslationEntry]] = {}
    for entry in project.entries:
        if not entry.original_text:
            continue
        groups.setdefault(entry.original_text, []).append(entry)

    inconsistent_groups = 0
    flagged = 0
    samples = []

    for original_text, entries in groups.items():
        if len(entries) < 2:
            continue

        candidates = [
            e for e in entries
            if not e.locked and e.status in _TRANSLATED_STATUSES and e.translated_text
        ]
        if len(candidates) < 2:
            continue

        legacy = [e for e in candidates if e.ai_provider == "legacy-import"]
        non_legacy = [e for e in candidates if e.ai_provider != "legacy-import"]
        legacy_translations = {e.translated_text for e in legacy}
        non_legacy_translations = {e.translated_text for e in non_legacy}

        # Legacy-import disagreeing with itself means a human translator
        # deliberately gave this reused English label different,
        # context-specific translations - a trusted source, never touch.
        if len(legacy_translations) > 1:
            continue

        if len(non_legacy_translations) > 1:
            # AI-sourced entries disagree with each other.
            to_flag = non_legacy
        elif legacy_translations and non_legacy_translations and legacy_translations != non_legacy_translations:
            # AI-sourced entries agree with each other but contradict a
            # unanimous legacy-import translation - trust legacy.
            to_flag = non_legacy
        else:
            continue

        inconsistent_groups += 1
        if len(samples) < 15:
            shown = legacy_translations | non_legacy_translations
            samples.append((original_text[:60], sorted(shown)[:4]))

        for entry in to_flag:
            if not args.dry_run:
                old_translation = entry.translated_text
                stale_note = (
                    f"[Inconsistent-duplicate requeue - source text had multiple "
                    f"disagreeing translations - previous: {old_translation!r}]"
                )
                entry.notes = f"{entry.notes}\n{stale_note}".strip() if entry.notes else stale_note
                entry.revert_to_pending()
            flagged += 1

    print(f"Project: {args.project}")
    print(f"Total entries: {len(project.entries)}")
    print(f"Inconsistent duplicate groups found: {inconsistent_groups}")
    print(f"Entries flagged for re-translation: {flagged}")
    for original, variants in samples:
        print(f"\n  source: {original!r}")
        for v in variants:
            print(f"    -> {v[:60]!r}")

    if args.dry_run:
        print("\nDry run - no changes saved.")
        return

    if flagged:
        project.save(args.project)
        print(f"\nSaved. {flagged} entries reset to PENDING (old translations kept in notes).")
    else:
        print("\nNothing to flag.")


if __name__ == "__main__":
    main()
