import asyncio
import logging
from types import SimpleNamespace
from typing import Any

from app.core.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)


def _latest_user_prompt(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        if str(message.get("role", "")).strip().lower() != "user":
            continue
        content = str(message.get("content", "")).strip()
        if content:
            return content
    return ""


def _extract_response_text(response: Any) -> str:
    message = getattr(response, "message", None)
    if isinstance(message, dict):
        content = message.get("content", [])
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    return text.strip()
    return str(response).strip()


def _compat_completion(text: str, prompt: str) -> SimpleNamespace:
    safe_text = str(text or "").strip() or "Sorry, no relevant information is found."
    prompt_tokens = max(1, len(str(prompt or "").split()))
    completion_tokens = max(1, len(safe_text.split()))
    usage = SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    )
    choice = SimpleNamespace(message=SimpleNamespace(content=safe_text))
    return SimpleNamespace(choices=[choice], usage=usage)


async def abrowser_agent_completion(messages: list[dict[str, Any]]) -> SimpleNamespace:
    """
    Run one prompt via AgentCore Browser tool using Strands.

    This path is optional and activated via BEDROCK_AGENTCORE_BROWSER_ENABLED.
    """
    prompt = _latest_user_prompt(messages)
    if not prompt:
        prompt = "Summarize the available web information with source links."

    try:
        from strands import Agent
        from strands_tools.browser import AgentCoreBrowser
    except Exception as exc:  # pragma: no cover - depends on optional runtime deps
        raise RuntimeError(
            "AgentCore Browser dependencies are missing. Install: "
            "bedrock-agentcore strands-agents strands-agents-tools playwright nest-asyncio"
        ) from exc

    region = str(getattr(settings.bedrock, "agentcore_browser_region", "")).strip() or "us-east-1"
    browser_identifier = (
        str(getattr(settings.bedrock, "agentcore_browser_identifier", "")).strip()
        or "aws.browser.v1"
    )

    def _invoke() -> Any:
        # Signature follows AWS quickstart examples; browserIdentifier is optional in some versions.
        try:
            browser_tool = AgentCoreBrowser(
                region=region,
                browserIdentifier=browser_identifier,
            )
        except TypeError:
            browser_tool = AgentCoreBrowser(region=region)
        agent = Agent(tools=[browser_tool.browser])
        return agent(prompt)

    response = await asyncio.to_thread(_invoke)
    text = _extract_response_text(response)
    logger.info(
        "AgentCoreBrowserInvoke | region=%s | browser_identifier=%s | prompt_chars=%s",
        region,
        browser_identifier,
        len(prompt),
    )
    return _compat_completion(text=text, prompt=prompt)

