"""Batch translation engine with queue, rate limiting, and cost tracking.

Processes translation requests through AI providers with configurable
batch size, concurrency, and rate limiting. Tracks token usage and costs.
"""

import json
import re
import time
import threading
from dataclasses import dataclass, field
from typing import Optional, Callable
from enum import Enum

from ai.provider_base import AIProviderBase, TranslationResult
from ai.prompt_manager import PromptManager
from ai.pricing_registry import calculate_cost
from core.translation_tokenizer import (
    PROMPT_INSTRUCTION,
    decode_after_translation,
    encode_for_translation,
)
from utils.logger import get_logger

logger = get_logger("ai.translation_engine")

# Told to the model whenever a chunk has more than one item: send/receive a
# JSON array instead of one bare string per request. Cuts per-string
# round-trip and system-prompt-reprocessing overhead, which dominates for
# localization strings (median ~7 tokens of actual content).
BATCH_JSON_INSTRUCTION = (
    'You will receive a JSON object: {"items": [{"id": 0, "text": "..."}, '
    '{"id": 1, "text": "..."}, ...]}. Translate the "text" field of every '
    'item independently, following all the rules above. Return ONLY a JSON '
    'object of the same shape: {"items": [{"id": <same id>, "translation": '
    '"..."}, ...]}, exactly one output item per input item. Preserve every '
    'id exactly as given. Do not add, remove, merge, reorder, or skip '
    'items. Output raw JSON only - no markdown code fences, no commentary, '
    'nothing before or after the object.'
)

# Appended to the batch prompt only when at least one item in the chunk
# actually carries a "context" field - cross-referencing another shipped
# translation of the SAME line to disambiguate meaning or grammar the
# source language alone can't express (e.g. English doesn't mark
# grammatical gender, so a Slavic reference translation resolves it).
CROSS_REFERENCE_INSTRUCTION = (
    'Some items include an optional "context" field with reference '
    'translations of that SAME line in other languages, for disambiguation '
    'only - always translate from the item\'s "text" field, never from the '
    'context field. A reference labeled as the original source language '
    'shows the game\'s original text before any translation - use it when '
    '"text" seems ambiguous or context-dependent. A reference labeled '
    '"for grammatical agreement only" is in a language related to the '
    'target language - use it only to pick the correct grammatical gender, '
    'case, or number in your translation when the source text doesn\'t '
    'mark them; never copy its wording, vocabulary, or phrasing.'
)

# Conservative per-chunk token budget so a run of unusually long strings
# can't blow past the fast context buckets (see ai/provider_ollama.py).
# Measured on the live game data: median string ~7 tokens, p99 ~90 tokens,
# max ~440 tokens across 187k entries - this budget comfortably holds many
# dozens of typical items per chunk.
MAX_CHUNK_TOKENS = 6000


