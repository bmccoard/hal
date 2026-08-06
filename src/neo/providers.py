from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from typing import Any

from .config import Config
from .models import ContentBlock, Message, Request, Response, Usage


class ProviderError(RuntimeError):
    pass


class Provider(ABC):
    name: str

    @abstractmethod
    def complete(self, request: Request) -> Response: ...


class HTTPProvider(Provider):
    timeout = 300
    max_retries = 4

    def _post(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        body = json.dumps(payload).encode()
        for attempt in range(self.max_retries + 1):
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", **headers}, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as response:
                    return json.loads(response.read())
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")
                if exc.code not in {408, 409, 429} and exc.code < 500:
                    raise ProviderError(f"{self.name} {exc.code}: {detail}") from exc
                if attempt == self.max_retries:
                    raise ProviderError(f"{self.name} {exc.code}: {detail}") from exc
                retry_after = exc.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else .5 * (2 ** attempt) + random.random() * .2
                time.sleep(min(delay, 30))
            except (OSError, TimeoutError) as exc:
                if attempt == self.max_retries:
                    raise ProviderError(f"{self.name}: {exc}") from exc
                time.sleep(.5 * (2 ** attempt) + random.random() * .2)
        raise AssertionError("unreachable")


def _anthropic_block(block: ContentBlock) -> dict[str, Any] | None:
    if block.type == "text":
        return {"type": "text", "text": block.text}
    if block.type == "image" and block.source:
        return {"type": "image", "source": block.source}
    if block.type == "tool_use":
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    if block.type == "tool_result":
        return {"type": "tool_result", "tool_use_id": block.tool_use_id, "content": block.content, "is_error": block.is_error}
    return None


class AnthropicProvider(HTTPProvider):
    name = "anthropic"

    def __init__(self, api_key: str, endpoint: str = "https://api.anthropic.com/v1/messages") -> None:
        self.api_key, self.endpoint = api_key, endpoint

    def complete(self, request: Request) -> Response:
        messages = []
        for message in request.messages:
            blocks = [value for block in message.content if (value := _anthropic_block(block)) is not None]
            if blocks:
                messages.append({"role": "assistant" if message.role == "assistant" else "user", "content": blocks})
        data = self._post(self.endpoint, {
            "model": request.model, "system": request.system, "messages": messages,
            "tools": [{"name": x.name, "description": x.description, "input_schema": x.input_schema} for x in request.tools],
            "max_tokens": request.max_tokens,
        }, {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"})
        if data.get("error"):
            raise ProviderError(f"anthropic: {data['error'].get('message', data['error'])}")
        usage = data.get("usage") or {}
        return Response(
            [ContentBlock.from_dict(x) for x in data.get("content", [])], data.get("stop_reason", "end_turn"),
            Usage(usage.get("input_tokens", 0), usage.get("output_tokens", 0), usage.get("cache_creation_input_tokens", 0), usage.get("cache_read_input_tokens", 0)),
        )


def _openai_input(messages: list[Message]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for message in messages:
        parts: list[dict[str, Any]] = []
        kind = "output_text" if message.role == "assistant" else "input_text"
        for block in message.content:
            if block.type == "text" and block.text:
                parts.append({"type": kind, "text": block.text})
            elif block.type == "image" and block.source and message.role != "assistant":
                src = block.source
                parts.append({"type": "input_image", "image_url": f"data:{src.get('media_type')};base64,{src.get('data')}"})
            elif block.type == "tool_use":
                if parts:
                    output.append({"type": "message", "role": "assistant", "content": parts}); parts = []
                output.append({"type": "function_call", "call_id": block.id, "name": block.name, "arguments": json.dumps(block.input)})
            elif block.type == "tool_result":
                output.append({"type": "function_call_output", "call_id": block.tool_use_id, "output": block.content})
            elif block.type == "raw" and isinstance(block.raw, dict) and block.raw.get("type") == "reasoning":
                output.append(block.raw)
        if parts:
            output.append({"type": "message", "role": "assistant" if message.role == "assistant" else "user", "content": parts})
    return output


class OpenAIProvider(HTTPProvider):
    name = "openai"

    def __init__(self, api_key: str, endpoint: str = "https://api.openai.com/v1/responses") -> None:
        self.api_key, self.endpoint = api_key, endpoint

    def complete(self, request: Request) -> Response:
        data = self._post(self.endpoint, {
            "model": request.model, "instructions": request.system, "input": _openai_input(request.messages),
            "tools": [{"type": "function", "name": x.name, "description": x.description, "parameters": x.input_schema} for x in request.tools],
            "tool_choice": "auto", "max_output_tokens": request.max_tokens, "store": False,
            "include": ["reasoning.encrypted_content"],
        }, {"Authorization": f"Bearer {self.api_key}"})
        if data.get("error"):
            raise ProviderError(f"openai: {data['error'].get('message', data['error'])}")
        blocks: list[ContentBlock] = []
        saw_tool = False
        for item in data.get("output", []):
            if item.get("type") == "message":
                text = "".join(x.get("text") or x.get("refusal") or "" for x in item.get("content", []) if x.get("type") in {"output_text", "refusal"})
                if text:
                    blocks.append(ContentBlock("text", text=text))
            elif item.get("type") == "function_call":
                saw_tool = True
                try:
                    arguments = json.loads(item.get("arguments") or "{}")
                except json.JSONDecodeError as exc:
                    raise ProviderError(f"openai: invalid tool arguments for {item.get('name')}: {exc}") from exc
                blocks.append(ContentBlock("tool_use", id=item.get("call_id", ""), name=item.get("name", ""), input=arguments))
            elif item.get("type") == "reasoning":
                blocks.append(ContentBlock("raw", raw=item))
        usage = data.get("usage") or {}; details = usage.get("input_tokens_details") or {}
        status = data.get("status", "completed")
        stop = "tool_use" if saw_tool else ("max_tokens" if status == "incomplete" else "end_turn")
        return Response(blocks, stop, Usage(usage.get("input_tokens", 0), usage.get("output_tokens", 0), 0, details.get("cached_tokens", 0)))


def _chat_messages(messages: list[Message]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for message in messages:
        text = "".join(x.text for x in message.content if x.type == "text")
        calls = [x for x in message.content if x.type == "tool_use"]
        results = [x for x in message.content if x.type == "tool_result"]
        if calls:
            output.append({"role": "assistant", "content": text or None, "tool_calls": [{"id": x.id, "type": "function", "function": {"name": x.name, "arguments": json.dumps(x.input)}} for x in calls]})
        elif text:
            output.append({"role": message.role if message.role != "tool" else "user", "content": text})
        for result in results:
            output.append({"role": "tool", "tool_call_id": result.tool_use_id, "content": result.content})
    return output


class OpenRouterProvider(HTTPProvider):
    name = "openrouter"

    def __init__(self, api_key: str, endpoint: str = "https://openrouter.ai/api/v1/chat/completions") -> None:
        self.api_key, self.endpoint = api_key, endpoint

    def complete(self, request: Request) -> Response:
        messages = [{"role": "system", "content": request.system}, *_chat_messages(request.messages)]
        data = self._post(self.endpoint, {
            "model": request.model, "messages": messages,
            "tools": [{"type": "function", "function": {"name": x.name, "description": x.description, "parameters": x.input_schema}} for x in request.tools],
            "max_tokens": request.max_tokens,
        }, {"Authorization": f"Bearer {self.api_key}"})
        if data.get("error"):
            raise ProviderError(f"openrouter: {data['error'].get('message', data['error'])}")
        choice = (data.get("choices") or [{}])[0]; message = choice.get("message") or {}
        blocks = [ContentBlock("text", text=message["content"])] if message.get("content") else []
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            blocks.append(ContentBlock("tool_use", id=call.get("id", ""), name=fn.get("name", ""), input=json.loads(fn.get("arguments") or "{}")))
        usage = data.get("usage") or {}
        return Response(blocks, "tool_use" if message.get("tool_calls") else choice.get("finish_reason", "end_turn"), Usage(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)))


class OpenAICompatibleProvider(OpenRouterProvider):
    """OpenAI-compatible Chat Completions provider at a custom API base."""

    def __init__(self, name: str, api_key: str, api_base: str) -> None:
        if not api_base:
            raise ProviderError(f"{name}: api_base is required")
        self.name = name
        super().__init__(api_key, f"{api_base.rstrip('/')}/chat/completions")


class GoogleProvider(HTTPProvider):
    name = "google"

    def __init__(self, api_key: str, endpoint: str = "https://generativelanguage.googleapis.com/v1beta/models") -> None:
        self.api_key, self.endpoint = api_key, endpoint

    def complete(self, request: Request) -> Response:
        tool_names: dict[str, str] = {}
        contents = []
        for message in request.messages:
            parts = []
            for block in message.content:
                if block.type == "text": parts.append({"text": block.text})
                elif block.type == "image" and block.source: parts.append({"inlineData": {"mimeType": block.source.get("media_type"), "data": block.source.get("data")}})
                elif block.type == "tool_use":
                    tool_names[block.id] = block.name; parts.append({"functionCall": {"id": block.id, "name": block.name, "args": block.input}})
                elif block.type == "tool_result":
                    parts.append({"functionResponse": {"id": block.tool_use_id, "name": tool_names.get(block.tool_use_id, block.tool_use_id), "response": {"error" if block.is_error else "output": block.content}}})
            if parts: contents.append({"role": "model" if message.role == "assistant" else "user", "parts": parts})
        url = f"{self.endpoint.rstrip('/')}/{urllib.parse.quote(request.model, safe='')}:generateContent"
        data = self._post(url, {
            "systemInstruction": {"parts": [{"text": request.system}]}, "contents": contents,
            "tools": [{"functionDeclarations": [{"name": x.name, "description": x.description, "parameters": x.input_schema} for x in request.tools]}],
            "generationConfig": {"maxOutputTokens": request.max_tokens},
        }, {"x-goog-api-key": self.api_key})
        if data.get("error"): raise ProviderError(f"google: {data['error'].get('message', data['error'])}")
        candidates = data.get("candidates") or []
        if not candidates: raise ProviderError("google: no candidates returned")
        candidate = candidates[0]; blocks = []
        for index, part in enumerate((candidate.get("content") or {}).get("parts") or []):
            if part.get("text"): blocks.append(ContentBlock("text", text=part["text"], raw=part))
            if part.get("functionCall"):
                call = part["functionCall"]; blocks.append(ContentBlock("tool_use", id=call.get("id") or f"{call.get('name','tool')}_{index}", name=call.get("name", ""), input=call.get("args") or {}, raw=part))
        usage = data.get("usageMetadata") or {}
        finish = candidate.get("finishReason", "STOP")
        return Response(blocks, "tool_use" if any(x.type == "tool_use" for x in blocks) else ("max_tokens" if finish == "MAX_TOKENS" else "end_turn"), Usage(usage.get("promptTokenCount", 0), usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0), 0, usage.get("cachedContentTokenCount", 0)))


def create_provider(config: Config) -> Provider:
    profile = config.active_profile()
    backend = config.backend()
    if backend == "openai" and config.openai_auth == "subscription" and profile is None:
        raise ProviderError("OpenAI subscription auth is not supported by the Python port; use openai_auth: api_key")
    env_name = config.credential_env()
    key = os.environ.get(env_name, "").strip()
    if not key: raise ProviderError(f"{env_name} is not set")
    if profile:
        if profile.protocol == "chat_completions":
            return OpenAICompatibleProvider(profile.name, key, profile.api_base)
        if backend == "openai":
            return OpenAIProvider(key, f"{profile.api_base.rstrip('/')}/responses")
    classes = {"anthropic": AnthropicProvider, "openai": OpenAIProvider, "openrouter": OpenRouterProvider, "google": GoogleProvider}
    return classes[backend](key)
