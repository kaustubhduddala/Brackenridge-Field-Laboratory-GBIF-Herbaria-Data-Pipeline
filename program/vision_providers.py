"""Vision-model providers.

LM Studio on this computer is the default. OpenAI, Google Gemini, Anthropic Claude and any other
OpenAI-compatible server (OpenRouter, Ollama, vLLM, a lab server) can be used with an API key.
Every client answers `ask_json(images, system, text, schema, name)` with a parsed JSON object.
Claude goes through the official anthropic SDK; the others through the openai SDK.
"""
import base64
import io
import json
import re
from dataclasses import dataclass, field

import requests

from config import (DEFAULT_TOKEN_LIMIT, LMSTUDIO_MODEL, LMSTUDIO_URL, MIN_CONTEXT_LENGTH, REQUEST_TIMEOUT_S,
                    ai_api_key)
from .utils import check

PROVIDERS = {
    "lmstudio": {"label": "LM Studio (this computer)", "base_url": LMSTUDIO_URL, "needs_key": False, "env": (),
                 "max_image_side": 2560, "key_url": ""},
    "openai": {"label": "OpenAI", "base_url": "https://api.openai.com/v1", "needs_key": True,
               "env": ("OPENAI_API_KEY",), "max_image_side": 2048,
               "key_url": "https://platform.openai.com/api-keys"},
    "gemini": {"label": "Google Gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
               "needs_key": True, "env": ("GEMINI_API_KEY", "GOOGLE_API_KEY"), "max_image_side": 3072,
               "key_url": "https://aistudio.google.com/apikey"},
    "anthropic": {"label": "Anthropic Claude", "base_url": "https://api.anthropic.com", "needs_key": True,
                  "env": ("ANTHROPIC_API_KEY",), "max_image_side": 2576, "default_model": "claude-opus-5-5",
                  "key_url": "https://platform.claude.com/settings/keys"},
    "custom": {"label": "Other OpenAI-compatible server", "base_url": "", "needs_key": False, "env": (),
               "max_image_side": 2048, "key_url": ""},
}
DEFAULT_PROVIDER = "lmstudio"
NOT_CHAT_MODEL = re.compile(r"embed|tts|whisper|dall-e|davinci|babbage|moderation|transcribe|realtime|audio|"
                            r"imagen|veo|aqa|search|computer-use|-image", re.IGNORECASE)


class ProviderError(RuntimeError):
    """A problem reaching or authenticating with the model provider, worded for the user."""


class ModelOutputError(ValueError):
    def __init__(self, message, raw):
        super().__init__(message)
        self.raw = raw


@dataclass
class ModelSettings:
    provider: str = DEFAULT_PROVIDER
    base_url: str = ""
    model: str = ""
    token_limit: int = DEFAULT_TOKEN_LIMIT
    reasoning_effort: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def info(self):
        return PROVIDERS.get(self.provider, PROVIDERS["custom"])

    @property
    def url(self):
        return (self.base_url or "").strip() or self.info["base_url"]

    @property
    def label(self):
        return self.info["label"]


def settings_from_app(saved, provider=None, model=None):
    """ModelSettings from the app's settings.json values (shared by the GUI and the command line)."""
    from config import THINKING_CHOICES

    provider = provider or saved.get("provider") or DEFAULT_PROVIDER
    if provider not in PROVIDERS:
        raise ProviderError(f"Unknown provider {provider}. Choose one of: {', '.join(PROVIDERS)}")
    servers, models = saved.get("servers") or {}, saved.get("models") or {}
    server = servers.get(provider) or (saved.get("server", "") if provider == "lmstudio" else "")
    try:
        token_limit = max(int(saved.get("token_limit", DEFAULT_TOKEN_LIMIT)), 0)
    except (TypeError, ValueError):
        token_limit = DEFAULT_TOKEN_LIMIT
    return ModelSettings(provider=provider, base_url=server, model=model or models.get(provider, ""),
                         token_limit=token_limit, reasoning_effort=THINKING_CHOICES.get(saved.get("thinking")))