class BatchState(Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    COMPLETED = "completed"
    ERROR = "error"


@dataclass
class TranslationStats:
    """Accumulated translation statistics."""
    total_requests: int = 0
    successful: int = 0
    failed: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens: int = 0
    total_cost: float = 0.0
    total_latency_ms: float = 0.0
    avg_latency_ms: float = 0.0


@dataclass
class TranslationRequest:
    """A single translation request in the queue."""
    index: int
    text: str
    key: str = ""
    context: str = ""


class TranslationEngine:
    """Batch translation engine with queue management."""

    def __init__(
        self,
        provider: AIProviderBase,
        prompt_manager: PromptManager,
        batch_size: int = 10,
        batch_delay_ms: int = 500,
    ):
        self._provider = provider
        self._prompt_manager = prompt_manager
        self._batch_size = batch_size
        self._batch_delay_ms = batch_delay_ms
        self._state = BatchState.IDLE
        self._stats = TranslationStats()
        self._lock = threading.Lock()
        self._pause_event = threading.Event()
        self._pause_event.set()
        self._stop_requested = False

    def set_glossary(self, glossary_text: str) -> None:
        """Set glossary text to inject into every AI prompt."""
        self._glossary_text = glossary_text

    def _record_result(self, result: TranslationResult) -> None:
        with self._lock:
            self._stats.total_requests += 1
            if result.success:
                self._stats.successful += 1
            else:
                self._stats.failed += 1
            self._stats.total_input_tokens += result.input_tokens
            self._stats.total_output_tokens += result.output_tokens
            self._stats.total_tokens += result.total_tokens
            self._stats.total_cost += result.cost_estimate
            self._stats.total_latency_ms += result.latency_ms
            if self._stats.successful > 0:
                self._stats.avg_latency_ms = (
                    self._stats.total_latency_ms / self._stats.successful
                )

    def _translate_one_raw(
        self, text: str, source_lang: str, target_lang: str,
        model: str, context: str, system_prompt: str,
    ) -> TranslationResult:
        """Placeholder-protected single-string translation, no stats bookkeeping.

        Shared by :meth:`translate_single` (public single-entry API) and the
        batch path's per-item fallback when a batched chunk can't be parsed.
        """
        encoded_text, token_table = encode_for_translation(text)

        result = self._provider.translate(
            text=encoded_text,
            source_lang=source_lang,
            target_lang=target_lang,
            model=model,
            system_prompt=system_prompt,
            context=context,
        )
        if result.success and result.translated_text:
            result = result.__class__(**{
                **result.__dict__,
                "translated_text": decode_after_translation(
                    result.translated_text, token_table,
                ),
            })
        return result

    def translate_single(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
        model: str = "",
        context: str = "",
    ) -> TranslationResult:
        """Translate a single string.

        Protected-token pipeline
        ------------------------
        Pearl Abyss paloc strings contain non-prose placeholders
        (``<br/>``, ``[EMPTY]``, ``%0``, ``{Key:...}``,
        ``{Staticinfo:Knowledge:...#Korean_Label}``, etc.) that
        MUST survive translation byte-for-byte. We round-trip
        them through opaque sentinels so the AI can't mangle them:

          1. Encode every protected token as ``⟦CFn⟧`` (or a
             paired ``⟦CFn⟧label⟦/CFn⟧`` for hash-label braces
             that contain a translatable Korean label).
          2. Append a short preservation instruction to the
             system prompt.
          3. Send the encoded text + augmented prompt to the AI.
          4. Decode the returned sentinels back to the original
             tokens (for paired sentinels, the AI-translated
             label stays put and we splice it back into the
             namespace#label frame).

        The result object stores the FINAL decoded translation,
        so downstream consumers never see a sentinel.
        """
        self._provider.reset_stop()
        glossary = getattr(self, "_glossary_text", "")
        system_prompt = self._prompt_manager.get_system_prompt(
            source_lang, target_lang, glossary_text=glossary
        )
        system_prompt = system_prompt + "\n\n" + PROMPT_INSTRUCTION

        result = self._translate_one_raw(
            text, source_lang, target_lang, model, context, system_prompt,
        )
        self._record_result(result)
        return result

    def _build_chunks(self, requests: list[TranslationRequest]) -> list[list[int]]:
        """Group request indices into chunks bounded by item count and token budget."""
        max_items = max(1, self._batch_size)
        chunks: list[list[int]] = []
        current: list[int] = []
        current_tokens = 0
        for i, req in enumerate(requests):
            item_tokens = len(req.text) // 3 + 20  # +20 covers JSON id/braces/quotes overhead
            if current and (
                len(current) >= max_items
                or current_tokens + item_tokens > MAX_CHUNK_TOKENS
            ):
                chunks.append(current)
                current = []
                current_tokens = 0
            current.append(i)
            current_tokens += item_tokens
        if current:
            chunks.append(current)
        return chunks

    def _parse_batch_response(self, raw: str, expected_count: int) -> Optional[list[str]]:
        """Parse the model's JSON batch response into translations ordered by id.

        Returns None if the response can't be trusted: broken JSON, wrong
        item count, duplicate/missing ids. Callers must fall back to
        translating the chunk one item at a time when this happens.
        """
        text = raw.strip()
        fence_match = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
        if fence_match:
            text = fence_match.group(1).strip()

        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return None

        if isinstance(data, dict):
            data = data.get("items")

        if not isinstance(data, list) or len(data) != expected_count:
            return None

        by_id: dict[int, str] = {}
        for item in data:
            if not isinstance(item, dict) or "id" not in item or "translation" not in item:
                return None
            try:
                item_id = int(item["id"])
            except (TypeError, ValueError):
                return None
            if item_id in by_id:
                return None
            by_id[item_id] = str(item["translation"])

        if set(by_id.keys()) != set(range(expected_count)):
            return None
        return [by_id[i] for i in range(expected_count)]

    def _translate_individually(
        self, chunk_requests: list[TranslationRequest],
        source_lang: str, target_lang: str, model: str, system_prompt: str,
    ) -> list[TranslationResult]:
        return [
            self._translate_one_raw(
                req.text, source_lang, target_lang, model, req.context, system_prompt,
            )
            for req in chunk_requests
        ]

    def _translate_bisected(
        self,
        chunk_requests: list[TranslationRequest],
        source_lang: str,
        target_lang: str,
        model: str,
        single_system_prompt: str,
        batch_system_prompt: str,
    ) -> list[TranslationResult]:
        """Split a failed chunk in half and translate each half through
        _translate_chunk again - recurses down to single items (which use
        _translate_individually) only for the half(s) that keep failing."""
        mid = len(chunk_requests) // 2
        left, right = chunk_requests[:mid], chunk_requests[mid:]
        return (
            self._translate_chunk(
                left, source_lang, target_lang, model,
                single_system_prompt, batch_system_prompt,
            )
            + self._translate_chunk(
                right, source_lang, target_lang, model,
                single_system_prompt, batch_system_prompt,
            )
        )

    def _translate_chunk(
        self,
        chunk_requests: list[TranslationRequest],
        source_lang: str,
        target_lang: str,
        model: str,
        single_system_prompt: str,
        batch_system_prompt: str,
    ) -> list[TranslationResult]:
        """Translate a chunk in one LLM call. On any sign the response can't
        be trusted, bisect instead of immediately falling back to
        one-at-a-time: usually only one item in a large chunk is actually
        the problem, and splitting isolates it in ~log2(n) extra calls
        instead of paying for n individual calls every time."""
        if self._stop_requested:
            return [
                TranslationResult(
                    translated_text="",
                    source_text=req.text,
                    source_lang=source_lang,
                    target_lang=target_lang,
                    model_used=model,
                    provider=self._provider.provider_id,
                    error="Translation stopped by user",
                    success=False,
                )
                for req in chunk_requests
            ]
        if len(chunk_requests) == 1:
            return self._translate_individually(
                chunk_requests, source_lang, target_lang, model, single_system_prompt,
            )

        encoded_items = [encode_for_translation(req.text) for req in chunk_requests]
        items_payload = []
        has_context = False
        for i, req in enumerate(chunk_requests):
            item = {"id": i, "text": encoded_items[i][0]}
            if req.context:
                item["context"] = req.context
                has_context = True
            items_payload.append(item)
        payload = json.dumps({"items": items_payload}, ensure_ascii=False)

        effective_batch_prompt = batch_system_prompt
        if has_context:
            effective_batch_prompt = batch_system_prompt + "\n\n" + CROSS_REFERENCE_INSTRUCTION

        batch_result = self._provider.translate(
            text=payload,
            source_lang=source_lang,
            target_lang=target_lang,
            model=model,
            system_prompt=effective_batch_prompt,
            context="",
        )

        if not batch_result.success:
            logger.warning(
                "Batch of %d failed (%s) - splitting in half and retrying. "
                "Raw payload sent:\n%s",
                len(chunk_requests), batch_result.error, payload,
            )
            return self._translate_bisected(
                chunk_requests, source_lang, target_lang, model,
                single_system_prompt, batch_system_prompt,
            )

        translations = self._parse_batch_response(
            batch_result.translated_text, len(chunk_requests),
        )
        if translations is None:
            logger.warning(
                "Batch of %d returned an unparseable/mismatched response - "
                "splitting in half and retrying.\n"
                "--- payload sent ---\n%s\n"
                "--- raw model response (%d chars) ---\n%s\n"
                "--- end raw response ---",
                len(chunk_requests), payload,
                len(batch_result.translated_text), batch_result.translated_text,
            )
            return self._translate_bisected(
                chunk_requests, source_lang, target_lang, model,
                single_system_prompt, batch_system_prompt,
            )

        # The API reports one usage total for the whole chunk - distribute
        # it across items proportionally to input length so per-entry
        # numbers look reasonable while the chunk sum stays exact.
        total_chars = sum(len(t) for t, _ in encoded_items) or 1
        results = []
        for i, req in enumerate(chunk_requests):
            encoded_text, token_table = encoded_items[i]
            share = len(encoded_text) / total_chars
            in_tok = round(batch_result.input_tokens * share)
            out_tok = round(batch_result.output_tokens * share)
            results.append(TranslationResult(
                translated_text=decode_after_translation(translations[i], token_table),
                source_text=req.text,
                source_lang=source_lang,
                target_lang=target_lang,
                model_used=batch_result.model_used,
                provider=batch_result.provider,
                input_tokens=in_tok,
                output_tokens=out_tok,
                total_tokens=in_tok + out_tok,
                cost_estimate=calculate_cost(
                    batch_result.provider, batch_result.model_used, in_tok, out_tok,
                ),
                latency_ms=batch_result.latency_ms / len(chunk_requests),
                success=True,
            ))
        return results

    def translate_batch(
        self,
        requests: list[TranslationRequest],
        source_lang: str,
        target_lang: str,
        model: str = "",
        progress_callback: Optional[Callable[[int, int, TranslationResult], None]] = None,
    ) -> list[TranslationResult]:
        """Translate a batch of strings with progress reporting.

        Groups requests into chunks (bounded by the configured batch size
        and a token-budget safety cap) and translates each chunk with a
        single JSON-array LLM call instead of one call per string - this is
        where the actual speedup over one-string-per-request comes from.
        Any chunk whose response can't be parsed/trusted falls back to
        translating that chunk's items one at a time, so a bad batch never
        loses or corrupts a translation - it's just slower for that chunk.

        Args:
            requests: List of translation requests.
            source_lang: Source language.
            target_lang: Target language.
            model: Model to use.
            progress_callback: Optional callback(completed, total, last_result),
                called once per item (not once per chunk).

        Returns:
            List of TranslationResult, one per request, in request order.
        """
        self._state = BatchState.RUNNING
        self._stop_requested = False
        self._provider.reset_stop()
        total = len(requests)
        if total == 0:
            self._state = BatchState.COMPLETED
            return []

        glossary = getattr(self, "_glossary_text", "")
        single_system_prompt = self._prompt_manager.get_system_prompt(
            source_lang, target_lang, glossary_text=glossary
        ) + "\n\n" + PROMPT_INSTRUCTION
        batch_system_prompt = single_system_prompt + "\n\n" + BATCH_JSON_INSTRUCTION

        chunks = self._build_chunks(requests)
        self._provider.prepare_for_batch(
            ["".join(requests[i].text for i in idxs) for idxs in chunks],
            batch_system_prompt,
        )

        ordered_results: list[Optional[TranslationResult]] = [None] * total
        completed = 0

        for chunk_pos, chunk_indices in enumerate(chunks):
            self._pause_event.wait()

            if self._stop_requested:
                self._state = BatchState.IDLE
                break

            chunk_results = self._translate_chunk(
                [requests[i] for i in chunk_indices],
                source_lang, target_lang, model,
                single_system_prompt, batch_system_prompt,
            )

            for local_i, req_i in enumerate(chunk_indices):
                result = chunk_results[local_i]
                ordered_results[req_i] = result
                self._record_result(result)
                completed += 1
                if progress_callback:
                    progress_callback(completed, total, result)

            if chunk_pos < len(chunks) - 1 and self._batch_delay_ms > 0:
                time.sleep(self._batch_delay_ms / 1000.0)

        if not self._stop_requested:
            self._state = BatchState.COMPLETED

        return [r for r in ordered_results if r is not None]

    def pause(self) -> None:
        """Pause batch translation."""
        self._pause_event.clear()
        self._state = BatchState.PAUSED
        logger.info("Translation paused")

    def resume(self) -> None:
        """Resume batch translation."""
        self._pause_event.set()
        self._state = BatchState.RUNNING
        logger.info("Translation resumed")

    def stop(self) -> None:
        """Stop batch translation."""
        self._stop_requested = True
        self._pause_event.set()
        self._state = BatchState.STOPPING
        # Wakes up any in-flight retry backoff sleep inside the provider
        # instead of leaving it to sleep out its full remaining delay.
        self._provider.request_stop()
        logger.info("Translation stop requested")

    @property
    def state(self) -> BatchState:
        return self._state

    @property
    def stats(self) -> TranslationStats:
        with self._lock:
            return TranslationStats(
                total_requests=self._stats.total_requests,
                successful=self._stats.successful,
                failed=self._stats.failed,
                total_input_tokens=self._stats.total_input_tokens,
                total_output_tokens=self._stats.total_output_tokens,
                total_tokens=self._stats.total_tokens,
                total_cost=self._stats.total_cost,
                total_latency_ms=self._stats.total_latency_ms,
                avg_latency_ms=self._stats.avg_latency_ms,
            )

    def reset_stats(self) -> None:
        with self._lock:
            self._stats = TranslationStats()

    def set_provider(self, provider: AIProviderBase) -> None:
        self._provider = provider

    def set_batch_config(self, batch_size: int = 0, batch_delay_ms: int = 0) -> None:
        if batch_size > 0:
            self._batch_size = batch_size
        if batch_delay_ms >= 0:
            self._batch_delay_ms = batch_delay_ms
