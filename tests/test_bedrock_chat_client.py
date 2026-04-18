import asyncio

from app.infra import bedrock_chat_client


def test_to_bedrock_payload_injects_placeholder_when_no_conversation_messages():
    system_blocks, convo_messages = bedrock_chat_client._to_bedrock_payload(
        [
            {"role": "system", "content": "rules"},
            {"role": "tool", "content": "ignored"},
        ]
    )

    assert system_blocks == [{"text": "rules"}]
    assert convo_messages == [{"role": "user", "content": [{"text": " "}]}]


def test_to_bedrock_payload_appends_user_turn_for_terminal_assistant_when_required():
    system_blocks, convo_messages = bedrock_chat_client._to_bedrock_payload(
        [
            {"role": "system", "content": "rules"},
            {"role": "assistant", "content": "draft answer"},
        ],
        require_terminal_user_message=True,
    )

    assert system_blocks == [{"text": "rules"}]
    assert convo_messages[0] == {"role": "assistant", "content": [{"text": "draft answer"}]}
    assert convo_messages[-1]["role"] == "user"
    assert "Revise the answer" in convo_messages[-1]["content"][0]["text"]


def test_from_bedrock_response_parses_text_and_usage_defaults():
    response = bedrock_chat_client._from_bedrock_response(
        {
            "output": {"message": {"content": [{"text": "hello"}, {"text": "world"}]}},
            "usage": {"inputTokens": 3, "outputTokens": 4},
        }
    )

    assert response.choices[0].message.content == "hello\nworld"
    assert response.usage.prompt_tokens == 3
    assert response.usage.completion_tokens == 4
    assert response.usage.total_tokens == 7


def test_from_bedrock_response_appends_citation_sources(monkeypatch):
    monkeypatch.setattr(
        bedrock_chat_client.settings.bedrock,
        "web_grounding_include_sources",
        True,
    )
    response = bedrock_chat_client._from_bedrock_response(
        {
            "output": {
                "message": {
                    "content": [
                        {
                            "text": "Recent update",
                            "citationsContent": {
                                "citations": [
                                    {
                                        "location": {
                                            "web": {"url": "https://example.com/news"},
                                        }
                                    }
                                ]
                            },
                        }
                    ]
                }
            },
            "usage": {"inputTokens": 3, "outputTokens": 4},
        }
    )

    assert "Recent update" in response.choices[0].message.content
    assert "Sources:" in response.choices[0].message.content
    assert "https://example.com/news" in response.choices[0].message.content


def test_create_injects_nova_grounding_tool_config(monkeypatch):
    captured = {}

    async def fake_aconverse(payload):
        captured["payload"] = payload
        return {
            "output": {"message": {"content": [{"text": "ok"}]}},
            "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
        }

    class _NoopLimiter:
        async def __aenter__(self):
            return None

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(bedrock_chat_client, "aconverse", fake_aconverse)
    monkeypatch.setattr(bedrock_chat_client, "dependency_limiter", lambda _name: _NoopLimiter())

    async def _call():
        await bedrock_chat_client.client.chat.completions.create(
            model="us.amazon.nova-2-lite-v1:0",
            messages=[{"role": "user", "content": "hi"}],
            enable_web_grounding=True,
        )

    asyncio.run(_call())
    assert captured["payload"]["toolConfig"] == {
        "tools": [{"systemTool": {"name": "nova_grounding"}}]
    }


def test_stream_filters_empty_deltas(monkeypatch):
    async def fake_stream_text(_payload):
        yield ""
        yield "a"
        yield "b"

    class _NoopLimiter:
        async def __aenter__(self):
            return None

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(bedrock_chat_client, "aconverse_stream_text", fake_stream_text)
    monkeypatch.setattr(bedrock_chat_client, "dependency_limiter", lambda _name: _NoopLimiter())

    async def _collect():
        chunks = []
        async for chunk in bedrock_chat_client.client.chat.completions.stream(
            model="model-id",
            messages=[{"role": "user", "content": "hi"}],
        ):
            chunks.append(chunk)
        return chunks

    assert asyncio.run(_collect()) == ["a", "b"]
