"""Concurrent multi-lane batch translation for high-concurrency providers.

Splits pending translation work across N parallel lane threads, each with
its own cloned provider instance and its own TranslationEngine. There is
no artificial pacing or staggered lane start here - this is deliberately
simple, meant for providers whose rate limiting is dynamic/load-based
with a generous concurrency ceiling rather than a hard per-minute quota.
DeepSeek is the motivating case: rate limiting is applied per-account
(not per-key, so multiple API keys wouldn't help) and is load-based
rather than a fixed RPM cap, with a documented concurrency ceiling in the
hundreds-to-thousands depending on model - far above what a handful of
lanes need. Each lane just runs as fast as it can; the provider's own
existing 429 (cooldown) / 5xx (short retry) handling already absorbs any
real throttling without a separate scheduler here.

An earlier version of this module added a strict per-lane "1 request per
minute" pace and staggered lane starts specifically to survive Gemini's
free-tier hard RPM cap - that mechanism doesn't belong here and was
dropped; if a provider genuinely needs it, it belongs on that provider,
not baked into the orchestrator every provider has to pay for.
"""

import threading
from typing import Optional, Callable

from ai.provider_base import AIProviderBase, TranslationResult
from ai.prompt_manager import PromptManager
from ai.translation_engine import BatchState, TranslationEngine, TranslationRequest, TranslationStats
from translation.translation_state import TranslationEntry
from translation.translation_project import TranslationProject
from utils.logger import get_logger

logger = get_logger("translation.concurrent_batch")


def _clone_provider(provider: AIProviderBase) -> AIProviderBase:
    """Construct a fresh, independent instance of the same provider class
    with the same connection settings - each lane needs its own HTTP
    client/internal state rather than sharing one across threads."""
    return type(provider)(
        api_key=provider.api_key,
        base_url=provider.base_url,
        timeout=provider.timeout,
        max_retries=provider.max_retries,
    )


