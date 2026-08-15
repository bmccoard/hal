from __future__ import annotations

import json
import os
import random
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from typing import Any

from .cancellation import CancellationToken, cancellation_or_default
from .config import Config
from .models import ContentBlock, Message, Request, Response, StreamDelta, Usage


class ProviderError(RuntimeError):
    pass


class StreamingUnsupported(ProviderError):
    """Raised only when a server rejects streaming before returning stream data."""


class BufferedStreamResponse(ProviderError):
    """Carries a successful JSON response from a server that ignored streaming."""

    def __init__(self, data: dict[str, Any]) -> None:
        super().__init__("server returned a buffered response to a streaming request")
        self.data = data


def _tool_call_block(call_id: str, name: str, raw_arguments: object) -> ContentBlock:
    """Decode tool arguments without aborting an otherwise valid model turn.

    Invalid arguments are never executed. The agent converts ``argument_error``
    into a matching tool result so the model can correct its call on the next turn.
    """
    if isinstance(raw_arguments, dict):
        return ContentBlock("tool_use", id=call_id, name=name, input=raw_arguments)
    raw = raw_arguments if isinstance(raw_arguments, str) else json.dumps(raw_arguments)
    try:
        arguments = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError) as exc:
        return ContentBlock(
            "tool_use", id=call_id, name=name,
            argument_error=(
                f"{name or 'tool'} was not executed: invalid JSON arguments ({exc}). "
                "Retry using one JSON object that exactly matches the tool schema."
            ),
        )
    if not isinstance(arguments, dict):
        return ContentBlock(
            "tool_use", id=call_id, name=name,
            argument_error=(
                f"{name or 'tool'} was not executed: tool arguments must be a JSON object. "
                "Retry using one JSON object that exactly matches the tool schema."
            ),
        )
    return ContentBlock("tool_use", id=call_id, name=name, input=arguments)


class Provider(ABC):
    name: str
    streaming_enabled = True

    @abstractmethod
    def complete(self, request: Request,
                 cancellation: CancellationToken | None = None) -> Response: ...

    def stream(self, request: Request, on_delta: Callable[[StreamDelta], None],
               cancellation: CancellationToken | None = None) -> Response:
        """Buffered compatibility implementation for providers without streaming."""
        response = self.complete(request, cancellation)
        for block in response.content:
            if block.type in {"text", "commentary"} and block.text:
                on_delta(StreamDelta(block.type, block.text))
        return response


