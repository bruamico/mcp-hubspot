"""
Claude API agent loop.
Receives a user message + conversation history, calls Claude with tools,
executes any requested tools, and loops until Claude produces a final answer.
"""
import logging
import os
from typing import Any

import anthropic

from .tools.granola_tools import GRANOLA_TOOL_DEFINITIONS, execute_granola_tool
from .tools.hubspot_tools import HUBSPOT_TOOL_DEFINITIONS, execute_hubspot_tool
from .tools.productive_tools import PRODUCTIVE_TOOL_DEFINITIONS, execute_productive_tool
from .tools.readai_tools import READAI_TOOL_DEFINITIONS, execute_readai_tool
from .tools.slack_tools import SLACK_TOOL_DEFINITIONS, execute_slack_tool

logger = logging.getLogger(__name__)

MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
MAX_TOKENS = int(os.getenv("AGENT_MAX_TOKENS", "4096"))
MAX_ITERATIONS = int(os.getenv("AGENT_MAX_ITERATIONS", "10"))

ALL_TOOL_DEFINITIONS = (
    GRANOLA_TOOL_DEFINITIONS
    + HUBSPOT_TOOL_DEFINITIONS
    + PRODUCTIVE_TOOL_DEFINITIONS
    + READAI_TOOL_DEFINITIONS
    + SLACK_TOOL_DEFINITIONS
)

_client: anthropic.AsyncAnthropic | None = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(
            api_key=os.getenv("ANTHROPIC_API_KEY")
        )
    return _client


async def _execute_tool(tool_name: str, tool_input: dict) -> str:
    """Route tool call to the correct executor."""
    granola_names = {t["name"] for t in GRANOLA_TOOL_DEFINITIONS}
    hubspot_names = {t["name"] for t in HUBSPOT_TOOL_DEFINITIONS}
    productive_names = {t["name"] for t in PRODUCTIVE_TOOL_DEFINITIONS}
    readai_names = {t["name"] for t in READAI_TOOL_DEFINITIONS}
    slack_names = {t["name"] for t in SLACK_TOOL_DEFINITIONS}

    if tool_name in granola_names:
        return await execute_granola_tool(tool_name, tool_input)
    elif tool_name in hubspot_names:
        return await execute_hubspot_tool(tool_name, tool_input)
    elif tool_name in productive_names:
        return await execute_productive_tool(tool_name, tool_input)
    elif tool_name in readai_names:
        return await execute_readai_tool(tool_name, tool_input)
    elif tool_name in slack_names:
        return await execute_slack_tool(tool_name, tool_input)
    else:
        return f"Ferramenta desconhecida: {tool_name}"


async def run_agent(
    user_message: str,
    history: list[dict],
    system_prompt: str,
) -> str:
    """
    Run the agent loop and return the final text response.

    Args:
        user_message:  The raw text from the user (already stripped of @mentions).
        history:       Previous messages in the thread (role/content dicts).
        system_prompt: The system prompt for Claude.

    Returns:
        Claude's final text response.
    """
    client = _get_client()

    # Build message list: history + current user message
    messages: list[dict[str, Any]] = list(history) + [
        {"role": "user", "content": user_message}
    ]

    for iteration in range(MAX_ITERATIONS):
        logger.debug("Agent iteration %d/%d", iteration + 1, MAX_ITERATIONS)

        response = await client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=system_prompt,
            tools=ALL_TOOL_DEFINITIONS,
            messages=messages,
        )

        logger.debug("Stop reason: %s", response.stop_reason)

        # ── Final text response ────────────────────────────────────────────
        if response.stop_reason == "end_turn":
            text_parts = [b.text for b in response.content if b.type == "text"]
            return "\n".join(text_parts) if text_parts else "(sem resposta)"

        # ── Tool use ───────────────────────────────────────────────────────
        if response.stop_reason == "tool_use":
            # Append assistant message (may contain text + tool_use blocks)
            messages.append({"role": "assistant", "content": response.content})

            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue

                logger.info("Tool call: %s %s", block.name, block.input)
                result_text = await _execute_tool(block.name, block.input)
                logger.debug("Tool result (%d chars): %s…", len(result_text), result_text[:200])

                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_text,
                    }
                )

            messages.append({"role": "user", "content": tool_results})
            continue

        # Unexpected stop reason
        logger.warning("Unexpected stop_reason: %s", response.stop_reason)
        break

    logger.warning("Agent reached max iterations (%d)", MAX_ITERATIONS)
    return "Não consegui completar a tarefa dentro do número máximo de passos. Tente reformular a pergunta."