def provider_key(settings):
    return ai_api_key(settings.provider, settings.info["env"])


def describe_key(provider):
    info = PROVIDERS[provider]
    key, where = ai_api_key(provider, info["env"])
    if not key:
        return "No API key saved." if info["needs_key"] else "No API key needed."
    return f"API key ending in …{key[-4:]} from {where}."


# --------------------------------------------------------------------------- images and JSON

def encode_image(image, max_side, quality=92):
    """JPEG bytes of a PIL image (or an existing data URL), no larger than max_side on the long edge."""
    if isinstance(image, str):
        header, _, data = image.partition(",")
        return base64.b64decode(data)
    work = image.convert("RGB")
    if max(work.size) > max_side:
        work = work.copy()
        work.thumbnail((max_side, max_side))
    buffer = io.BytesIO()
    work.save(buffer, "JPEG", quality=quality)
    return buffer.getvalue()


def data_url(jpeg_bytes):
    return "data:image/jpeg;base64," + base64.b64encode(jpeg_bytes).decode()


def parse_json(content):
    text = re.sub(r"<think>.*?</think>", "", str(content or ""), flags=re.DOTALL | re.IGNORECASE)
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1]
    if not text.strip():
        raise ValueError("Model returned an empty response")
    # Keep the last complete top-level object; objects nested inside it are skipped, so a nested
    # answer such as {"panicles": [{"tip": {...}}]} is not mistaken for its innermost point.
    decoder = json.JSONDecoder()
    found, position = None, 0
    while True:
        start = text.find("{", position)
        if start < 0:
            break
        try:
            payload, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            position = start + 1
            continue
        if isinstance(payload, dict) and payload:
            found = payload
        position = end
    if found is None:
        raise ValueError("No valid JSON object found in model response")
    return found


def _extra(obj, name):
    value = getattr(obj, name, None)
    if value is None:
        value = (getattr(obj, "model_extra", None) or {}).get(name)
    return value


# --------------------------------------------------------------------------- LM Studio helpers

def _api_root(base_url):
    root = base_url.rstrip("/")
    return root[:-3] if root.endswith("/v1") else root


def _lmstudio_models(base_url):
    resp = requests.get(f"{_api_root(base_url)}/api/v0/models", timeout=10)
    resp.raise_for_status()
    return resp.json().get("data", [])


def find_loaded_model(base_url=LMSTUDIO_URL):
    if LMSTUDIO_MODEL:
        return {"id": LMSTUDIO_MODEL, "type": "", "context": None}
    try:
        models = _lmstudio_models(base_url)
    except (requests.RequestException, ValueError):
        models = None
    if models is not None:
        loaded = [m for m in models if m.get("state") == "loaded" and m.get("type") in ("vlm", "llm")]
        if not loaded:
            raise ProviderError("LM Studio is running but no model is loaded. Load a vision model in LM Studio first.")
        best = next((m for m in loaded if m.get("type") == "vlm"), loaded[0])
        return {"id": best["id"], "type": best.get("type", ""), "context": best.get("loaded_context_length")}
    try:
        resp = requests.get(f"{base_url.rstrip('/')}/models", timeout=10)
        resp.raise_for_status()
        data = resp.json().get("data", [])
    except (requests.RequestException, ValueError) as exc:
        raise ProviderError(f"Could not reach LM Studio at {base_url}: {exc}") from exc
    if not data:
        raise ProviderError("The server reports no models. Load a vision model in LM Studio first.")
    return {"id": data[0]["id"], "type": "", "context": None}


def model_warnings(model):
    warnings = []
    if model.get("type") == "llm":
        warnings.append(f"{model['id']} is a text-only model; load a vision model to measure images.")
    if model.get("context") and model["context"] < MIN_CONTEXT_LENGTH:
        warnings.append(f"{model['id']} is loaded with a {model['context']}-token context. Reload it in LM Studio "
                        f"with at least {MIN_CONTEXT_LENGTH} so the image, prompt and answer fit.")
    return warnings


# --------------------------------------------------------------------------- OpenAI-compatible client

