"""Google Gemini provider (Gemini 3.1 Pro, Flash, etc.).

Uses the google-genai SDK for native access, but also supports
OpenAI-compatible mode via the Gemini OpenAI endpoint.
"""

import time
from google import genai
from google.genai import errors, types

from ai.provider_base import (
    AIProviderBase, ModelInfo, TranslationResult, ConnectionResult,
)
from ai.pricing_registry import calculate_cost
from utils.logger import get_logger

logger = get_logger("ai.gemini")

# See ai/provider_openai_compat.py for why 429 gets a real cooldown instead
# of a quick backoff: free-tier RPM quotas reset per minute, and a 429
# should never be treated as "this batch's content is broken".
RATE_LIMIT_WAIT_S = 30
RATE_LIMIT_MAX_RETRIES = 6

# 5xx is the server's fault, not the request's - a short retry catches
# transient blips without burning a whole batch (and its share of a
# free-tier daily quota) on translation_engine's bisection just because
# Google's backend hiccuped once.
SERVER_ERROR_WAIT_S = 5
SERVER_ERROR_MAX_RETRIES = 3


class GeminiProvider(AIProviderBase):
    name = "Google Gemini"
    provider_id = "gemini"
    requires_api_key = True
    supports_openai_compat = True

    def __init__(self, api_key: str = "",
                 base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/",
                 timeout: int = 60, max_retries: int = 3):
        super().__init__(api_key, base_url, timeout, max_retries)
        self._client = None

    def _get_client(self) -> genai.Client:
        if self._client is None:
            self._client = genai.Client(
                api_key=self._api_key,
                http_options=types.HttpOptions(
                    # Our own retry loop below (429 -> 30s cooldown, 5xx -> 5s)
                    # already handles the exact codes the SDK retries by
                    # default (408/429/5xx, up to 5 attempts with exponential
                    # backoff to 60s EACH). Stacked together, one logical
                    # request could silently retry for minutes inside the SDK
                    # before our code ever sees the error - attempts=1 disables
                    # that inner layer so ours is the only one running, with
                    # timing we actually chose.
                    retry_options=types.HttpRetryOptions(attempts=1),
                    timeout=self._timeout * 1000,
                ),
            )
        return self._client

    def list_models(self) -> list[ModelInfo]:
        try:
            client = self._get_client()
            models = []
            for m in client.models.list():
                model_id = m.name
                if model_id.startswith("models/"):
                    model_id = model_id[7:]
                # Filter by capability ("generateContent" = text generation),
                # not by name - a substring check on "gemini" wrongly excludes
                # Gemma models (same API, same generateContent capability)
                # while letting image/video models with "gemini" in their
                # internal id (e.g. Nano Banana) slip through.
                if "generateContent" in (m.supported_actions or []):
                    models.append(ModelInfo(
                        model_id=model_id,
                        name=getattr(m, "display_name", model_id),
                        provider=self.provider_id,
                    ))
            models.sort(key=lambda x: x.model_id)
            return models
        except Exception as e:
            logger.error("Failed to list Gemini models: %s", e)
            raise ConnectionError(
                f"Failed to list Gemini models: {e}. "
                f"Check your API key and internet connection."
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
            model = "gemini-2.5-flash"

        if not system_prompt:
            from ai.default_prompt import get_default_system_prompt
            system_prompt = get_default_system_prompt(source_lang, target_lang)

        user_content = text
        if context:
            user_content = f"Context: {context}\n\nTranslate: {text}"

        start_time = time.time()
        rate_limit_retries = 0
        server_error_retries = 0
        while True:
            try:
                client = self._get_client()
                response = client.models.generate_content(
                    model=model,
                    contents=user_content,
                    config=types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        temperature=0.3,
                        top_p=0.95,
                        top_k=40,
                        # Deliberately NOT setting max_output_tokens. Reasoning
                        # models (Gemma "-it" models, Gemini thinking models)
                        # spend output tokens on internal "thinking" that counts
                        # against this budget but can't be sized in advance -
                        # thinking_budget is flatly rejected for these Gemma
                        # models (confirmed live: 400 INVALID_ARGUMENT), and
                        # Google's own SDK team confirms max_output_tokens=None
                        # is the only reliable workaround (googleapis/python-genai
                        # issue #782) - any explicit cap risks a MAX_TOKENS cutoff
                        # mid-thought with response.text coming back empty. We're
                        # not VRAM-constrained here, so there's no upside to
                        # capping it ourselves.
                    ),
                )
                latency = (time.time() - start_time) * 1000

                finish_reason = None
                if response.candidates:
                    finish_reason = getattr(response.candidates[0], "finish_reason", None)

                if not response.text or not response.text.strip():
                    reason_note = f" (finish_reason={finish_reason})" if finish_reason else ""
                    logger.warning(
                        "Gemini returned an empty response for: %r%s", text[:80], reason_note,
                    )
                    return TranslationResult(
                        translated_text="",
                        source_text=text,
                        source_lang=source_lang,
                        target_lang=target_lang,
                        model_used=model,
                        provider=self.provider_id,
                        latency_ms=latency,
                        error=f"Model returned an empty response{reason_note} - "
                              f"likely hit MAX_TOKENS mid-thought if finish_reason is MAX_TOKENS",
                        success=False,
                    )

                translated = response.text.strip()

                in_tok = 0
                out_tok = 0
                if response.usage_metadata:
                    in_tok = getattr(response.usage_metadata, "prompt_token_count", 0) or 0
                    out_tok = getattr(response.usage_metadata, "candidates_token_count", 0) or 0

                return TranslationResult(
                    translated_text=translated,
                    source_text=text,
                    source_lang=source_lang,
                    target_lang=target_lang,
                    model_used=model,
                    provider=self.provider_id,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    total_tokens=in_tok + out_tok,
                    cost_estimate=calculate_cost(self.provider_id, model, in_tok, out_tok),
                    latency_ms=latency,
                    success=True,
                )
            except errors.ClientError as e:
                if getattr(e, "code", None) == 429:
                    rate_limit_retries += 1
                    if rate_limit_retries > RATE_LIMIT_MAX_RETRIES:
                        latency = (time.time() - start_time) * 1000
                        logger.error(
                            "Gemini still rate-limited after %d retries - giving up: %s",
                            rate_limit_retries - 1, e,
                        )
                        return TranslationResult(
                            translated_text="",
                            source_text=text,
                            source_lang=source_lang,
                            target_lang=target_lang,
                            model_used=model,
                            provider=self.provider_id,
                            latency_ms=(time.time() - start_time) * 1000,
                            error=f"Rate limited (429) after {rate_limit_retries - 1} retries: {e}",
                            success=False,
                        )
                    logger.warning(
                        "Gemini rate-limited (429) - waiting %ds before retry %d/%d",
                        RATE_LIMIT_WAIT_S, rate_limit_retries, RATE_LIMIT_MAX_RETRIES,
                    )
                    time.sleep(RATE_LIMIT_WAIT_S)
                    continue
                latency = (time.time() - start_time) * 1000
                logger.error("Gemini translation failed: %s", e)
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
            except errors.ServerError as e:
                server_error_retries += 1
                if server_error_retries > SERVER_ERROR_MAX_RETRIES:
                    latency = (time.time() - start_time) * 1000
                    logger.error(
                        "Gemini server error persisted after %d retries - giving up: %s",
                        server_error_retries - 1, e,
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
                    "Gemini server error (%s) - waiting %ds before retry %d/%d",
                    getattr(e, "code", "5xx"), SERVER_ERROR_WAIT_S,
                    server_error_retries, SERVER_ERROR_MAX_RETRIES,
                )
                time.sleep(SERVER_ERROR_WAIT_S)
                continue
            except Exception as e:
                latency = (time.time() - start_time) * 1000
                logger.error("Gemini translation failed: %s", e)
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
