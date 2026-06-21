"""Automatic DM reply bot using an LLM API.

Listens for incoming private messages on all configured Telegram accounts
and replies automatically, mimicking the account owner's tone based on
recent conversation history.

Activated when AUTO_REPLY_ENABLED=true is set in the environment.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

import httpx
from telethon import TelegramClient, events
from telethon.tl.types import User

logger = logging.getLogger("telegram_mcp.auto_reply")

# ---------------------------------------------------------------------------
# Config (read once at import time so the runner can validate early)
# ---------------------------------------------------------------------------

AUTO_REPLY_ENABLED: bool = os.environ.get("AUTO_REPLY_ENABLED", "").strip().lower() in (
    "1", "true", "yes", "on",
)

# LLM settings
LLM_API_KEY: str = os.environ.get("AUTO_REPLY_LLM_API_KEY", "")
LLM_BASE_URL: str = os.environ.get(
    "AUTO_REPLY_LLM_BASE_URL", "https://openrouter.ai/api/v1"
).rstrip("/")
LLM_MODEL: str = os.environ.get("AUTO_REPLY_LLM_MODEL", "mistralai/mistral-7b-instruct:free")
LLM_MAX_TOKENS: int = int(os.environ.get("AUTO_REPLY_LLM_MAX_TOKENS", "300"))
LLM_TEMPERATURE: float = float(os.environ.get("AUTO_REPLY_LLM_TEMPERATURE", "0.8"))

# How many recent messages to load as context (back-and-forth pairs)
HISTORY_LIMIT: int = int(os.environ.get("AUTO_REPLY_HISTORY_LIMIT", "20"))

# The system prompt that defines the owner's personality / tone.
# Keep it short and natural — just describe how you normally write.
DEFAULT_SYSTEM_PROMPT = (
    "You are replying to a Telegram DM on behalf of the account owner. "
    "Match the owner's natural, casual messaging tone exactly as shown in the "
    "conversation history. Be concise. Never reveal you are an AI. "
    "Reply only with the message text, nothing else."
)
SYSTEM_PROMPT: str = os.environ.get("AUTO_REPLY_SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT)

# Typing simulation: pause this many seconds before sending (feels human)
TYPING_DELAY: float = float(os.environ.get("AUTO_REPLY_TYPING_DELAY", "2.0"))

# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------


async def _call_llm(messages: list[dict]) -> Optional[str]:
    """Call the configured LLM API (OpenAI-compatible) and return the reply text."""
    if not LLM_API_KEY:
        logger.error("AUTO_REPLY_LLM_API_KEY is not set — cannot generate reply.")
        return None

    payload = {
        "model": LLM_MODEL,
        "messages": messages,
        "max_tokens": LLM_MAX_TOKENS,
        "temperature": LLM_TEMPERATURE,
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{LLM_BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {LLM_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()
    except httpx.HTTPStatusError as exc:
        logger.error("LLM API HTTP error %s: %s", exc.response.status_code, exc.response.text)
    except Exception:
        logger.error("LLM API call failed", exc_info=True)
    return None


# ---------------------------------------------------------------------------
# History builder
# ---------------------------------------------------------------------------


async def _build_messages(
    client: TelegramClient,
    peer,
    me_id: int,
    incoming_text: str,
) -> list[dict]:
    """Build the chat-completions messages list from Telegram history."""
    history_messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    # Fetch recent messages (oldest first after reversing)
    try:
        fetched = await client.get_messages(peer, limit=HISTORY_LIMIT)
        for msg in reversed(fetched):
            text = getattr(msg, "message", "") or ""
            if not text:
                continue
            sender_id = getattr(msg, "sender_id", None)
            role = "assistant" if sender_id == me_id else "user"
            history_messages.append({"role": role, "content": text})
    except Exception:
        logger.warning("Could not fetch history, replying with no context.", exc_info=True)

    # The new incoming message (may already be in history, but appending ensures
    # it's always the last turn for the model)
    history_messages.append({"role": "user", "content": incoming_text})
    return history_messages


# ---------------------------------------------------------------------------
# Per-account handler registration
# ---------------------------------------------------------------------------


def _is_bot(sender) -> bool:
    return isinstance(sender, User) and bool(getattr(sender, "bot", False))


def register_auto_reply(client: TelegramClient, label: str) -> None:
    """Register the incoming-DM handler on a single TelegramClient."""

    @client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
    async def _handle_dm(event: events.NewMessage.Event) -> None:
        text: str = getattr(event.message, "message", "") or ""
        if not text.strip():
            # Skip media-only messages (voice, photos without caption, etc.)
            return

        sender = await event.get_sender()

        # Never reply to bots or to ourselves
        if _is_bot(sender):
            return

        me = await client.get_me()
        if sender and getattr(sender, "id", None) == me.id:
            return

        sender_name = getattr(sender, "first_name", None) or str(getattr(sender, "id", "?"))
        logger.info("[%s] Incoming DM from %s — generating reply...", label, sender_name)

        llm_messages = await _build_messages(client, event.peer_id, me.id, text)
        reply_text = await _call_llm(llm_messages)

        if not reply_text:
            logger.warning("[%s] No reply generated for DM from %s.", label, sender_name)
            return

        # Brief pause so the reply doesn't feel instant/robotic
        if TYPING_DELAY > 0:
            async with client.action(event.chat_id, "typing"):
                await asyncio.sleep(TYPING_DELAY)

        await client.send_message(event.chat_id, reply_text)
        logger.info("[%s] Replied to %s.", label, sender_name)

    logger.info("[%s] Auto-reply handler registered (DMs only).", label)


# ---------------------------------------------------------------------------
# Public entry point called from runner.py
# ---------------------------------------------------------------------------


def setup_auto_reply(clients: dict[str, TelegramClient]) -> None:
    """Register auto-reply handlers on all configured clients.

    Called from runner._main() after clients are connected, before the MCP
    transport starts. Safe to call when AUTO_REPLY_ENABLED is False — it
    exits immediately so the caller doesn't need to guard.
    """
    if not AUTO_REPLY_ENABLED:
        return

    if not LLM_API_KEY:
        print(
            "WARNING: AUTO_REPLY_ENABLED=true but AUTO_REPLY_LLM_API_KEY is not set. "
            "Auto-reply is disabled.",
            flush=True,
        )
        return

    for label, client in clients.items():
        register_auto_reply(client, label)

    print(
        f"Auto-reply active on {len(clients)} account(s) "
        f"using model '{LLM_MODEL}' ({LLM_BASE_URL}).",
        flush=True,
    )