class OpenAICompatibleClient:
    """LM Studio, OpenAI, Gemini (through Google's OpenAI-compatible endpoint) and custom servers."""

    # Request options some servers reject, with words their error messages use for them.
    # They are dropped one at a time when the server rejects the request.
    OPTIONAL = {"reasoning_effort": ("reasoning",), "response_format": ("response_format", "json_schema", "structured"),
                "temperature": ("temperature",)}

    def __init__(self, settings, log=print):
        from openai import OpenAI

        self.settings, self.log = settings, log
        self.provider = settings.provider
        self.max_image_side = settings.info["max_image_side"]
        key, _ = provider_key(settings)
        if settings.info["needs_key"] and not key:
            raise ProviderError(f"No {settings.label} API key is saved. Add one with the API key… button "
                                f"on the Measure tab.")
        if self.provider == "custom" and not settings.url:
            raise ProviderError("Enter the server address for the OpenAI-compatible server.")
        self.client = OpenAI(base_url=settings.url, api_key=key or "not-needed", timeout=REQUEST_TIMEOUT_S)
        self.dropped = set()
        self.warnings = []
        if self.provider == "lmstudio" and not settings.model:
            model = find_loaded_model(settings.url)
            self.warnings = model_warnings(model)
            self.model_id = model["id"]
        else:
            self.model_id = settings.model.strip()
        if not self.model_id:
            raise ProviderError(f"Choose a {settings.label} model first (use Refresh to list the available ones).")

    def _request(self, images, system, text, schema, name):
        content = [{"type": "text", "text": text}]
        for image in images:
            part = {"type": "image_url", "image_url": {"url": data_url(encode_image(image, self.max_image_side))}}
            if self.provider == "openai":
                part["image_url"]["detail"] = "high"
            content.append(part)
        request = {"model": self.model_id, "stream": True, "temperature": 0.0,
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}]}
        limit = int(self.settings.token_limit or 0)
        if limit:
            request["max_completion_tokens" if self.provider == "openai" else "max_tokens"] = limit
        if self.settings.reasoning_effort:
            request["reasoning_effort"] = self.settings.reasoning_effort
        if schema is not None:
            request["response_format"] = {"type": "json_schema",
                                          "json_schema": {"name": name, "strict": True, "schema": schema}}
        for option in self.dropped:
            request.pop(option, None)
        return request

    def ask_json(self, images, system, text, schema=None, name="answer", control=None, status=None, stage=""):
        from openai import APIConnectionError, AuthenticationError, BadRequestError, NotFoundError, PermissionDeniedError

        request = self._request(images, system, text, schema, name)
        while True:
            try:
                return self._stream(request, control, status, stage)
            except BadRequestError as exc:
                message = str(exc).lower()
                if "max_tokens" in message and "max_tokens" in request:
                    request["max_completion_tokens"] = request.pop("max_tokens")
                    continue
                named = [o for o, words in self.OPTIONAL.items() if o in request and any(w in message for w in words)]
                option = (named or [o for o in self.OPTIONAL if o in request] or [None])[0]
                if option is None:
                    raise
                request.pop(option)
                self.dropped.add(option)
                self.log(f"    {self.settings.label} rejected the {option} option ({exc}); continuing without it")
            except (AuthenticationError, PermissionDeniedError) as exc:
                raise ProviderError(f"{self.settings.label} rejected the API key: {exc}") from exc
            except NotFoundError as exc:
                raise ProviderError(f"{self.settings.label} does not know the model {self.model_id}: {exc}") from exc
            except APIConnectionError as exc:
                raise ProviderError(f"Could not reach {self.settings.label} at {self.settings.url}: {exc}") from exc

    def _stream(self, request, control, status, stage):
        content, reasoning = [], []
        finish_reason, model, tokens, reported = None, self.model_id, 0, 0
        stream = self.client.chat.completions.create(**request)
        try:
            for chunk in stream:
                check(control)
                model = getattr(chunk, "model", None) or model
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta
                if delta is not None:
                    if delta.content:
                        content.append(delta.content)
                        tokens += 1
                    thought = _extra(delta, "reasoning_content") or _extra(delta, "reasoning")
                    if thought:
                        reasoning.append(thought)
                        tokens += 1
                finish_reason = choice.finish_reason or finish_reason
                if status and tokens - reported >= 20:
                    reported = tokens
                    status(f"{stage or 'Thinking'}: {'writing answer' if content else 'thinking'}, about {tokens} tokens")
        finally:
            stream.close()
        text = "".join(content)
        raw = {"provider": self.provider, "model": model, "finish_reason": finish_reason, "approx_tokens": tokens,
               "structured_output": "response_format" in request, "content": text, "reasoning": "".join(reasoning)}
        try:
            raw["parsed"] = parse_json(text if text.strip() else raw["reasoning"])
        except ValueError as exc:
            message = str(exc)
            if finish_reason == "length":
                message = (f"The model stopped after about {tokens} tokens without finishing its answer. Raise the "
                           "token limit, raise the model's context length, or set Thinking to Low or Off.")
            raise ModelOutputError(message, raw) from exc
        return raw["parsed"], raw


