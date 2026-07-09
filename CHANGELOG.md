# Changelog

🇬🇧 English | [🇺🇦 Українська](CHANGELOG.uk.md)

This file tracks changes made in this fork on top of upstream
[hzeemr/crimsonforge](https://github.com/hzeemr/crimsonforge). The
full historical changelog for the base project (every version back to
1.0) lives in `version.py` and is rendered in the app's **About** tab.

## v1.30.0 — 2026-07-09

### Ship to App fixes

- **Manager ZIP package (CDUMM/DMM/Vortex) now actually works.** It was
  writing raw, unencrypted, unencoded paloc data under a `files/` prefix
  with no game-group folder - no mod manager could apply it. It now
  shares the same PAZ/PAMT/PAPGT repacking logic as the Standalone ZIP
  and writes the patched archives directly under their numbered
  game-group folders at the ZIP root, matching CDUMM's documented
  "Folders -> PAZ/PAMT files" format. Verified against a live DMM
  install after the fix.
- The bundled `README.txt` for Manager ZIP packages now documents the
  real Vortex -> DMM install flow (establish baseline, import, mount)
  plus a manual no-mod-manager install (copy the numbered folders
  straight into the game directory), localized when the project's
  target language is Ukrainian.
- Fixed `generator_url` metadata and README credit links in shipped
  packages (both translation and mesh mods) pointing at the upstream
  repo instead of the fork that actually built them.

## v1.29.0 — 2026-07-05

### Parallel translation for high-concurrency providers

- **New "Parallel Workers" setting** (Settings → Translation, 1-100,
  default 1) splits Auto-Translate-All across N concurrent lanes, each
  with its own cloned provider instance and no artificial pacing
  between lanes. Meant for providers whose rate limiting is dynamic/
  load-based with a generous concurrency ceiling rather than a hard
  per-minute quota - DeepSeek is the motivating case: limits are
  per-account (not per-key - multiple API keys don't multiply
  capacity) and load-based, with a documented concurrency ceiling in
  the hundreds-to-thousands depending on model.
- An earlier version of this built for Gemini's free tier needed a
  strict per-lane pacing scheduler to survive its hard RPM cap, and was
  fully reverted after live testing showed it still broke down under
  load anyway. This version has none of that scheduling complexity -
  each lane just runs as fast as it can and relies on the provider's
  own existing 429/5xx retry handling.
- **Real numbers from a live run:** 50 concurrent lanes against
  DeepSeek translated several thousand pending strings (out of a
  187,526-entry project) in under 10 minutes. Total DeepSeek spend for
  the entire day's testing plus the full production translation run:
  **~$1**.
- **Stop is responsive even with many lanes in flight.** A lane stuck
  in a 429/5xx retry backoff now wakes up immediately instead of
  blocking Stop for up to 30 seconds per lane. Verified live: stopping
  mid-run with 5 active lanes returned in ~3.6s (bounded only by
  already-in-flight HTTP requests finishing, which can't be cancelled
  mid-request).

### Translation quality

- **Cross-language reference context for Ukrainian targets.** English
  alone doesn't mark grammatical gender/case/number - a documented
  weakness in at least one existing community Ukrainian translation
  for this game. When the target language is Ukrainian,
  Auto-Translate-All now reads the game's Korean (original source
  language) and Russian (grammatically close to Ukrainian) text for
  the same line and passes both into the AI prompt as
  disambiguation-only reference: translate from the English source,
  consult Korean when the English text itself seems ambiguous, use
  Russian only to pick correct grammatical agreement - never copy its
  wording. Silently skipped if the game or those languages aren't
  available.

### Data quality tooling

- **New `tools/flag_latin_leak_entries.py`** - finds entries marked
  translated whose translation still contains unprotected Latin-script
  text (names left untranslated before the transliteration prompt fix,
  or a rare batch response that echoed source text back unchanged) and
  requeues them to PENDING with the old translation kept in notes.
  Correctly ignores protected placeholder tokens (including their own
  sentinel spelling, which contains Latin letters) and Roman numerals
  (a legitimate convention for item/blueprint tiers). Verified against
  a real 187k-entry project: an initial naive version flagged 22,291
  entries, almost entirely false positives from the sentinel format
  itself; the corrected version flags 5,625, predominantly genuine
  leaked names on manual review.

## v1.28.0 — 2026-07-04

### Storage

- **SQLite replaces the single-JSON-file project format.** Full save of
  a 187k-entry project drops from ~4.17s to ~1.5s, and the file itself
  shrinks (a real 187k-entry project went from 245MB to 135MB).
  `TranslationProject.persist_entry()` upserts a single row directly
  into the same file during a running batch, so the separate
  `checkpoint_journal.db` from v1.27.0 is no longer needed and has been
  removed - its whole reason to exist (surviving a crash between
  expensive full JSON rewrites) is now solved at the source.
- **Legacy JSON projects migrate automatically and safely.**
  `TranslationProject.load()` detects a pre-1.28 JSON project by
  sniffing for the SQLite magic header, copies it to a
  `.legacy-json-backup` sidecar, builds the new SQLite file at a temp
  path, and atomically swaps it in - an interrupted migration can never
  leave a project file that's neither valid JSON nor valid SQLite.

### Patch target and change detection

- **New "Patch Target (game file)" selector**, independent of Source
  Language. Previously "Patch to Game" always wrote translated text
  back into the same file it read as source - which makes that file
  useless as a future comparison baseline the moment it's patched once,
  since a later sync could read its own prior output back as if it
  were new source text. The selector defaults to whichever discovered
  file matches Destination Language, and leaves nothing preselected
  when the game has no native file for the target language, forcing an
  explicit choice of which existing slot carries the translation
  (mirroring the "Game Language to Replace" picker already used for
  packaged mod exports).
- **Source-text changes now auto-requeue for translation.** An entry
  whose source text changed is reset straight to PENDING (the previous
  translation is stashed in `notes` for reference) instead of being
  left TRANSLATED under an implicit "needs re-review" label - the next
  Auto-Translate-All run picks it up automatically. Locked entries
  (auto-approved placeholders) are left untouched.
- **Character/place names are now transliterated for script-mismatched
  languages** (e.g. Latin -> Cyrillic) instead of being left
  untranslated in the source script, which read as a bug rather than a
  deliberate choice. Same-script language pairs keep the previous
  behavior.

### Data repair

- **New `tools/repair_corrupted_original_text.py`** for projects hit by
  a pre-1.28 bug: once a source file had been patched, a later sync
  could read that patched file back as fresh source text and silently
  overwrite `original_text` with the entry's own prior translation.
  The tool repairs affected entries against the untouched
  `BaselineManager` record (which only ever records a key's value once
  and never overwrites it), touching only `original_text` - existing
  translations and statuses are never discarded or needlessly
  re-queued. Verified against a real 187k-entry project: 150,387
  entries (80%) had this corruption, all repaired cleanly with zero
  ambiguous cases and zero translation/status changes.

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
