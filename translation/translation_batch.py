"""Batch translation processing with queue, progress, pause, and resume.

Wraps the AI translation engine with project-level state management,
connecting AI results to TranslationEntry state machine updates.
"""

from typing import Optional, Callable

from ai.translation_engine import TranslationEngine, TranslationRequest, BatchState
from ai.provider_base import TranslationResult
from translation import checkpoint_journal
from translation.translation_state import TranslationEntry, StringStatus
from translation.translation_project import TranslationProject
from utils.logger import get_logger

logger = get_logger("translation.batch")


class TranslationBatchProcessor:
    """Processes batch translations, updating project entries with results."""

    def __init__(self, engine: TranslationEngine, project: TranslationProject):
        self._engine = engine
        self._project = project

    def _dedup_and_translate(
        self,
        entries: list[TranslationEntry],
        model: str,
        progress_callback: Optional[Callable[[int, int, Optional[TranslationResult]], None]],
    ) -> list[TranslationResult]:
        """Group entries by identical original_text, translate each unique
        text once, and stamp the result onto every entry that shares it.

        Real localization data repeats heavily - one measured project had
        "Cannot be used at the moment." 1,481 times among ~40k pending
        strings, ~51% overall duplicate rate. Translating unique text once
        instead of once per occurrence roughly halves the real AI calls.
        """
        groups: dict[str, list[TranslationEntry]] = {}
        for entry in entries:
            groups.setdefault(entry.original_text, []).append(entry)

        unique_texts = list(groups.keys())
        requests = [
            TranslationRequest(index=i, text=text, key=groups[text][0].key)
            for i, text in enumerate(unique_texts)
        ]

        total = len(entries)
        resolved = 0
        text_to_result: dict[str, TranslationResult] = {}

        def on_progress(unique_completed: int, unique_total: int, result: TranslationResult):
            nonlocal resolved
            text = unique_texts[unique_completed - 1]
            group = groups[text]
            text_to_result[text] = result
            if result.success:
                for entry in group:
                    entry.set_translated(
                        text=result.translated_text,
                        provider=result.provider,
                        model=result.model_used,
                        tokens=result.total_tokens,
                        cost=result.cost_estimate,
                    )
                    checkpoint_journal.record(entry)
                self._project.mark_modified()
            resolved += len(group)
            if progress_callback:
                progress_callback(resolved, total, result)

        self._engine.translate_batch(
            requests=requests,
            source_lang=self._project.source_lang,
            target_lang=self._project.target_lang,
            model=model,
            progress_callback=on_progress,
        )

        # Expand back to one result per original entry - callers count
        # successes/failures against the real entry count, not the
        # deduplicated call count. Entries whose text never got a result
        # (e.g. the run was stopped early) are simply omitted, matching the
        # old one-request-per-entry behavior on an early stop.
        return [
            text_to_result[e.original_text]
            for e in entries
            if e.original_text in text_to_result
        ]

    def translate_all_pending(
        self,
        model: str = "",
        progress_callback: Optional[Callable[[int, int, Optional[TranslationResult]], None]] = None,
    ) -> list[TranslationResult]:
        """Translate all pending entries in the project.

        Args:
            model: AI model to use.
            progress_callback: callback(completed, total, last_result).

        Returns:
            List of TranslationResult objects.
        """
        pending = self._project.get_pending_entries()
        if not pending:
            logger.info("No pending entries to translate")
            return []
        return self._dedup_and_translate(pending, model, progress_callback)

    def translate_entries(
        self,
        entries: list[TranslationEntry],
        model: str = "",
        progress_callback: Optional[Callable[[int, int, Optional[TranslationResult]], None]] = None,
    ) -> list[TranslationResult]:
        """Translate a specific list of entries (for batch-selected AI translate).

        Args:
            entries: List of TranslationEntry objects to translate.
            model: AI model to use.
            progress_callback: callback(completed, total, last_result).

        Returns:
            List of TranslationResult objects.
        """
        if not entries:
            return []
        return self._dedup_and_translate(entries, model, progress_callback)

    def translate_single_entry(
        self,
        entry: TranslationEntry,
        model: str = "",
        context: str = "",
    ) -> TranslationResult:
        """Translate a single entry using AI."""
        result = self._engine.translate_single(
            text=entry.original_text,
            source_lang=self._project.source_lang,
            target_lang=self._project.target_lang,
            model=model,
            context=context,
        )

        if result.success:
            entry.set_translated(
                text=result.translated_text,
                provider=result.provider,
                model=result.model_used,
                tokens=result.total_tokens,
                cost=result.cost_estimate,
            )
            self._project.mark_modified()

        return result

    def pause(self) -> None:
        self._engine.pause()

    def resume(self) -> None:
        self._engine.resume()

    def stop(self) -> None:
        self._engine.stop()

    @property
    def state(self) -> BatchState:
        return self._engine.state

    @property
    def stats(self):
        return self._engine.stats
