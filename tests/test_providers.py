import io
import json
import threading
import time
import urllib.error

import pytest

from hal.cancellation import CancelledError, CancellationToken
from hal.config import parse_config
from hal.models import Message, ContentBlock, Request
from hal.providers import (
    AnthropicProvider,
    GoogleProvider,
    OpenAICompatibleProvider,
    MetaProvider,
    OpenAIProvider,
    OpenRouterProvider,
    ProviderError,
    StreamingUnsupported,
    create_provider,
)


def test_custom_openai_profile_uses_chat_completions_endpoint(monkeypatch) -> None:
    monkeypatch.setenv("ENTERPRISE_LLM_TOKEN", "placeholder-token")
    monkeypatch.setenv("ENTERPRISE_LLM_BASE_URL", "https://example.test/openai/v1")
    config = parse_config({
        "provider": "enterprise",
        "providers": [{
            "id": "enterprise", "name": "Example Enterprise GPT", "provider": "openai",
            "model": "internal-model", "api_base_env": "ENTERPRISE_LLM_BASE_URL",
            "api_key_env": "ENTERPRISE_LLM_TOKEN",
        }],
    })
    provider = create_provider(config)
    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.endpoint == "https://example.test/openai/v1/chat/completions"

    captured = {}
    def fake_post(url, payload, headers, cancellation=None):
        captured.update(url=url, payload=payload, headers=headers)
        return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {}}
    monkeypatch.setattr(provider, "_post", fake_post)
    response = provider.complete(Request("internal-model", "system", [Message("user", [ContentBlock("text", text="hello")])], []))
    assert response.content[0].text == "ok"
    assert response.stop_reason == "end_turn"
    assert captured["url"].endswith("/chat/completions")
    assert captured["headers"]["Authorization"] == "Bearer placeholder-token"


def test_provider_respects_global_streaming_disable() -> None:
    config = parse_config({
        "provider": "openrouter", "api_key": "placeholder",
        "features": {"streaming": False},
    })

    assert create_provider(config).streaming_enabled is False


def test_meta_provider_uses_responses_endpoint_and_bearer_key(monkeypatch) -> None:
    monkeypatch.setenv("META_API_KEY", "meta-placeholder")
    monkeypatch.delenv("META_API_BASE_URL", raising=False)
    config = parse_config({
        "provider": "meta", "model": "muse-spark-1.2-contributor",
    })
    provider = create_provider(config)
    assert isinstance(provider, MetaProvider)
    assert provider.endpoint == "https://api.meta.ai/v1/responses"

    captured = {}
    def fake_post(url, payload, headers, cancellation=None):
        captured.update(url=url, payload=payload, headers=headers)
        return {
            "status": "completed",
            "output": [{
                "type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": "ok"}],
            }],
            "usage": {"input_tokens": 3, "output_tokens": 1},
        }

    monkeypatch.setattr(provider, "_post", fake_post)
    response = provider.complete(Request(
        config.model, "system",
        [Message("user", [ContentBlock("text", text="hello")])], [],
    ))

    assert response.content[0].text == "ok"
    assert captured["payload"]["model"] == "muse-spark-1.2-contributor"
    assert captured["payload"]["input"] == [{
        "type": "message", "role": "user",
        "content": [{"type": "input_text", "text": "hello"}],
    }]
    assert "store" not in captured["payload"]
    assert "include" not in captured["payload"]
    assert "reasoning" not in captured["payload"]
    assert "tools" not in captured["payload"]
    assert captured["headers"] == {"Authorization": "Bearer meta-placeholder"}


def test_meta_provider_sends_responses_reasoning_effort(monkeypatch) -> None:
    provider = MetaProvider("meta-placeholder")
    captured = {}

    def fake_post(url, payload, headers, cancellation=None):
        captured.update(payload)
        return {"status": "completed", "output": [], "usage": {}}

    monkeypatch.setattr(provider, "_post", fake_post)
    provider.complete(Request(
        "muse-spark-1.2-contributor", "system",
        [Message("user", [ContentBlock("text", text="hello")])], [],
        reasoning_effort="xhigh",
    ))

    assert captured["reasoning"] == {"effort": "xhigh"}


