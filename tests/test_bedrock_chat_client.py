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
