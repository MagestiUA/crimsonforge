# Changelog

🇬🇧 English | [🇺🇦 Українська](CHANGELOG.uk.md)

This file tracks changes made in this fork on top of upstream
[hzeemr/crimsonforge](https://github.com/hzeemr/crimsonforge). The
full historical changelog for the base project (every version back to
1.0) lives in `version.py` and is rendered in the app's **About** tab.

## v1.27.0 — 2026-07-04

### Translation reliability

- **Crash-safe checkpoint journal.** Every translated entry is now
  upserted into a small SQLite journal (`translation/checkpoint_journal.py`,
  ~35ms per write) instead of rewriting the entire project JSON on every
  autosave tick (4.17s measured on a 187k-entry / 165MB project). The
  journal is merged back into the project on load, so an interrupted
  translation run never loses entries that were already translated.
- **Fixed a save race condition** (`WinError 32`) between the periodic
  autosave timer and per-item checkpoints by having `TranslationProject.save()`
  hold a lock for its entire write.
- **Duplicate-aware batch translation.** Pending entries are grouped by
  identical source text before sending anything to the AI; each unique
  string is translated once and the result is stamped onto every entry
  that shares it. One measured project had a single string repeated
  1,481 times among ~40k pending entries (~51% overall duplicate rate) —
  this roughly halves real AI calls on typical localization data.
- **JSON-array batch requests.** Instead of one AI call per string, the
  engine groups many strings into a single JSON-array request/response
  per batch. A batch whose response can't be trusted (broken JSON, wrong
  item count, duplicate/missing ids) is bisected and retried in halves
  rather than immediately falling back to one call per item — isolates
  the one bad item in ~log2(n) calls instead of n calls every time.
- **Provider-aware retry handling.** Gemini and every OpenAI-compatible
  provider (DeepSeek, Ollama, Mistral, custom) now retry HTTP 429 with a
  real cooldown (free-tier RPM quotas reset per minute, so a rate limit
  is a pacing problem, not broken content) and retry 5xx with a short
  backoff (the backend's fault, not the request's). An empty AI response
  is now treated as a failed translation instead of silently accepted.
- **Gemini/Gemma fixes:** `list_models()` now filters by the
  `generateContent` capability instead of a `"gemini"` name match, which
  was hiding Gemma models available on the same API key. Stopped
  capping `max_output_tokens` on Gemini requests — Gemma reasoning
  models spend part of that budget on internal "thinking" that can't be
  sized in advance, and setting an explicit `thinking_budget` is
  rejected outright for these models. Disabled the SDK's own internal
  HTTP retry, which was retrying the same status codes our own logic
  already handles and could silently add minutes of delay when stacked
  with it.

### Local model (Ollama) resilience

- CrimsonForge now force-restarts any Ollama server it didn't start
  itself before a translation batch, so KV-cache quantization (`q4_0`)
  and flash attention are always applied — a server already running
  unquantized otherwise ignores those settings for its entire lifetime.
- Fixed an orphaned-VRAM bug where stopping only `ollama.exe` left
  `llama-server.exe` (the process actually holding the model in GPU
  memory) running.
- New `prepare_for_batch()` / `ensure_ready()` provider hooks let a
  provider verify or restart its backend before a batch run and after a
  long pause.

### Tooling

- `tools/import_legacy_translations.py` — one-off migration of a
  Ukrainian translation memory from an older, separate SQLite-based
  translation tool into a CrimsonForge project. Matches by the game's
  current localization key; entries whose English source is unchanged
  are imported as-is, entries whose source changed are left pending
  with the old translation stashed in notes for reference.
- `tools/bench_batch_translate.py` — benchmarks batch-size / latency /
  fallback-rate tradeoffs against a live AI provider.