def test_meta_provider_allows_preview_endpoint_override(monkeypatch) -> None:
    monkeypatch.setenv("META_API_KEY", "meta-placeholder")
    monkeypatch.setenv("META_API_BASE_URL", "https://preview.example.test/openai/v1/")

    provider = create_provider(parse_config({"provider": "meta"}))

    assert isinstance(provider, MetaProvider)
    assert provider.endpoint == "https://preview.example.test/openai/v1/responses"


def test_custom_profile_reports_missing_endpoint_environment_variable(monkeypatch) -> None:
    monkeypatch.setenv("ENTERPRISE_LLM_TOKEN", "placeholder-token")
    monkeypatch.delenv("ENTERPRISE_LLM_BASE_URL", raising=False)
    config = parse_config({
        "provider": "enterprise",
        "providers": [{
            "id": "enterprise", "name": "Enterprise", "provider": "openai",
            "model": "internal-model", "api_base_env": "ENTERPRISE_LLM_BASE_URL",
            "api_key_env": "ENTERPRISE_LLM_TOKEN",
        }],
    })

    with pytest.raises(ProviderError, match="set ENTERPRISE_LLM_BASE_URL"):
        create_provider(config)


def test_custom_profile_accepts_inline_key(monkeypatch) -> None:
    monkeypatch.delenv("ENTERPRISE_LLM_TOKEN", raising=False)
    config = parse_config({
        "provider": "enterprise",
        "providers": [{
            "id": "enterprise", "name": "Enterprise", "provider": "openai",
            "model": "internal-model", "apiBase": "https://example.test/v1",
            "api_key": "inline-placeholder",
        }],
    })
    provider = create_provider(config)
    assert provider.api_key == "inline-placeholder"


def test_custom_profile_sends_max_completion_tokens(monkeypatch) -> None:
    config = parse_config({
        "provider": "enterprise",
        "providers": [{
            "id": "enterprise", "name": "Enterprise", "provider": "openai",
            "model": "gpt-5.1", "apiBase": "https://example.test/v1",
            "api_key": "placeholder", "max_tokens_parameter": "max_completion_tokens",
        }],
    })
    provider = create_provider(config)
    captured = {}

    def fake_post(url, payload, headers, cancellation=None):
        captured.update(payload)
        return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {}}

    monkeypatch.setattr(provider, "_post", fake_post)
    provider.complete(Request("gpt-5.1", "system", [Message("user", [ContentBlock("text", text="hello")])], [], max_tokens=321))
    assert captured["max_completion_tokens"] == 321
    assert "max_tokens" not in captured


def test_chat_completions_normalizes_length_to_max_tokens(monkeypatch) -> None:
    provider = OpenRouterProvider("placeholder")
    monkeypatch.setattr(provider, "_post", lambda *_args: {
        "choices": [{"message": {"content": "partial"}, "finish_reason": "length"}],
        "usage": {},
    })

    response = provider.complete(Request(
        "model", "system", [Message("user", [ContentBlock("text", text="hello")])], [],
    ))

    assert response.stop_reason == "max_tokens"


def test_chat_completions_normalizes_tool_calls(monkeypatch) -> None:
    provider = OpenRouterProvider("placeholder")
    monkeypatch.setattr(provider, "_post", lambda *_args: {
        "choices": [{
            "message": {"tool_calls": [{
                "id": "call-1", "type": "function",
                "function": {"name": "read_file", "arguments": '{"path":"README.md"}'},
            }]},
            "finish_reason": "tool_calls",
        }],
        "usage": {},
    })

    response = provider.complete(Request(
        "model", "system", [Message("user", [ContentBlock("text", text="hello")])], [],
    ))

    assert response.stop_reason == "tool_use"
    assert response.content[0].name == "read_file"


