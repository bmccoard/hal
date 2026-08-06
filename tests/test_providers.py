from neo.config import parse_config
from neo.models import Message, ContentBlock, Request
from neo.providers import OpenAICompatibleProvider, create_provider


def test_custom_openai_profile_uses_chat_completions_endpoint(monkeypatch) -> None:
    monkeypatch.setenv("ENTERPRISE_LLM_TOKEN", "placeholder-token")
    config = parse_config({
        "provider": "enterprise",
        "providers": [{
            "id": "enterprise", "name": "Example Enterprise GPT", "provider": "openai",
            "model": "internal-model", "apiBase": "https://example.test/openai/v1",
            "api_key_env": "ENTERPRISE_LLM_TOKEN",
        }],
    })
    provider = create_provider(config)
    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.endpoint == "https://example.test/openai/v1/chat/completions"

    captured = {}
    def fake_post(url, payload, headers):
        captured.update(url=url, payload=payload, headers=headers)
        return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {}}
    monkeypatch.setattr(provider, "_post", fake_post)
    response = provider.complete(Request("internal-model", "system", [Message("user", [ContentBlock("text", text="hello")])], []))
    assert response.content[0].text == "ok"
    assert captured["url"].endswith("/chat/completions")
    assert captured["headers"]["Authorization"] == "Bearer placeholder-token"