class HTTPProvider(Provider):
    timeout = 300
    max_retries = 4

    def _iter_sse(self, url: str, payload: dict[str, Any], headers: dict[str, str],
                  cancellation: CancellationToken | None = None) -> Iterator[dict[str, Any]]:
        """Yield decoded Server-Sent Event JSON objects with connection retries."""
        cancellation = cancellation_or_default(cancellation)
        body = json.dumps(payload).encode()
        response = None
        for attempt in range(self.max_retries + 1):
            cancellation.raise_if_cancelled()
            req = urllib.request.Request(
                url, data=body,
                headers={
                    "Accept": "text/event-stream", "Content-Type": "application/json",
                    **headers,
                },
                method="POST",
            )
            try:
                response = urllib.request.urlopen(
                    req, timeout=cancellation.bounded_timeout(self.timeout),
                )
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")
                if exc.code in {400, 404, 405, 406, 415, 422, 501}:
                    raise StreamingUnsupported(
                        f"{self.name} rejected streaming ({exc.code}): {detail}"
                    ) from exc
                if exc.code not in {408, 409, 429} and exc.code < 500:
                    raise ProviderError(f"{self.name} {exc.code}: {detail}") from exc
                if attempt == self.max_retries:
                    raise ProviderError(f"{self.name} {exc.code}: {detail}") from exc
                retry_after = exc.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else .5 * (2 ** attempt) + random.random() * .2
                cancellation.wait(min(delay, 30))
            except (OSError, TimeoutError) as exc:
                if attempt == self.max_retries:
                    raise ProviderError(f"{self.name}: {exc}") from exc
                cancellation.wait(.5 * (2 ** attempt) + random.random() * .2)
        if response is None:
            raise AssertionError("unreachable")
        data_lines: list[str] = []

        def close_response() -> None:
            try:
                response.close()
            except Exception:
                # Closing is best-effort and may race with a blocked urllib read.
                pass

        remove_cancel_callback = cancellation.add_cancel_callback(close_response)
        try:
            cancellation.raise_if_cancelled()
            content_type = str(response.headers.get("Content-Type", "")).lower()
            if "text/event-stream" not in content_type:
                try:
                    raw_body = response.read()
                    cancellation.raise_if_cancelled()
                    data = json.loads(raw_body)
                except Exception as exc:
                    cancellation.raise_if_cancelled()
                    raise ProviderError(
                        f"{self.name}: expected an event stream, received {content_type or 'unknown content type'}"
                    ) from exc
                if not isinstance(data, dict):
                    raise ProviderError(f"{self.name}: buffered streaming response must be an object")
                raise BufferedStreamResponse(data)
            while True:
                cancellation.raise_if_cancelled()
                try:
                    raw = response.readline()
                except Exception as exc:
                    # On Windows, closing a chunked HTTPResponse from the cancel
                    # callback may surface as AttributeError from fp.peek().
                    cancellation.raise_if_cancelled()
                    raise ProviderError(f"{self.name}: streaming read failed: {exc}") from exc
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if not line:
                    if data_lines:
                        value = "\n".join(data_lines); data_lines.clear()
                        if value == "[DONE]":
                            break
                        try:
                            yield json.loads(value)
                        except json.JSONDecodeError as exc:
                            raise ProviderError(f"{self.name}: invalid streaming event: {exc}") from exc
                    continue
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
            if data_lines:
                value = "\n".join(data_lines)
                if value != "[DONE]":
                    try:
                        yield json.loads(value)
                    except json.JSONDecodeError as exc:
                        raise ProviderError(f"{self.name}: invalid streaming event: {exc}") from exc
            cancellation.raise_if_cancelled()
        finally:
            remove_cancel_callback()
            close_response()

    def _post(self, url: str, payload: dict[str, Any], headers: dict[str, str],
              cancellation: CancellationToken | None = None) -> dict[str, Any]:
        cancellation = cancellation_or_default(cancellation)
        body = json.dumps(payload).encode()
        for attempt in range(self.max_retries + 1):
            cancellation.raise_if_cancelled()
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", **headers}, method="POST")
            try:
                with urllib.request.urlopen(
                    req, timeout=cancellation.bounded_timeout(self.timeout),
                ) as response:
                    result = json.loads(response.read())
                    cancellation.raise_if_cancelled()
                    return result
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")
                if exc.code not in {408, 409, 429} and exc.code < 500:
                    raise ProviderError(f"{self.name} {exc.code}: {detail}") from exc
                if attempt == self.max_retries:
                    raise ProviderError(f"{self.name} {exc.code}: {detail}") from exc
                retry_after = exc.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else .5 * (2 ** attempt) + random.random() * .2
                cancellation.wait(min(delay, 30))
            except (OSError, TimeoutError) as exc:
                if attempt == self.max_retries:
                    raise ProviderError(f"{self.name}: {exc}") from exc
                cancellation.wait(.5 * (2 ** attempt) + random.random() * .2)
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

    def _payload(self, request: Request, *, stream: bool = False) -> dict[str, Any]:
        messages = []
        for message in request.messages:
            blocks = [value for block in message.content if (value := _anthropic_block(block)) is not None]
            if blocks:
                messages.append({"role": "assistant" if message.role == "assistant" else "user", "content": blocks})
        payload = {
            "model": request.model, "system": request.system, "messages": messages,
            "tools": [{"name": x.name, "description": x.description, "input_schema": x.input_schema} for x in request.tools],
            "max_tokens": request.max_tokens,
        }
        if stream:
            payload["stream"] = True
        return payload

    def complete(self, request: Request,
                 cancellation: CancellationToken | None = None) -> Response:
        data = self._post(
            self.endpoint, self._payload(request),
            {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
            cancellation,
        )
        return _anthropic_response(data)

    def stream(self, request: Request, on_delta: Callable[[StreamDelta], None],
               cancellation: CancellationToken | None = None) -> Response:
        blocks: dict[int, ContentBlock] = {}
        argument_parts: dict[int, list[str]] = {}
        stop_reason = "end_turn"
        usage = Usage()
        try:
            for event in self._iter_sse(
                self.endpoint, self._payload(request, stream=True),
                {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
                cancellation,
            ):
                kind = event.get("type", "")
                if kind == "error":
                    error = event.get("error") or event
                    raise ProviderError(f"anthropic: {error.get('message', error)}")
                if kind == "message_start":
                    current = (event.get("message") or {}).get("usage") or {}
                    usage.input_tokens = current.get("input_tokens", 0)
                    usage.cache_creation_tokens = current.get("cache_creation_input_tokens", 0)
                    usage.cache_read_tokens = current.get("cache_read_input_tokens", 0)
                elif kind == "content_block_start":
                    index = int(event.get("index", len(blocks)))
                    block = event.get("content_block") or {}
                    block_type = block.get("type", "")
                    if block_type == "text":
                        text = str(block.get("text") or "")
                        blocks[index] = ContentBlock("text", text=text)
                        if text:
                            on_delta(StreamDelta("text", text))
                    elif block_type in {"thinking", "redacted_thinking"}:
                        text = str(block.get("thinking") or "")
                        blocks[index] = ContentBlock("commentary", text=text)
                        if text:
                            on_delta(StreamDelta("commentary", text))
                    elif block_type == "tool_use":
                        blocks[index] = ContentBlock(
                            "tool_use", id=str(block.get("id") or ""),
                            name=str(block.get("name") or ""), input=block.get("input") or {},
                        )
                        argument_parts[index] = []
                elif kind == "content_block_delta":
                    index = int(event.get("index", 0)); delta = event.get("delta") or {}
                    delta_type = delta.get("type", "")
                    if delta_type == "text_delta":
                        text = str(delta.get("text") or "")
                        block = blocks.setdefault(index, ContentBlock("text")); block.text += text
                        if text:
                            on_delta(StreamDelta("text", text))
                    elif delta_type in {"thinking_delta", "signature_delta"}:
                        text = str(delta.get("thinking") or "")
                        block = blocks.setdefault(index, ContentBlock("commentary")); block.text += text
                        if text:
                            on_delta(StreamDelta("commentary", text))
                    elif delta_type == "input_json_delta":
                        argument_parts.setdefault(index, []).append(str(delta.get("partial_json") or ""))
                elif kind == "message_delta":
                    delta = event.get("delta") or {}
                    stop_reason = delta.get("stop_reason") or stop_reason
                    current = event.get("usage") or {}
                    usage.output_tokens = current.get("output_tokens", usage.output_tokens)
        except StreamingUnsupported:
            return super().stream(request, on_delta, cancellation)
        except BufferedStreamResponse as exc:
            response = _anthropic_response(exc.data)
            _emit_response_deltas(response, on_delta)
            return response
        for index, parts in argument_parts.items():
            if not parts:
                continue
            previous = blocks[index]
            blocks[index] = _tool_call_block(
                previous.id, previous.name, "".join(parts),
            )
        content = [blocks[index] for index in sorted(blocks)]
        if any(block.type == "tool_use" for block in content):
            stop_reason = "tool_use"
        return Response(content, stop_reason, usage)


def _anthropic_response(data: dict[str, Any]) -> Response:
    if data.get("error"):
        raise ProviderError(f"anthropic: {data['error'].get('message', data['error'])}")
    usage = data.get("usage") or {}
    return Response(
        [ContentBlock.from_dict(x) for x in data.get("content", [])],
        data.get("stop_reason", "end_turn"),
        Usage(
            usage.get("input_tokens", 0), usage.get("output_tokens", 0),
            usage.get("cache_creation_input_tokens", 0),
            usage.get("cache_read_input_tokens", 0),
        ),
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

    def _payload(self, request: Request, *, stream: bool = False) -> dict[str, Any]:
        payload = {
            "model": request.model, "instructions": request.system,
            "input": _openai_input(request.messages),
            "tools": [{"type": "function", "name": x.name, "description": x.description, "parameters": x.input_schema} for x in request.tools],
            "tool_choice": "auto", "max_output_tokens": request.max_tokens,
            "store": False, "include": ["reasoning.encrypted_content"],
        }
        if stream:
            payload["stream"] = True
        return payload

    def complete(self, request: Request,
                 cancellation: CancellationToken | None = None) -> Response:
        data = self._post(
            self.endpoint, self._payload(request),
            {"Authorization": f"Bearer {self.api_key}"}, cancellation,
        )
        return _openai_response(data, self.name)

    def stream(self, request: Request, on_delta: Callable[[StreamDelta], None],
               cancellation: CancellationToken | None = None) -> Response:
        text_parts: list[str] = []
        calls: dict[object, dict[str, str]] = {}
        terminal: dict[str, Any] | None = None
        try:
            events = self._iter_sse(
                self.endpoint, self._payload(request, stream=True),
                {"Authorization": f"Bearer {self.api_key}"}, cancellation,
            )
            for event in events:
                kind = event.get("type", "")
                if kind in {"response.output_text.delta", "response.refusal.delta"}:
                    delta = str(event.get("delta") or "")
                    if delta:
                        text_parts.append(delta); on_delta(StreamDelta("text", delta))
                elif kind in {"response.reasoning_summary_text.delta", "response.reasoning_text.delta"}:
                    delta = str(event.get("delta") or "")
                    if delta:
                        on_delta(StreamDelta("commentary", delta))
                elif kind == "response.output_item.added":
                    item = event.get("item") or {}
                    if item.get("type") == "function_call":
                        key = item.get("id") or event.get("output_index", len(calls))
                        calls[key] = {
                            "id": str(item.get("call_id") or item.get("id") or ""),
                            "name": str(item.get("name") or ""),
                            "arguments": str(item.get("arguments") or ""),
                        }
                elif kind == "response.function_call_arguments.delta":
                    key = event.get("item_id") or event.get("output_index", len(calls))
                    call = calls.setdefault(key, {"id": "", "name": "", "arguments": ""})
                    call["arguments"] += str(event.get("delta") or "")
                elif kind == "response.function_call_arguments.done":
                    key = event.get("item_id") or event.get("output_index", len(calls))
                    call = calls.setdefault(key, {"id": "", "name": "", "arguments": ""})
                    call["name"] = str(event.get("name") or call["name"])
                    call["arguments"] = str(event.get("arguments") or call["arguments"])
                elif kind in {"response.completed", "response.incomplete"}:
                    terminal = event.get("response") or {}
                elif kind in {"response.failed", "error"}:
                    error = event.get("error") or (event.get("response") or {}).get("error") or event
                    raise ProviderError(
                        f"{self.name}: "
                        f"{error.get('message', error) if isinstance(error, dict) else error}"
                    )
        except StreamingUnsupported:
            return super().stream(request, on_delta, cancellation)
        except BufferedStreamResponse as exc:
            response = _openai_response(exc.data, self.name)
            _emit_response_deltas(response, on_delta)
            return response
        if terminal is not None:
            return _openai_response(terminal, self.name)
        blocks = [ContentBlock("text", text="".join(text_parts))] if text_parts else []
        for call in calls.values():
            blocks.append(_tool_call_block(
                call["id"], call["name"], call["arguments"],
            ))
        return Response(blocks, "tool_use" if calls else "end_turn")


def _openai_response(data: dict[str, Any], name: str = "openai") -> Response:
    if data.get("error"):
        raise ProviderError(f"{name}: {data['error'].get('message', data['error'])}")
    blocks: list[ContentBlock] = []
    saw_tool = False
    for item in data.get("output", []):
        if item.get("type") == "message":
            text = "".join(
                x.get("text") or x.get("refusal") or ""
                for x in item.get("content", [])
                if x.get("type") in {"output_text", "refusal"}
            )
            if text:
                blocks.append(ContentBlock("text", text=text))
        elif item.get("type") == "function_call":
            saw_tool = True
            blocks.append(_tool_call_block(
                item.get("call_id", ""), item.get("name", ""),
                item.get("arguments") or "{}",
            ))
        elif item.get("type") == "reasoning":
            blocks.append(ContentBlock("raw", raw=item))
    usage = data.get("usage") or {}
    details = usage.get("input_tokens_details") or {}
    status = data.get("status", "completed")
    stop = "tool_use" if saw_tool else (
        "max_tokens" if status == "incomplete" else "end_turn"
    )
    return Response(
        blocks, stop,
        Usage(
            usage.get("input_tokens", 0), usage.get("output_tokens", 0),
            0, details.get("cached_tokens", 0),
        ),
    )


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


def _chat_stop_reason(choice: dict[str, Any], message: dict[str, Any]) -> str:
    """Normalize Chat Completions finish reasons to HAL's internal protocol."""
    finish_reason = choice.get("finish_reason", "")
    if message.get("tool_calls") or finish_reason == "tool_calls":
        return "tool_use"
    if finish_reason == "length":
        return "max_tokens"
    return "end_turn"


class OpenRouterProvider(HTTPProvider):
    name = "openrouter"

    def __init__(self, api_key: str, endpoint: str = "https://openrouter.ai/api/v1/chat/completions",
                 max_tokens_parameter: str = "max_tokens") -> None:
        self.api_key, self.endpoint = api_key, endpoint
        self.max_tokens_parameter = max_tokens_parameter

    def _payload(self, request: Request, *, stream: bool = False) -> dict[str, Any]:
        messages = [{"role": "system", "content": request.system}, *_chat_messages(request.messages)]
        payload = {
            "model": request.model, "messages": messages,
            "tools": [{"type": "function", "function": {"name": x.name, "description": x.description, "parameters": x.input_schema}} for x in request.tools],
            self.max_tokens_parameter: request.max_tokens,
        }
        if stream:
            payload["stream"] = True
        return payload

    def complete(self, request: Request,
                 cancellation: CancellationToken | None = None) -> Response:
        data = self._post(
            self.endpoint, self._payload(request), {"Authorization": f"Bearer {self.api_key}"},
            cancellation,
        )
        return _chat_response(self.name, data)

    def stream(self, request: Request, on_delta: Callable[[StreamDelta], None],
               cancellation: CancellationToken | None = None) -> Response:
        text_parts: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        finish_reason = ""
        usage: dict[str, Any] = {}
        try:
            for event in self._iter_sse(
                self.endpoint, self._payload(request, stream=True),
                {"Authorization": f"Bearer {self.api_key}"}, cancellation,
            ):
                if event.get("error"):
                    error = event["error"]
                    raise ProviderError(f"{self.name}: {error.get('message', error) if isinstance(error, dict) else error}")
                usage = event.get("usage") or usage
                for choice in event.get("choices") or []:
                    delta = choice.get("delta") or {}
                    content = delta.get("content")
                    if isinstance(content, str) and content:
                        text_parts.append(content); on_delta(StreamDelta("text", content))
                    commentary = delta.get("reasoning_content") or delta.get("reasoning")
                    if isinstance(commentary, str) and commentary:
                        on_delta(StreamDelta("commentary", commentary))
                    for item in delta.get("tool_calls") or []:
                        index = int(item.get("index", len(calls)))
                        call = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                        fn = item.get("function") or {}
                        call["id"] += str(item.get("id") or "")
                        call["name"] += str(fn.get("name") or "")
                        call["arguments"] += str(fn.get("arguments") or "")
                    finish_reason = choice.get("finish_reason") or finish_reason
        except StreamingUnsupported:
            return super().stream(request, on_delta, cancellation)
        except BufferedStreamResponse as exc:
            response = _chat_response(self.name, exc.data)
            _emit_response_deltas(response, on_delta)
            return response
        blocks = [ContentBlock("text", text="".join(text_parts))] if text_parts else []
        for call in (calls[index] for index in sorted(calls)):
            blocks.append(_tool_call_block(
                call["id"], call["name"], call["arguments"],
            ))
        stop = "tool_use" if calls or finish_reason == "tool_calls" else (
            "max_tokens" if finish_reason == "length" else "end_turn"
        )
        return Response(
            blocks, stop,
            Usage(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)),
        )


def _chat_response(name: str, data: dict[str, Any]) -> Response:
    if data.get("error"):
        raise ProviderError(f"{name}: {data['error'].get('message', data['error'])}")
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    blocks = [ContentBlock("text", text=message["content"])] if message.get("content") else []
    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        blocks.append(_tool_call_block(
            call.get("id", ""), fn.get("name", ""),
            fn.get("arguments") or "{}",
        ))
    usage = data.get("usage") or {}
    return Response(
        blocks,
        _chat_stop_reason(choice, message),
        Usage(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)),
    )


class OpenAICompatibleProvider(OpenRouterProvider):
    """OpenAI-compatible Chat Completions provider at a custom API base."""

    def __init__(self, name: str, api_key: str, api_base: str,
                 max_tokens_parameter: str = "max_tokens") -> None:
        if not api_base:
            raise ProviderError(f"{name}: api_base is required")
        self.name = name
        super().__init__(api_key, f"{api_base.rstrip('/')}/chat/completions", max_tokens_parameter)


class MetaProvider(OpenAIProvider):
    """Muse Spark through Meta Model API's Responses-compatible surface."""

    name = "meta"
    DEFAULT_API_BASE = "https://api.meta.ai/v1"

    def __init__(self, api_key: str, api_base: str = "") -> None:
        base = (
            api_base or os.environ.get("META_API_BASE_URL", "").strip()
            or self.DEFAULT_API_BASE
        )
        super().__init__(api_key, f"{base.rstrip('/')}/responses")

    def _payload(self, request: Request, *, stream: bool = False) -> dict[str, Any]:
        payload = super()._payload(request, stream=stream)
        # These OpenAI-specific persistence and encrypted-reasoning options are
        # not part of Meta's documented Responses request surface.
        payload.pop("store", None)
        payload.pop("include", None)
        if not request.tools:
            payload.pop("tools", None)
            payload.pop("tool_choice", None)
        return payload


class GoogleProvider(HTTPProvider):
    name = "google"

    def __init__(self, api_key: str, endpoint: str = "https://generativelanguage.googleapis.com/v1beta/models") -> None:
        self.api_key, self.endpoint = api_key, endpoint

    def _payload(self, request: Request) -> dict[str, Any]:
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
        return {
            "systemInstruction": {"parts": [{"text": request.system}]}, "contents": contents,
            "tools": [{"functionDeclarations": [{"name": x.name, "description": x.description, "parameters": x.input_schema} for x in request.tools]}],
            "generationConfig": {"maxOutputTokens": request.max_tokens},
        }

    def complete(self, request: Request,
                 cancellation: CancellationToken | None = None) -> Response:
        url = f"{self.endpoint.rstrip('/')}/{urllib.parse.quote(request.model, safe='')}:generateContent"
        data = self._post(url, self._payload(request), {"x-goog-api-key": self.api_key}, cancellation)
        return _google_response(data)

    def stream(self, request: Request, on_delta: Callable[[StreamDelta], None],
               cancellation: CancellationToken | None = None) -> Response:
        model = urllib.parse.quote(request.model, safe="")
        url = f"{self.endpoint.rstrip('/')}/{model}:streamGenerateContent?alt=sse"
        blocks: list[ContentBlock] = []
        finish = "STOP"
        usage: dict[str, Any] = {}
        try:
            for event in self._iter_sse(
                url, self._payload(request), {"x-goog-api-key": self.api_key}, cancellation,
            ):
                if event.get("error"):
                    error = event["error"]
                    raise ProviderError(f"google: {error.get('message', error)}")
                usage = event.get("usageMetadata") or usage
                candidates = event.get("candidates") or []
                if not candidates:
                    continue
                candidate = candidates[0]
                finish = candidate.get("finishReason") or finish
                for index, part in enumerate((candidate.get("content") or {}).get("parts") or []):
                    if part.get("text"):
                        text = str(part["text"])
                        blocks.append(ContentBlock("text", text=text, raw=part))
                        on_delta(StreamDelta("text", text))
                    if part.get("functionCall"):
                        call = part["functionCall"]
                        blocks.append(ContentBlock(
                            "tool_use", id=call.get("id") or f"{call.get('name', 'tool')}_{len(blocks) + index}",
                            name=call.get("name", ""), input=call.get("args") or {}, raw=part,
                        ))
        except StreamingUnsupported:
            return super().stream(request, on_delta, cancellation)
        except BufferedStreamResponse as exc:
            response = _google_response(exc.data)
            _emit_response_deltas(response, on_delta)
            return response
        merged: list[ContentBlock] = []
        for block in blocks:
            if block.type == "text" and merged and merged[-1].type == "text":
                merged[-1].text += block.text
            else:
                merged.append(block)
        return _google_blocks_response(merged, finish, usage)


def _google_response(data: dict[str, Any]) -> Response:
    if data.get("error"):
        raise ProviderError(f"google: {data['error'].get('message', data['error'])}")
    candidates = data.get("candidates") or []
    if not candidates:
        raise ProviderError("google: no candidates returned")
    candidate = candidates[0]
    blocks = []
    for index, part in enumerate((candidate.get("content") or {}).get("parts") or []):
        if part.get("text"):
            blocks.append(ContentBlock("text", text=part["text"], raw=part))
        if part.get("functionCall"):
            call = part["functionCall"]
            blocks.append(ContentBlock(
                "tool_use", id=call.get("id") or f"{call.get('name', 'tool')}_{index}",
                name=call.get("name", ""), input=call.get("args") or {}, raw=part,
            ))
    usage = data.get("usageMetadata") or {}
    finish = candidate.get("finishReason", "STOP")
    return _google_blocks_response(blocks, finish, usage)


def _google_blocks_response(blocks: list[ContentBlock], finish: str,
                            usage: dict[str, Any]) -> Response:
    return Response(
        blocks,
        "tool_use" if any(x.type == "tool_use" for x in blocks) else (
            "max_tokens" if finish == "MAX_TOKENS" else "end_turn"
        ),
        Usage(
            usage.get("promptTokenCount", 0),
            usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0),
            0, usage.get("cachedContentTokenCount", 0),
        ),
    )