def test_chat_completions_preserves_valid_arguments_unchanged(monkeypatch) -> None:
    provider = OpenRouterProvider("placeholder")
    arguments = {"command": "printf ok", "timeout": 3}
    monkeypatch.setattr(provider, "_post", lambda *_args: {
        "choices": [{
            "message": {"tool_calls": [{
                "id": "call-1", "type": "function",
                "function": {"name": "bash", "arguments": json.dumps(arguments)},
            }]},
            "finish_reason": "tool_calls",
        }],
        "usage": {},
    })

    response = provider.complete(_request())

    assert response.content[0].input == arguments
    assert response.content[0].argument_error == ""


def test_chat_completions_returns_malformed_arguments_for_safe_retry(monkeypatch) -> None:
    provider = OpenRouterProvider("placeholder")
    monkeypatch.setattr(provider, "_post", lambda *_args: {
        "choices": [{
            "message": {"tool_calls": [{
                "id": "call-1", "type": "function",
                "function": {"name": "bash", "arguments": '{"command": "git'},
            }]},
            "finish_reason": "tool_calls",
        }],
        "usage": {},
    })

    response = provider.complete(_request())

    call = response.content[0]
    assert call.type == "tool_use"
    assert call.input == {}
    assert "was not executed: invalid JSON arguments" in call.argument_error


def test_retry_after_wait_respects_cancellation_deadline(monkeypatch) -> None:
    provider = OpenRouterProvider("placeholder")
    error = urllib.error.HTTPError(
        provider.endpoint, 429, "busy", {"Retry-After": "30"}, io.BytesIO(b"busy"),
    )
    monkeypatch.setattr("hal.providers.urllib.request.urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(error))
    started = time.monotonic()

    with pytest.raises(CancelledError, match="timed out"):
        provider._post(
            provider.endpoint, {}, {}, CancellationToken.with_timeout(.01),
        )

    assert time.monotonic() - started < 1


def _request(model: str = "model") -> Request:
    return Request(model, "system", [Message("user", [ContentBlock("text", text="hello")])], [])


def test_chat_completions_streams_text_tool_arguments_and_usage(monkeypatch) -> None:
    provider = OpenAICompatibleProvider(
        "enterprise", "placeholder", "https://example.test/v1",
        "max_completion_tokens",
    )
    captured = {}

    def events(url, payload, headers, cancellation=None):
        captured.update(url=url, payload=payload, headers=headers)
        yield {"choices": [{"delta": {"content": "Hel"}, "finish_reason": None}]}
        yield {"choices": [{"delta": {"content": "lo"}, "finish_reason": None}]}
        yield {"choices": [{"delta": {"tool_calls": [{
            "index": 0, "id": "call-1",
            "function": {"name": "read_file", "arguments": '{"path":'},
        }]}, "finish_reason": None}]}
        yield {"choices": [{"delta": {"tool_calls": [{
            "index": 0, "function": {"arguments": '"README.md"}'},
        }]}, "finish_reason": "tool_calls"}], "usage": {
            "prompt_tokens": 10, "completion_tokens": 3,
        }}

    monkeypatch.setattr(provider, "_iter_sse", events)
    deltas = []
    response = provider.stream(_request("gpt-5.1"), deltas.append)

    assert [delta.text for delta in deltas] == ["Hel", "lo"]
    assert response.content[0].text == "Hello"
    assert response.content[1].input == {"path": "README.md"}
    assert response.stop_reason == "tool_use"
    assert response.usage.input_tokens == 10
    assert captured["payload"]["stream"] is True
    assert captured["payload"]["max_completion_tokens"] == 8192


def test_chat_stream_returns_malformed_arguments_for_safe_retry(monkeypatch) -> None:
    provider = OpenRouterProvider("placeholder")

    def events(*_args, **_kwargs):
        yield {"choices": [{"delta": {"tool_calls": [{
            "index": 0, "id": "call-1",
            "function": {"name": "bash", "arguments": '{"command":'},
        }]}, "finish_reason": "tool_calls"}]}

    monkeypatch.setattr(provider, "_iter_sse", events)
    response = provider.stream(_request(), lambda _delta: None)

    assert response.stop_reason == "tool_use"
    assert "was not executed" in response.content[0].argument_error


def test_chat_stream_rejection_falls_back_to_buffered_completion(monkeypatch) -> None:
    provider = OpenAICompatibleProvider("enterprise", "placeholder", "https://example.test/v1")

    def unsupported(*_args, **_kwargs):
        raise StreamingUnsupported("not supported")
        yield  # pragma: no cover

    monkeypatch.setattr(provider, "_iter_sse", unsupported)
    monkeypatch.setattr(provider, "_post", lambda *_args, **_kwargs: {
        "choices": [{"message": {"content": "buffered"}, "finish_reason": "stop"}],
        "usage": {},
    })
    deltas = []

    response = provider.stream(_request("gpt-5.1"), deltas.append)

    assert response.content[0].text == "buffered"
    assert [delta.text for delta in deltas] == ["buffered"]


def test_chat_gateway_that_ignores_stream_flag_uses_same_buffered_response(monkeypatch) -> None:
    provider = OpenAICompatibleProvider("enterprise", "placeholder", "https://example.test/v1")

    class BufferedResponse:
        headers = {"Content-Type": "application/json"}

        def readline(self):
            raise AssertionError("SSE reader should not be used")

        def read(self):
            return json.dumps({
                "choices": [{"message": {"content": "same request"}, "finish_reason": "stop"}],
                "usage": {},
            }).encode()

        def close(self):
            pass

    monkeypatch.setattr("hal.providers.urllib.request.urlopen", lambda *_args, **_kwargs: BufferedResponse())
    monkeypatch.setattr(
        provider, "_post",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("duplicate request")),
    )
    deltas = []

    response = provider.stream(_request("gpt-5.1"), deltas.append)

    assert response.content[0].text == "same request"
    assert [delta.text for delta in deltas] == ["same request"]


