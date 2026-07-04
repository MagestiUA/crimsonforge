"""Benchmark: how many localization strings fit in ONE AI batch request.

The only variable is N_LINES below (or pass it as argv[1]). Everything else
stays fixed - provider, model, source strings. Setting `batch_size=N_LINES`
on the engine is what keeps this to exactly one call to the model: as long
as N_LINES stays under the engine's per-chunk token budget
(ai.translation_engine.MAX_CHUNK_TOKENS), all N_LINES strings are grouped
into a single chunk and sent as one JSON-array request - not N calls, not
N/20 calls, exactly 1. A wrapper around the provider counts the real calls
made so you don't have to take that on faith.

Test strings are a small, hand-written, clean sample (cycled to reach
N_LINES) - deliberately NOT pulled from the real 180MB translation project,
so a failure here can't be blamed on some oddball character/encoding
buried in real game data. If this fails at some N, the cause is the
batch size/context/model itself, not the input.

Usage:
    python tools/bench_batch_translate.py [N_LINES]
"""

import logging
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
logging.disable(logging.INFO)  # silence debug/info noise (openai SDK, Ollama startup) - warnings/errors still show

N_LINES = int(sys.argv[1]) if len(sys.argv) > 1 else 20

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai.provider_ollama import OllamaProvider  # noqa: E402
from ai.prompt_manager import PromptManager  # noqa: E402
from ai.translation_engine import TranslationEngine, TranslationRequest  # noqa: E402

MODEL = "gemma4:26b-a4b-it-qat"
SOURCE_LANG = "English"
TARGET_LANG = "Ukrainian"

# Plain, ordinary game-UI-style sentences. Nothing exotic: no placeholders,
# no HTML tags, no unusual punctuation or non-ASCII - just clean English
# text of varying length, so we know exactly what's going into every request.
SAMPLE_LINES = [
    "Inventory",
    "Store",
    "Settings",
    "Exit to Main Menu",
    "Save Game",
    "Load Game",
    "Continue",
    "New Game",
    "Are you sure you want to quit?",
    "Cannot be used at the moment.",
    "How many do you want to move?",
    "Select an item to equip.",
    "Your health is low.",
    "You have gained a new skill.",
    "Press any key to continue.",
    "The merchant has nothing left to sell.",
    "You cannot carry any more items.",
    "Would you like to save your progress before leaving?",
    "This weapon requires a higher strength to wield effectively.",
    "The road ahead splits into two paths, one leading north and one east.",
    "[Hotfix] Explorer search `ext:.dds canta` (and any other field-filter combined with extra terms) now works. The bare-extension shortcut was consuming the whole search string, so `ext:.dds canta` ended up filtering on the literal extension `.dds canta` which no row could match. Shortcut now fires only when the query is the bare extension with no further terms; richer queries route through the parsed evaluator that correctly applies the ext filter AND every other clause.",
    "[Hotfix] Explorer search keystroke latency on the 1.4 M-row file list is back under one frame for complex queries. The first cut of the enterprise parser tokenised every row's path on every keystroke, which made boolean / field / wildcard queries feel sluggish on a fully-loaded archive. The complex-query path now uses C-fast substring + fnmatch checks per clause and never tokenises the path corpus inside the per-row loop. Simple single-token queries still take the v1.24.0 Tier A / Tier B fast path.",
    "[Hotfix] Catalog Browser no longer points at non-existent .pac files for items whose iteminfo prefab hash resolves to an `_index01_n.prefab` / `_index02_n.prefab` / `_n.prefab` descriptor. Previously `core.item_index.build_item_index` and `core.item_catalog.parse_iteminfo_records` synthesised a `.pac` filename by stripping a fixed list of suffixes (`_l _r _u _s _t _index01 _index02 _index03`) from the resolved prefab name and appending `.pac`, with no check that the resulting file actually shipped — so the Demenissian Soldier's Cloth Barding catalog row showed `cd_r0002_00_horse_ub_0019_index01_n.pac` (a fabricated name) instead of the real `cd_r0002_00_horse_ub_0019.pac` + `cd_r0002_00_horse_ub_0019_sub01.pac` files the engine loads. Both modules now strip compound `_index??_n` and bare `_n` suffixes (longest-first), then verify each candidate stem against the live PAMT and only keep stems that exist as a real `.pac` entry. Bumps the catalog parser version so cached pickles rebuild silently on next launch.",
]


def build_test_lines(count: int) -> list[str]:
    return [SAMPLE_LINES[i % len(SAMPLE_LINES)] for i in range(count)]


def main() -> None:
    texts = build_test_lines(N_LINES)
    requests = [
        TranslationRequest(index=i, text=text, key=str(i))
        for i, text in enumerate(texts)
    ]

    provider = OllamaProvider()

    # Count real calls to the model - proves N_LINES only changes what goes
    # INTO each call, not how many calls happen.
    call_count = 0
    real_translate = provider.translate

    def counting_translate(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return real_translate(*args, **kwargs)

    provider.translate = counting_translate

    engine = TranslationEngine(
        provider=provider,
        prompt_manager=PromptManager(),
        batch_size=N_LINES,
        batch_delay_ms=0,
    )

    print(f"Sending {N_LINES} strings, batch_size={N_LINES}, model={MODEL}...", flush=True)
    t0 = time.time()
    results = engine.translate_batch(requests, SOURCE_LANG, TARGET_LANG, model=MODEL)
    elapsed = time.time() - t0

    ok = sum(1 for r in results if r.success)

    print(flush=True)
    print(f"Requested line count : {N_LINES}", flush=True)
    print(f"Actual model calls   : {call_count}"
          + ("  (== 1, confirmed single batch request)" if call_count == 1 else
             "  (WARNING: more than one call - token budget split the chunk, "
             "or a bad response fell back to per-item retries)"), flush=True)
    print(f"Succeeded            : {ok}/{len(results)}", flush=True)
    print(f"Total time           : {elapsed:.1f}s", flush=True)
    print(f"Per-string           : {elapsed / len(results):.2f}s", flush=True)
    print(flush=True)
    for req, r in zip(requests[:5], results[:5]):
        shown = r.translated_text if r.success else f"FAILED: {r.error}"
        print(f"  {req.text[:45]!r} -> {shown[:45]!r}", flush=True)


if __name__ == "__main__":
    main()
