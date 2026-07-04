# CrimsonForge

🇬🇧 English | [🇺🇦 Українська](README.uk.md)

CrimsonForge is a complete modding studio for **Crimson Desert**.

It lets you browse game archives, translate localization with AI, inspect and replace meshes, replace audio, generate dialogue voices, patch fonts, and write everything back with valid checksums so the game still launches normally.

CrimsonForge handles the full pipeline:

`decrypt (ChaCha20) -> decompress (LZ4) -> parse -> modify -> recompress -> re-encrypt -> update PAMT -> update PAPGT -> ready to play`

## About This Fork

This is an actively maintained continuation of [hzeemr/crimsonforge](https://github.com/hzeemr/crimsonforge) — a genuinely excellent foundation built from scratch by the original author, including deep reverse-engineering of the PAC/PAB/PAA mesh and animation formats, the skeleton/rigging pipeline, and the full archive/checksum chain. Upstream development paused after `v1.26.0` (last commit 2026-05-12); this fork continues active work on top of it, starting with the translation pipeline.

**What this fork adds on top of `v1.26.0`** (full detail in [CHANGELOG.md](CHANGELOG.md)):

- SQLite-backed translation project storage — faster full saves, crash-safe incremental writes during a batch, automatic migration from the old JSON format
- Reliable AI batch translation: JSON-array batching with bisection retry on untrustworthy responses, per-provider 429/5xx handling tuned for real rate limits, duplicate-aware translation that roughly halves AI calls on typical localization data
- Ollama quantization enforcement, so local translation actually runs at the speed it's configured for instead of silently falling back to an unquantized server
- Independent Patch Target selection — translate from one game language and patch the result into a different one, so a language the game doesn't ship natively (e.g. Ukrainian) can still get a fully native-feeling in-game slot
- Automatic re-queueing when the game's source text changes, instead of a stale translation silently sitting there
- Script-aware name transliteration (e.g. Latin → Cyrillic) instead of leaving names untranslated in the wrong alphabet
- A data-repair tool for projects affected by a historical source-text corruption bug

If you're looking for the original project and its full reverse-engineering history, go to [hzeemr/crimsonforge](https://github.com/hzeemr/crimsonforge) — all of that foundational work is still there, and this fork wouldn't exist without it.

**Current version:** `1.28.0` — see [CHANGELOG.md](CHANGELOG.md) for this fork's changes, or the in-app **About** tab / `version.py` for the full history back to `v1.0`.

## Main Features

### Archive Explorer

- Browse more than **1.4 million** files across the game packages with fast filtering
- Preview textures, meshes, audio, text, fonts, and web/UI files
- Built-in editor for CSS, HTML, XML, JSON, and localization data
- Extract files with automatic decryption and decompression
- Right-click workflows for mesh export/import, audio export/import, and patch-to-game

### Mesh Modding

- Preview and export `.pac`, `.pam`, and `.pamlod` meshes
- Export meshes to **OBJ** and **FBX**
- Import edited OBJ files back into the game
- Supports real round-trip workflows for static meshes and PAC weapon/character mesh editing
- One-click **Import OBJ + Patch to Game**
- Matching item-name search in Explorer to find the exact live asset more easily

### Audio Modding

- Browse, search, play, export, and replace game audio
- Linked dialogue text for a large portion of voice assets
- Export to WAV/OGG
- Import WAV and patch back into the game
- Wwise-assisted WEM rebuild support

### AI Text-to-Speech

- Generate replacement voices for dialogue lines
- Supports multiple TTS providers including Edge TTS, OpenAI, ElevenLabs, Google Cloud, Azure Speech, and Mistral Voxtral
- Generate and patch audio in one workflow

### Translation Workspace

- Parse and work with more than **172,000** localization entries
- SQLite-backed project storage — fast full saves, crash-safe incremental writes during a batch, automatic migration from older JSON projects
- AI batch translation with multiple providers
- JSON-array batch requests (many strings per call) with automatic bisection retry when a batch response can't be trusted
- Duplicate-aware translation — identical source strings are translated once and stamped across every occurrence
- Optional parallel translation (1-100 concurrent lanes) for providers with a generous, load-based concurrency ceiling instead of a hard per-minute quota — e.g. 50 lanes against DeepSeek translated several thousand strings in under 10 minutes for about $1 total
- Automatic 429/5xx handling per provider (rate-limit cooldown vs transient-error retry, tuned separately), with a Stop button that stays responsive even mid-retry across many concurrent lanes
- Cross-language reference context for Ukrainian targets — consults the game's Korean (original) and Russian (grammatically close) text for the same line to help the AI disambiguate meaning and pick correct grammatical gender/case, without copying either language's wording
- Prompt system designed to preserve placeholders, tags, and game terminology; transliterates names into the target script when it differs from the source
- Independent Patch Target selection — translate from one game language and patch the result into a different one (useful when the game has no native file for your target language)
- Glossary support
- Autosave and session recovery
- Game update detection and merge behavior for changed localization data, including auto-requeueing entries whose source text changed

### Font Builder

- Extract game fonts
- Analyze missing glyph coverage for target languages
- Pull needed glyphs from donor fonts
- Patch updated fonts back into the game

### Ship to App

- Generate end-user install packages for translators and mod teams
- Build plug-and-play packages with `install.bat` and `uninstall.bat`
- End users do not need Python or modding tools

### Patch to Game

- Automatic backup before patching
- Rebuilds archives and checksum chains
- Handles patched or partially modified installs more safely than older tools

## Installation

### Option 1: Standalone build

If you just want to run CrimsonForge, use the standalone executable from the **GitHub Releases** page.

- Download `CrimsonForge.exe`
- Run it directly
- No separate Python install is required

### Option 2: Run from source

Requirements:

- Python `3.12+`
- Crimson Desert installed via Steam
- At least one API key if you want AI translation or TTS

Steps:

```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

## End User Translation Mods

CrimsonForge also supports a translator-to-player workflow.

A translator can generate a packaged mod release, and the end user only needs to:

1. Download the translator's ZIP package
2. Extract it anywhere
3. Double-click `install.bat`
4. Launch Crimson Desert

For removal, the generated package also supports uninstall via Steam file verification workflows.

## AI Providers

### Translation

- OpenAI
- Anthropic
- Google Gemini
- DeepSeek
- DeepL
- Mistral
- Cohere
- Ollama
- vLLM / custom-compatible endpoints

### Text-to-Speech

- Edge TTS
- OpenAI TTS
- ElevenLabs
- Google Cloud TTS
- Azure Speech
- Mistral Voxtral

## Building the Standalone EXE

This project includes a PyInstaller spec for a bundled Windows build:

```powershell
python -m PyInstaller --clean --noconfirm CrimsonForge.spec
```

Output:

- `dist/CrimsonForge.exe`

The build spec bundles:

- the full `data` directory
- required runtime resources
- `core/pa_checksum.dll`

## Credits

- **hzeem** - CrimsonForge author and maintainer
- **Lazorr / lazorr410** - foundational research into Pearl Abyss archive formats
- **MrIkso** - early archive and checksum tooling references
- **Altair200333** - `crimson-desert-model-browser`, helpful PAC reference and validation project

## License

This project is released under the **MIT License**.