def _emit_response_deltas(response: Response,
                          on_delta: Callable[[StreamDelta], None]) -> None:
    for block in response.content:
        if block.type in {"text", "commentary"} and block.text:
            on_delta(StreamDelta(block.type, block.text))


def create_provider(config: Config) -> Provider:
    profile = config.active_profile()
    backend = config.backend()
    if backend == "openai" and config.openai_auth == "subscription" and profile is None:
        raise ProviderError("OpenAI subscription auth is not supported by the Python port; use openai_auth: api_key")
    env_name = config.credential_env()
    key = config.credential()
    if not key: raise ProviderError(f"no API key is configured; set api_key or {env_name}")
    if profile:
        api_base = config.api_base()
        if not api_base:
            source = profile.api_base_env or "api_base"
            raise ProviderError(f"{profile.name}: API endpoint is not configured; set {source}")
        if profile.protocol == "chat_completions":
            provider = OpenAICompatibleProvider(
                profile.name, key, api_base, profile.max_tokens_parameter
            )
        elif backend == "openai":
            provider = OpenAIProvider(key, f"{api_base}/responses")
        else:
            raise ProviderError(f"{profile.name}: unsupported provider protocol")
    else:
        classes = {
            "anthropic": AnthropicProvider, "openai": OpenAIProvider,
            "openrouter": OpenRouterProvider, "google": GoogleProvider,
            "meta": MetaProvider,
        }
        provider = classes[backend](key)
    provider.streaming_enabled = config.streaming
    return provider
