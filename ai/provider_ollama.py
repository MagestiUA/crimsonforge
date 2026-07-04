"""Ollama local provider. OpenAI-compatible API at localhost."""

from ai.provider_base import ConnectionResult
from ai.provider_openai_compat import OpenAICompatProvider
from ai.ollama_manager import ensure_quantized_server

# Per-batch dynamic sizing (pick the smallest of [8192, 16384, 32768, 65536,
# 131072] that fits the current batch) is DISABLED for now - it was
# implicated in empty-response failures at larger batch sizes (e.g. 50
# items), and the auto-sizing math never budgeted for the fact that
# Ukrainian output tokenizes far denser than the English input it was
# measured against. Hardcoded to 64k while this gets re-measured; restore
# the bucket logic (see git history) once a batch-size/context relationship
# is actually confirmed.
FIXED_NUM_CTX = 65536


class OllamaProvider(OpenAICompatProvider):
    name = "Ollama"
    provider_id = "ollama"
    requires_api_key = False

    def __init__(self, api_key: str = "ollama", base_url: str = "http://localhost:11434/v1",
                 timeout: int = 120, max_retries: int = 1):
        super().__init__(api_key, base_url, timeout, max_retries)
        self._num_ctx = FIXED_NUM_CTX

    def _get_default_model(self) -> str:
        return "llama3.2"

    def prepare_for_batch(self, sample_texts: list[str], system_prompt: str = "") -> None:
        """Ensure the server is up before a batch run. Context sizing is
        currently fixed (see FIXED_NUM_CTX) rather than picked per batch.
        """
        ensure_quantized_server(self._base_url)

    def _extra_body(self) -> dict:
        # Translation is the only thing this provider is used for in
        # CrimsonForge, and reasoning tokens only add latency there - always off.
        return {"think": False, "options": {"num_ctx": self._num_ctx}}

    def test_connection(self) -> ConnectionResult:
        ensure_quantized_server(self._base_url)
        return super().test_connection()

    def ensure_ready(self) -> None:
        ensure_quantized_server(self._base_url)
