"""Anthropic Claude through the official anthropic SDK (vision + structured JSON output)."""
import base64

import anthropic

from config import REQUEST_TIMEOUT_S
from .providers import ModelOutputError, ProviderError, encode_image, parse_json, provider_key
from .utils import check

DEFAULT_MODEL = "claude-opus-5-5"
# Models that accept server-side refusal fallbacks ("default" routing picks the substitute model).
FALLBACK_MODELS = ("claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5")
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# The app's Thinking choice -> Claude effort. Thinking cannot be switched off on current Claude models.
EFFORT = {"low": "low", "none": "low", "medium": "medium", "high": "high"}


def _client(settings, timeout=REQUEST_TIMEOUT_S):
    key, _ = provider_key(settings)
    if not key:
        raise ProviderError("No Anthropic API key is saved. Add one with the API key… button on the Measure tab.")
    return anthropic.Anthropic(api_key=key, timeout=timeout, max_retries=3)


def list_claude_models(settings):
    try:
        return [m.id for m in _client(settings, timeout=30).models.list()]
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
        raise ProviderError(f"Anthropic rejected the API key: {exc}") from exc
    except anthropic.APIConnectionError as exc:
        raise ProviderError(f"Could not reach Anthropic: {exc}") from exc


class ClaudeClient:
    def __init__(self, settings, log=print):
        self.settings, self.log = settings, log
        self.provider = "anthropic"
        self.max_image_side = settings.info["max_image_side"]
        self.client = _client(settings)
        self.model_id = settings.model.strip() or DEFAULT_MODEL
        self.warnings = []
        self.dropped = set()

    def _params(self, images, system, text, schema):
        content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                "data": base64.standard_b64encode(
                                                    encode_image(image, self.max_image_side)).decode()}}
                   for image in images]
        content.append({"type": "text", "text": text})
        limit = int(self.settings.token_limit or 0)
        params = {"model": self.model_id, "max_tokens": max(limit, 4096) if limit else 32000, "system": system,
                  "messages": [{"role": "user", "content": content}]}
        output_config = {}
        if schema is not None and "format" not in self.dropped:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        effort = EFFORT.get(self.settings.reasoning_effort or "")
        if effort and "effort" not in self.dropped:
            output_config["effort"] = effort
        if output_config:
            params["output_config"] = output_config
        if self.model_id in FALLBACK_MODELS and "fallbacks" not in self.dropped:
            params["betas"] = [FALLBACK_BETA]
            params["fallbacks"] = "default"
        return params

    def ask_json(self, images, system, text, schema=None, name="answer", control=None, status=None, stage=""):
        while True:
            params = self._params(images, system, text, schema)
            try:
                return self._stream(params, control, status, stage)
            except anthropic.BadRequestError as exc:
                message = str(exc).lower()
                option = next((o for o, words in (("fallbacks", ("fallback",)), ("effort", ("effort",)),
                                                  ("format", ("format", "schema")))
                               if o not in self.dropped and any(w in message for w in words)), None)
                if option is None:
                    raise
                self.dropped.add(option)
                self.log(f"    Claude rejected the {option} option ({exc}); continuing without it")
            except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
                raise ProviderError(f"Anthropic rejected the API key: {exc}") from exc
            except anthropic.NotFoundError as exc:
                raise ProviderError(f"Anthropic does not know the model {self.model_id}: {exc}") from exc
            except anthropic.APIConnectionError as exc:
                raise ProviderError(f"Could not reach Anthropic: {exc}") from exc

    def _stream(self, params, control, status, stage):
        events, reported = 0, 0
        with self.client.beta.messages.stream(**params) as stream:
            for event in stream:
                check(control)
                if getattr(event, "type", "") in ("text", "thinking"):
                    events += 1
                if status and events - reported >= 20:
                    reported = events
                    status(f"{stage or 'Thinking'}: about {events} chunks received")
            message = stream.get_final_message()
        text = "".join(block.text for block in message.content if block.type == "text")
        raw = {"provider": self.provider, "model": message.model, "finish_reason": message.stop_reason,
               "approx_tokens": getattr(message.usage, "output_tokens", None),
               "structured_output": "format" in params.get("output_config", {}), "content": text, "reasoning": ""}
        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            raise ModelOutputError(f"Claude declined this request ({getattr(details, 'category', None) or 'no reason given'}).", raw)
        try:
            raw["parsed"] = parse_json(text)
        except ValueError as exc:
            reason = str(exc)
            if message.stop_reason == "max_tokens":
                reason = "Claude stopped at the token limit before finishing. Raise the token limit (or set it to 0)."
            raise ModelOutputError(reason, raw) from exc
        return raw["parsed"], raw