def test_stream_cancellation_closes_blocking_http_response(monkeypatch) -> None:
    provider = OpenRouterProvider("placeholder")
    closed = threading.Event()

    class BlockingResponse:
        headers = {"Content-Type": "text/event-stream"}

        def readline(self):
            closed.wait(5)
            return b""

        def close(self):
            closed.set()

    monkeypatch.setattr("hal.providers.urllib.request.urlopen", lambda *_args, **_kwargs: BlockingResponse())
    cancellation = CancellationToken()
    timer = threading.Timer(.02, lambda: cancellation.cancel("cancel stream"))
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(CancelledError, match="cancel stream"):
            list(provider._iter_sse(provider.endpoint, {"stream": True}, {}, cancellation))
    finally:
        timer.cancel()

    assert closed.is_set()
    assert time.monotonic() - started < 1


def test_stream_cancellation_normalizes_windows_chunked_peek_race(monkeypatch) -> None:
    provider = OpenRouterProvider("placeholder")
    closed = threading.Event()

    class RacingResponse:
        headers = {"Content-Type": "text/event-stream"}

        def readline(self):
            closed.wait(5)
            raise AttributeError("'NoneType' object has no attribute 'peek'")

        def close(self):
            closed.set()

    monkeypatch.setattr("hal.providers.urllib.request.urlopen", lambda *_args, **_kwargs: RacingResponse())
    cancellation = CancellationToken()
    timer = threading.Timer(.02, lambda: cancellation.cancel("cancel stream"))
    timer.start()
    try:
        with pytest.raises(CancelledError, match="cancel stream"):
            list(provider._iter_sse(provider.endpoint, {"stream": True}, {}, cancellation))
    finally:
        timer.cancel()

    assert closed.is_set()