# --------------------------------------------------------------------------- public helpers

def open_client(settings, log=print):
    if settings.provider == "anthropic":
        return ClaudeClient(settings, log)
    return OpenAICompatibleClient(settings, log)


def list_models(settings):
    """Model ids the provider offers, loaded/likely-vision ones first where the provider says."""
    if settings.provider == "anthropic":
        return list_claude_models(settings)
    if settings.provider == "lmstudio":
        try:
            models = _lmstudio_models(settings.url)
        except (requests.RequestException, ValueError) as exc:
            raise ProviderError(f"Could not reach LM Studio at {settings.url}: {exc}") from exc
        usable = [m for m in models if m.get("type") in ("vlm", "llm")]
        usable.sort(key=lambda m: (m.get("state") != "loaded", m.get("type") != "vlm", m["id"]))
        return [m["id"] for m in usable]

    from openai import APIConnectionError, AuthenticationError, OpenAI, PermissionDeniedError

    key, _ = provider_key(settings)
    if settings.info["needs_key"] and not key:
        raise ProviderError(f"No {settings.label} API key is saved.")
    client = OpenAI(base_url=settings.url, api_key=key or "not-needed", timeout=30)
    try:
        ids = [m.id for m in client.models.list()]
    except (AuthenticationError, PermissionDeniedError) as exc:
        raise ProviderError(f"{settings.label} rejected the API key: {exc}") from exc
    except APIConnectionError as exc:
        raise ProviderError(f"Could not reach {settings.label} at {settings.url}: {exc}") from exc
    ids = [i.removeprefix("models/") for i in ids]
    if settings.provider in ("openai", "gemini"):
        ids = [i for i in ids if not NOT_CHAT_MODEL.search(i)]
    return sorted(dict.fromkeys(ids))


def check_connection(settings):
    client = open_client(settings)
    lines = [f"Connected to {settings.label}. Measurements will use {client.model_id}."]
    if settings.provider != "lmstudio":
        available = list_models(settings)
        if available and client.model_id not in available:
            lines.append(f"Warning: {client.model_id} is not in the provider's model list.")
    lines += [f"Warning: {w}" for w in client.warnings]
    return "\n".join(lines)


# --------------------------------------------------------------------------- Anthropic Claude

DEFAULT_MODEL = "claude-opus-5-5"
# Models that accept server-side refusal fallbacks ("default" routing picks the substitute model).
FALLBACK_MODELS = ("claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5")
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# The app's Thinking choice -> Claude effort. Thinking cannot be switched off on current Claude models.
EFFORT = {"low": "low", "none": "low", "medium": "medium", "high": "high"}


def _client(settings, timeout=REQUEST_TIMEOUT_S):
    import anthropic

    key, _ = provider_key(settings)
    if not key:
        raise ProviderError("No Anthropic API key is saved. Add one with the API key… button on the Measure tab.")
    return anthropic.Anthropic(api_key=key, timeout=timeout, max_retries=3)


def list_claude_models(settings):
    import anthropic

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
        import anthropic

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
