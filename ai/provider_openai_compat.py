"""Base class for OpenAI-compatible providers.

DeepSeek, Ollama, vLLM, Mistral, and Custom providers all use
the OpenAI SDK with different base URLs. This base class eliminates
duplication while letting each provider customize its behavior.
"""

import time
from openai import OpenAI, RateLimitError, InternalServerError

from ai.provider_base import (
    AIProviderBase, ModelInfo, TranslationResult, ConnectionResult,
)
from ai.pricing_registry import calculate_cost
from utils.logger import get_logger

logger = get_logger("ai.openai_compat")

# A 429 means "wrong pace", not "bad content" - retry after a real cooldown
# instead of letting translation_engine's bisection treat it as a broken
# batch. Free-tier quotas (e.g. Google AI Studio's 15 RPM) reset per minute,
# so a short exponential backoff isn't enough; wait long enough to clear the
# window, and cap retries so a genuinely dead key still fails eventually.
RATE_LIMIT_WAIT_S = 30
RATE_LIMIT_MAX_RETRIES = 6

# 5xx is the backend's fault, not the request's - a short retry catches
# transient blips without burning a batch (and its share of a free-tier
# daily quota) on translation_engine's bisection over a one-off hiccup.
SERVER_ERROR_WAIT_S = 5
SERVER_ERROR_MAX_RETRIES = 3


class OpenAICompatProvider(AIProviderBase):
    """Base for providers that expose an OpenAI-compatible API."""

    supports_openai_compat = True

    def __init__(self, api_key: str = "", base_url: str = "",
                 timeout: int = 60, max_retries: int = 3):
        super().__init__(api_key, base_url, timeout, max_retries)
        self._client = None

    def _get_client(self) -> OpenAI:
        if self._client is None:
            self._client = OpenAI(
                api_key=self._api_key or "not-needed",
                base_url=self._base_url,
                timeout=self._timeout,
                max_retries=self._max_retries,
            )
        return self._client

    def list_models(self) -> list[ModelInfo]:
        try:
            client = self._get_client()
            response = client.models.list()
            models = []
            for m in response.data:
                models.append(ModelInfo(
                    model_id=m.id,
                    name=m.id,
                    provider=self.provider_id,
                ))
            models.sort(key=lambda x: x.model_id)
            return models
        except Exception as e:
            logger.error("Failed to list %s models: %s", self.name, e)
            raise ConnectionError(
                f"Failed to list {self.name} models: {e}. "
                f"Check the API key and base URL in settings."
            ) from e

    def translate(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
        model: str = "",
        system_prompt: str = "",
        context: str = "",
    ) -> TranslationResult:
        if not model:
            model = self._get_default_model()

        if not system_prompt:
            from ai.default_prompt import get_default_system_prompt
            system_prompt = get_default_system_prompt(source_lang, target_lang)

        messages = [{"role": "system", "content": system_prompt}]
        if context:
            messages.append({"role": "user", "content": f"Context: {context}"})
        messages.append({"role": "user", "content": text})

        start_time = time.time()
        rate_limit_retries = 0
        server_error_retries = 0

        def _stopped_result() -> TranslationResult:
            return TranslationResult(
                translated_text="",
                source_text=text,
                source_lang=source_lang,
                target_lang=target_lang,
                model_used=model,
                provider=self.provider_id,
                latency_ms=(time.time() - start_time) * 1000,
                error="Translation stopped by user",
                success=False,
            )

        while True:
            if self._stopped():
                return _stopped_result()
            try:
                client = self._get_client()
                extra_body = self._extra_body() if hasattr(self, "_extra_body") else None
                create_kwargs = {}
                if extra_body:
                    create_kwargs["extra_body"] = extra_body
                response = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=0.3,
                    **create_kwargs,
                )
                latency = (time.time() - start_time) * 1000

                content = response.choices[0].message.content
                if not content or not content.strip():
                    logger.warning(
                        "%s returned an empty response for: %r", self.name, text[:80],
                    )
                    return TranslationResult(
                        translated_text="",
                        source_text=text,
                        source_lang=source_lang,
                        target_lang=target_lang,
                        model_used=model,
                        provider=self.provider_id,
                        latency_ms=latency,
                        error="Model returned an empty response (no translated text)",
                        success=False,
                    )

                translated = content.strip()
                usage = response.usage
                in_tok = usage.prompt_tokens if usage else 0
                out_tok = usage.completion_tokens if usage else 0

                return TranslationResult(
                    translated_text=translated,
                    source_text=text,
                    source_lang=source_lang,
                    target_lang=target_lang,
                    model_used=model,
                    provider=self.provider_id,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    total_tokens=usage.total_tokens if usage else 0,
                    cost_estimate=calculate_cost(self.provider_id, model, in_tok, out_tok),
                    latency_ms=latency,
                    success=True,
                )
            except RateLimitError as e:
                rate_limit_retries += 1
                if rate_limit_retries > RATE_LIMIT_MAX_RETRIES:
                    latency = (time.time() - start_time) * 1000
                    logger.error(
                        "%s still rate-limited after %d retries - giving up: %s",
                        self.name, rate_limit_retries - 1, e,
                    )
                    return TranslationResult(
                        translated_text="",
                        source_text=text,
                        source_lang=source_lang,
                        target_lang=target_lang,
                        model_used=model,
                        provider=self.provider_id,
                        latency_ms=latency,
                        error=f"Rate limited (429) after {rate_limit_retries - 1} retries: {e}",
                        success=False,
                    )
                logger.warning(
                    "%s rate-limited (429) - waiting %ds before retry %d/%d",
                    self.name, RATE_LIMIT_WAIT_S, rate_limit_retries, RATE_LIMIT_MAX_RETRIES,
                )
                self._stop_event.wait(RATE_LIMIT_WAIT_S)
                continue
            except InternalServerError as e:
                server_error_retries += 1
                if server_error_retries > SERVER_ERROR_MAX_RETRIES:
                    latency = (time.time() - start_time) * 1000
                    logger.error(
                        "%s server error persisted after %d retries - giving up: %s",
                        self.name, server_error_retries - 1, e,
                    )
                    return TranslationResult(
                        translated_text="",
                        source_text=text,
                        source_lang=source_lang,
                        target_lang=target_lang,
                        model_used=model,
                        provider=self.provider_id,
                        latency_ms=latency,
                        error=f"Server error after {server_error_retries - 1} retries: {e}",
                        success=False,
                    )
                logger.warning(
                    "%s server error (5xx) - waiting %ds before retry %d/%d",
                    self.name, SERVER_ERROR_WAIT_S, server_error_retries, SERVER_ERROR_MAX_RETRIES,
                )
                self._stop_event.wait(SERVER_ERROR_WAIT_S)
                continue
            except Exception as e:
                latency = (time.time() - start_time) * 1000
                logger.error("%s translation failed: %s", self.name, e)
                return TranslationResult(
                    translated_text="",
                    source_text=text,
                    source_lang=source_lang,
                    target_lang=target_lang,
                    model_used=model,
                    provider=self.provider_id,
                    latency_ms=latency,
                    error=str(e),
                    success=False,
                )

    def test_connection(self) -> ConnectionResult:
        try:
            models = self.list_models()
            return ConnectionResult(
                connected=True,
                provider=self.provider_id,
                message=f"Connected. {len(models)} models available.",
                models_available=len(models),
            )
        except Exception as e:
            return ConnectionResult(
                connected=False,
                provider=self.provider_id,
                message=f"Connection failed: {e}",
                error=str(e),
            )

    def _get_default_model(self) -> str:
        """Override in subclass to provide a sensible default model."""
        return ""