def test_sse_parser_ignores_comments_and_stops_at_done(monkeypatch) -> None:
    provider = OpenRouterProvider("placeholder")

    class EventStream(io.BytesIO):
        headers = {"Content-Type": "text/event-stream; charset=utf-8"}

        def close(self):
            # Keep BytesIO readable long enough for the assertion path.
            pass

    stream = EventStream(
        b": keepalive\n\n"
        b"data: {\"choices\":[{\"delta\":{\"content\":\"Hi\"}}]}\n\n"
        b"data: [DONE]\n\n"
        b"data: {\"ignored\":true}\n\n"
    )
    monkeypatch.setattr("hal.providers.urllib.request.urlopen", lambda *_args, **_kwargs: stream)

    events = list(provider._iter_sse(provider.endpoint, {"stream": True}, {}))

    assert events == [{"choices": [{"delta": {"content": "Hi"}}]}]


def test_chat_stream_does_not_duplicate_request_after_partial_failure(monkeypatch) -> None:
    provider = OpenRouterProvider("placeholder")

    def partial(*_args, **_kwargs):
        yield {"choices": [{"delta": {"content": "partial"}}]}
        raise ProviderError("connection lost")

    monkeypatch.setattr(provider, "_iter_sse", partial)
    monkeypatch.setattr(
        provider, "_post",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("duplicate request")),
    )
    deltas = []

    with pytest.raises(ProviderError, match="connection lost"):
        provider.stream(_request(), deltas.append)

    assert [delta.text for delta in deltas] == ["partial"]


def test_responses_api_stream_normalizes_deltas_and_terminal_response(monkeypatch) -> None:
    provider = OpenAIProvider("placeholder")

    def events(*_args, **_kwargs):
        yield {"type": "response.output_text.delta", "delta": "Hi"}
        yield {"type": "response.output_text.delta", "delta": " there"}
        yield {"type": "response.completed", "response": {
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "Hi there"}]}],
            "usage": {"input_tokens": 4, "output_tokens": 2},
        }}

    monkeypatch.setattr(provider, "_iter_sse", events)
    deltas = []
    response = provider.stream(_request(), deltas.append)

    assert [delta.text for delta in deltas] == ["Hi", " there"]
    assert response.content[0].text == "Hi there"
    assert response.usage.output_tokens == 2


def test_anthropic_stream_normalizes_text_and_fragmented_tool_input(monkeypatch) -> None:
    provider = AnthropicProvider("placeholder")

    def events(*_args, **_kwargs):
        yield {"type": "message_start", "message": {"usage": {"input_tokens": 5}}}
        yield {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}
        yield {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello"}}
        yield {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "call-1", "name": "read_file", "input": {}}}
        yield {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"path":"README.md"}'}}
        yield {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 3}}

    monkeypatch.setattr(provider, "_iter_sse", events)
    deltas = []
    response = provider.stream(_request(), deltas.append)

    assert [delta.text for delta in deltas] == ["Hello"]
    assert response.content[1].input == {"path": "README.md"}
    assert response.usage.input_tokens == 5
    assert response.usage.output_tokens == 3


def test_google_stream_normalizes_incremental_candidates(monkeypatch) -> None:
    provider = GoogleProvider("placeholder")

    def events(*_args, **_kwargs):
        yield {"candidates": [{"content": {"parts": [{"text": "Hello "}]}}]}
        yield {
            "candidates": [{"content": {"parts": [{"text": "world"}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 2},
        }

    monkeypatch.setattr(provider, "_iter_sse", events)
    deltas = []
    response = provider.stream(_request(), deltas.append)

    assert [delta.text for delta in deltas] == ["Hello ", "world"]
    assert response.content[0].text == "Hello world"
    assert response.usage.output_tokens == 2
