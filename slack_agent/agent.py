"""
Claude API agent loop.
Receives a user message + conversation history, calls Claude with tools,
executes any requested tools, and loops until Claude produces a final answer.
"""
import asyncio
import logging
import os
from typing import Any

import anthropic

from .tools.gcal_tools import GCAL_TOOL_DEFINITIONS, execute_gcal_tool
from .tools.gmail_tools import GMAIL_TOOL_DEFINITIONS, execute_gmail_tool
from .tools.granola_tools import GRANOLA_TOOL_DEFINITIONS, execute_granola_tool
from .tools.hubspot_tools import HUBSPOT_TOOL_DEFINITIONS, execute_hubspot_tool
from .tools.memory_tools import MEMORY_TOOL_DEFINITIONS, execute_memory_tool
from .tools.productive_tools import PRODUCTIVE_TOOL_DEFINITIONS, execute_productive_tool
from .tools.readai_tools import READAI_TOOL_DEFINITIONS, execute_readai_tool
from .tools.slack_tools import SLACK_TOOL_DEFINITIONS, execute_slack_tool

logger = logging.getLogger(__name__)

MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
MAX_TOKENS = int(os.getenv("AGENT_MAX_TOKENS", "4096"))
MAX_ITERATIONS = int(os.getenv("AGENT_MAX_ITERATIONS", "15"))
# Truncate each tool result to avoid blowing up the context window
MAX_TOOL_RESULT_CHARS = int(os.getenv("AGENT_MAX_TOOL_RESULT_CHARS", "4000"))

ALL_TOOL_DEFINITIONS = (
    GCAL_TOOL_DEFINITIONS
    + GMAIL_TOOL_DEFINITIONS
    + GRANOLA_TOOL_DEFINITIONS
    + HUBSPOT_TOOL_DEFINITIONS
    + MEMORY_TOOL_DEFINITIONS
    + PRODUCTIVE_TOOL_DEFINITIONS
    + READAI_TOOL_DEFINITIONS
    + SLACK_TOOL_DEFINITIONS
)

# Tools needed for report generation — excludes write/admin tools to save tokens
_REPORT_TOOL_NAMES = {
    # HubSpot — read only
    "hubspot_search_company_by_name",
    "hubspot_get_company_timeline",
    "hubspot_get_active_companies",
    "hubspot_get_active_contacts",
    "hubspot_search_contact_by_name",
    # Slack — internal + external reading
    "slack_list_clients",
    "slack_read_tropical_channel",
    "slack_list_client_channels",
    "slack_read_client_channel",
    "slack_get_client_overview",
    "slack_check_unanswered",
    # Meetings
    "readai_get_recent_meetings",
    "readai_search_meetings",
    "granola_list_available_tools",
    "granola_call_tool",
    "granola_list_notes",
    "granola_search_notes",
    "granola_get_note",
    # Productive — overview only
    "productive_list_projects",
    "productive_list_time_entries",
    "productive_list_tasks",
    # Memory — always available
    "memory_recall",
    "memory_save",
    "memory_list_topics",
    "memory_delete",
}

_REPORT_KEYWORDS = {
    "relatório", "relatórios", "relatorio", "relatorios",
    "report", "resumo", "resumos", "panorama",
    "overview", "hoje", "semana", "clientes", "cliente",
    "pendências", "pendencias", "acionáveis", "acionaveis",
}


def _select_tools(user_message: str) -> list:
    """Return a subset of tools relevant to the request to reduce input token usage."""
    words = set(user_message.lower().split())
    if words & _REPORT_KEYWORDS:
        return [t for t in ALL_TOOL_DEFINITIONS if t["name"] in _REPORT_TOOL_NAMES]
    return ALL_TOOL_DEFINITIONS

_client: anthropic.AsyncAnthropic | None = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(
            api_key=os.getenv("ANTHROPIC_API_KEY")
        )
    return _client


def _truncate(text: str, max_chars: int = MAX_TOOL_RESULT_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n[... resultado truncado — {len(text)} chars total]"


async def _execute_tool(tool_name: str, tool_input: dict) -> str:
    """Route tool call to the correct executor."""
    granola_names = {t["name"] for t in GRANOLA_TOOL_DEFINITIONS}
    hubspot_names = {t["name"] for t in HUBSPOT_TOOL_DEFINITIONS}
    memory_names = {t["name"] for t in MEMORY_TOOL_DEFINITIONS}
    gcal_names = {t["name"] for t in GCAL_TOOL_DEFINITIONS}
    productive_names = {t["name"] for t in PRODUCTIVE_TOOL_DEFINITIONS}
    readai_names = {t["name"] for t in READAI_TOOL_DEFINITIONS}
    slack_names = {t["name"] for t in SLACK_TOOL_DEFINITIONS}

    gmail_names = {t["name"] for t in GMAIL_TOOL_DEFINITIONS}

    if tool_name in gcal_names:
        return await execute_gcal_tool(tool_name, tool_input)
    elif tool_name in gmail_names:
        return await execute_gmail_tool(tool_name, tool_input)
    elif tool_name in granola_names:
        return await execute_granola_tool(tool_name, tool_input)
    elif tool_name in hubspot_names:
        return await execute_hubspot_tool(tool_name, tool_input)
    elif tool_name in memory_names:
        return await execute_memory_tool(tool_name, tool_input)
    elif tool_name in productive_names:
        return await execute_productive_tool(tool_name, tool_input)
    elif tool_name in readai_names:
        return await execute_readai_tool(tool_name, tool_input)
    elif tool_name in slack_names:
        return await execute_slack_tool(tool_name, tool_input)
    else:
        return f"Ferramenta desconhecida: {tool_name}"


async def _call_with_retry(client, **kwargs) -> Any:
    """Call the Anthropic API with exponential backoff on rate limit errors."""
    delays = [5, 15, 30]
    for attempt, delay in enumerate(delays + [None]):
        try:
            return await client.messages.create(**kwargs)
        except anthropic.RateLimitError as exc:
            if delay is None:
                raise
            logger.warning(
                "Rate limit hit (attempt %d/%d), retrying in %ds: %s",
                attempt + 1, len(delays) + 1, delay, exc
            )
            await asyncio.sleep(delay)


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

    tools = _select_tools(user_message)
    logger.debug("Using %d/%d tools for this request", len(tools), len(ALL_TOOL_DEFINITIONS))

    for iteration in range(MAX_ITERATIONS):
        logger.debug("Agent iteration %d/%d", iteration + 1, MAX_ITERATIONS)

        response = await _call_with_retry(
            client,
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=system_prompt,
            tools=tools,
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
                result_text = _truncate(result_text)
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
