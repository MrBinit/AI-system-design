from dataclasses import dataclass
from typing import Any, AsyncIterator

from app.core.config import get_settings
from app.infra.bedrock_client import aconverse, aconverse_stream_text
from app.infra.io_limiters import dependency_limiter

settings = get_settings()


@dataclass
class _CompatMessage:
    content: str


@dataclass
class _CompatChoice:
    message: _CompatMessage


@dataclass
class _CompatUsage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclass
class _CompatResponse:
    choices: list[_CompatChoice]
    usage: _CompatUsage


def _to_bedrock_payload(
    messages: list[dict[str, Any]],
    *,
    require_terminal_user_message: bool = False,
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    """Convert role/content chat messages into Bedrock Converse payload fields."""
    system_blocks: list[dict[str, str]] = []
    convo_messages: list[dict[str, Any]] = []

    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        content = message.get("content")
        if not isinstance(content, str):
            continue
        text = content.strip()
        if not text:
            continue

        if role == "system":
            system_blocks.append({"text": text})
            continue
        if role not in {"user", "assistant"}:
            continue

        convo_messages.append(
            {
                "role": role,
                "content": [{"text": text}],
            }
        )

    if not convo_messages:
        convo_messages = [{"role": "user", "content": [{"text": " "}]}]
    elif (
        require_terminal_user_message
        and str(convo_messages[-1].get("role", "")).strip().lower() != "user"
    ):
        # Some Bedrock models (for example Nova) reject assistant-prefill payloads and
        # require the final turn to be a user message.
        convo_messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "text": (
                            "Revise the answer using the prior instructions and evidence, and "
                            "return the final response."
                        )
                    }
                ],
            }
        )

    return system_blocks, convo_messages


def _from_bedrock_response(response: dict[str, Any]) -> _CompatResponse:
    """Normalize Bedrock Converse response into the subset used by services."""
    content_blocks = response.get("output", {}).get("message", {}).get("content", [])
    texts: list[str] = []
    citation_urls = _extract_citation_urls(content_blocks)
    if isinstance(content_blocks, list):
        for block in content_blocks:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    texts.append(text.strip())
    output_text = "\n".join(texts)
    if citation_urls and bool(getattr(settings.bedrock, "web_grounding_include_sources", True)):
        sources = "\n".join(f"- {url}" for url in citation_urls)
        output_text = f"{output_text}\n\nSources:\n{sources}".strip()

    usage = response.get("usage", {})
    prompt_tokens = int(usage.get("inputTokens") or 0)
    completion_tokens = int(usage.get("outputTokens") or 0)
    total_tokens = int(usage.get("totalTokens") or (prompt_tokens + completion_tokens))

    return _CompatResponse(
        choices=[_CompatChoice(message=_CompatMessage(content=output_text))],
        usage=_CompatUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        ),
    )


def _extract_citation_urls(content_blocks: Any) -> list[str]:
    """Collect unique citation URLs from Bedrock `citationsContent` blocks."""
    if not isinstance(content_blocks, list):
        return []
    urls: list[str] = []
    seen: set[str] = set()
    for block in content_blocks:
        if not isinstance(block, dict):
            continue
        raw_citations_content = block.get("citationsContent")
        if isinstance(raw_citations_content, list):
            citation_groups = raw_citations_content
        elif isinstance(raw_citations_content, dict):
            citation_groups = [raw_citations_content]
        else:
            citation_groups = []
        for group in citation_groups:
            if not isinstance(group, dict):
                continue
            raw_citations = group.get("citations")
            if isinstance(raw_citations, list):
                citations = raw_citations
            elif isinstance(raw_citations, dict):
                citations = [raw_citations]
            else:
                citations = []
            for citation in citations:
                if not isinstance(citation, dict):
                    continue
                location = citation.get("location")
                if not isinstance(location, dict):
                    continue
                web = location.get("web")
                if not isinstance(web, dict):
                    continue
                url = str(web.get("url", "")).strip()
                if not url or url in seen:
                    continue
                seen.add(url)
                urls.append(url)
    return urls


def _should_enable_web_grounding(*, model: str, enable_web_grounding: bool) -> bool:
    """Guard Web Grounding to explicit opt-in and Nova model families only."""
    if not enable_web_grounding:
        return False
    return "nova" in str(model).strip().lower()


def _nova_grounding_tool_config() -> dict[str, Any]:
    return {"tools": [{"systemTool": {"name": "nova_grounding"}}]}


class _BedrockCompatCompletions:
    async def create(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        limiter_name: str = "llm",
        limiter_acquire_timeout_seconds: float | None = None,
        rate_limit_profile: str = "answer",
        enable_web_grounding: bool = False,
    ):
        """Async chat completion entrypoint backed by Bedrock Converse."""
        system_blocks, convo_messages = _to_bedrock_payload(
            messages,
            require_terminal_user_message=True,
        )
        payload: dict[str, Any] = {
            "modelId": model,
            "messages": convo_messages,
        }
        if system_blocks:
            payload["system"] = system_blocks
        if _should_enable_web_grounding(
            model=model,
            enable_web_grounding=bool(enable_web_grounding),
        ):
            payload["toolConfig"] = _nova_grounding_tool_config()

        if limiter_acquire_timeout_seconds and limiter_acquire_timeout_seconds > 0:
            limiter_context = dependency_limiter(
                limiter_name,
                acquire_timeout_seconds=float(limiter_acquire_timeout_seconds),
            )
        else:
            limiter_context = dependency_limiter(limiter_name)

        async with limiter_context:
            if rate_limit_profile == "answer":
                response = await aconverse(payload)
            else:
                response = await aconverse(payload, rate_limit_profile=rate_limit_profile)
        return _from_bedrock_response(response)

    async def stream(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        limiter_name: str = "llm",
        limiter_acquire_timeout_seconds: float | None = None,
        rate_limit_profile: str = "answer",
    ) -> AsyncIterator[str]:
        """Yield true Bedrock token deltas for chat responses."""
        system_blocks, convo_messages = _to_bedrock_payload(
            messages,
            require_terminal_user_message=True,
        )
        payload: dict[str, Any] = {
            "modelId": model,
            "messages": convo_messages,
        }
        if system_blocks:
            payload["system"] = system_blocks

        if limiter_acquire_timeout_seconds and limiter_acquire_timeout_seconds > 0:
            limiter_context = dependency_limiter(
                limiter_name,
                acquire_timeout_seconds=float(limiter_acquire_timeout_seconds),
            )
        else:
            limiter_context = dependency_limiter(limiter_name)

        async with limiter_context:
            if rate_limit_profile == "answer":
                stream_iter = aconverse_stream_text(payload)
            else:
                stream_iter = aconverse_stream_text(
                    payload,
                    rate_limit_profile=rate_limit_profile,
                )
            async for delta in stream_iter:
                if delta:
                    yield delta


class _BedrockCompatChat:
    def __init__(self):
        self.completions = _BedrockCompatCompletions()


class _BedrockCompatClient:
    def __init__(self):
        self.chat = _BedrockCompatChat()


client = _BedrockCompatClient()