class ConcurrentTranslationBatchProcessor:
    """Translates all pending entries by splitting them across N parallel
    lanes. Matches TranslationBatchProcessor's pause/resume/stop/state/
    stats interface so the UI's existing controls work unchanged."""

    def __init__(
        self,
        provider: AIProviderBase,
        prompt_manager: PromptManager,
        project: TranslationProject,
        num_lanes: int = 5,
        batch_size: int = 20,
    ):
        self._provider = provider
        self._prompt_manager = prompt_manager
        self._project = project
        self._num_lanes = max(1, num_lanes)
        self._batch_size = batch_size
        self._glossary_text = ""
        self._reference_languages: list[tuple[str, str, dict[str, str]]] = []
        self._stop_requested = False
        self._paused = False
        self._state = BatchState.IDLE
        self._lane_engines: list[TranslationEngine] = []
        self._lane_engines_lock = threading.Lock()

    def set_glossary(self, glossary_text: str) -> None:
        self._glossary_text = glossary_text

    def set_reference_languages(
        self, reference_languages: list[tuple[str, str, dict[str, str]]],
    ) -> None:
        self._reference_languages = reference_languages

    def _build_context(self, key: str) -> str:
        if not self._reference_languages:
            return ""
        parts = []
        for label, purpose, key_to_text in self._reference_languages:
            text = key_to_text.get(key)
            if not text:
                continue
            if purpose == "original":
                parts.append(f"{label} (original source language): {text}")
            elif purpose == "grammar":
                parts.append(f"{label} (for grammatical agreement only): {text}")
        return "\n".join(parts)

    def translate_all_pending(
        self,
        model: str = "",
        progress_callback: Optional[Callable[[int, int, Optional[TranslationResult]], None]] = None,
    ) -> list[TranslationResult]:
        pending = self._project.get_pending_entries()
        if not pending:
            logger.info("No pending entries to translate")
            return []

        self._state = BatchState.RUNNING
        self._stop_requested = False

        groups: dict[str, list[TranslationEntry]] = {}
        for entry in pending:
            groups.setdefault(entry.original_text, []).append(entry)
        unique_texts = list(groups.keys())

        num_lanes = min(self._num_lanes, len(unique_texts)) or 1
        lanes: list[list[str]] = [[] for _ in range(num_lanes)]
        for i, text in enumerate(unique_texts):
            lanes[i % num_lanes].append(text)

        total_entries = len(pending)
        completed_entries = 0
        results: list[TranslationResult] = []
        progress_lock = threading.Lock()

        logger.info(
            "Starting concurrent translation: %d unique texts across %d lanes "
            "(%d total entries incl. duplicates)",
            len(unique_texts), num_lanes, total_entries,
        )

        def on_lane_result(text: str, result: TranslationResult) -> None:
            nonlocal completed_entries
            group = groups[text]
            if result.success:
                for entry in group:
                    entry.set_translated(
                        text=result.translated_text,
                        provider=result.provider,
                        model=result.model_used,
                        tokens=result.total_tokens,
                        cost=result.cost_estimate,
                    )
                    self._project.persist_entry(entry)
                self._project.mark_modified()
            with progress_lock:
                completed_entries += len(group)
                results.append(result)
                if progress_callback:
                    progress_callback(completed_entries, total_entries, result)

        def run_lane(lane_index: int, lane_texts: list[str]) -> None:
            if self._stop_requested or not lane_texts:
                return

            lane_provider = _clone_provider(self._provider)
            lane_engine = TranslationEngine(
                provider=lane_provider,
                prompt_manager=self._prompt_manager,
                batch_size=self._batch_size,
                batch_delay_ms=0,
            )
            lane_engine.set_glossary(self._glossary_text)
            if self._paused:
                lane_engine.pause()

            with self._lane_engines_lock:
                self._lane_engines.append(lane_engine)

            if self._stop_requested:
                return

            requests = [
                TranslationRequest(
                    index=i, text=text, key=groups[text][0].key,
                    context=self._build_context(groups[text][0].key),
                )
                for i, text in enumerate(lane_texts)
            ]

            def lane_progress(completed: int, total: int, result: TranslationResult):
                on_lane_result(lane_texts[completed - 1], result)

            lane_engine.translate_batch(
                requests=requests,
                source_lang=self._project.source_lang,
                target_lang=self._project.target_lang,
                model=model,
                progress_callback=lane_progress,
            )

        threads = [
            threading.Thread(target=run_lane, args=(i, lanes[i]), daemon=True, name=f"translate-lane-{i}")
            for i in range(num_lanes)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        if not self._stop_requested:
            self._state = BatchState.COMPLETED
        logger.info(
            "Concurrent translation finished: %d/%d entries resolved",
            completed_entries, total_entries,
        )
        return results

    def pause(self) -> None:
        self._paused = True
        self._state = BatchState.PAUSED
        with self._lane_engines_lock:
            for engine in self._lane_engines:
                engine.pause()
        logger.info("Concurrent translation paused (%d active lanes)", len(self._lane_engines))

    def resume(self) -> None:
        self._paused = False
        self._state = BatchState.RUNNING
        with self._lane_engines_lock:
            for engine in self._lane_engines:
                engine.resume()
        logger.info("Concurrent translation resumed")

    def stop(self) -> None:
        self._stop_requested = True
        self._state = BatchState.STOPPING
        with self._lane_engines_lock:
            for engine in self._lane_engines:
                engine.stop()
        logger.info("Concurrent translation stop requested")

    @property
    def state(self) -> BatchState:
        return self._state

    @property
    def stats(self) -> TranslationStats:
        combined = TranslationStats()
        with self._lane_engines_lock:
            engines = list(self._lane_engines)
        for engine in engines:
            s = engine.stats
            combined.total_requests += s.total_requests
            combined.successful += s.successful
            combined.failed += s.failed
            combined.total_input_tokens += s.total_input_tokens
            combined.total_output_tokens += s.total_output_tokens
            combined.total_tokens += s.total_tokens
            combined.total_cost += s.total_cost
            combined.total_latency_ms += s.total_latency_ms
        if combined.successful > 0:
            combined.avg_latency_ms = combined.total_latency_ms / combined.successful
        return combined
