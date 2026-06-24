"""Automatic DM reply bot using an LLM API.

Listens for incoming private messages on all configured Telegram accounts
and replies automatically, mimicking the account owner's tone based on
recent conversation history.

Activated when AUTO_REPLY_ENABLED=true is set in the environment.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Optional

import httpx
from telethon import TelegramClient, events
from telethon.tl.types import User

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

AUTO_REPLY_ENABLED: bool = os.environ.get("AUTO_REPLY_ENABLED", "").strip().lower() in (
    "1", "true", "yes", "on",
)

LLM_API_KEY: str = os.environ.get("AUTO_REPLY_LLM_API_KEY", "")
LLM_BASE_URL: str = os.environ.get(
    "AUTO_REPLY_LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai"
).rstrip("/")
LLM_MODEL: str = os.environ.get("AUTO_REPLY_LLM_MODEL", "gemini-2.0-flash")
LLM_MAX_TOKENS: int = int(os.environ.get("AUTO_REPLY_LLM_MAX_TOKENS", "300"))
LLM_TEMPERATURE: float = float(os.environ.get("AUTO_REPLY_LLM_TEMPERATURE", "0.8"))

HISTORY_LIMIT: int = int(os.environ.get("AUTO_REPLY_HISTORY_LIMIT", "20"))

DEFAULT_SYSTEM_PROMPT = (
    "You are replying to a Telegram DM on behalf of the account owner. "
    "Match the owner's natural, casual messaging tone exactly as shown in the "
    "conversation history. Be concise. Never reveal you are an AI. "
    "Reply only with the message text, nothing else."
)
SYSTEM_PROMPT: str = os.environ.get("AUTO_REPLY_SYSTEM_PROMPT", "") or DEFAULT_SYSTEM_PROMPT

# Seconds to wait before marking the message as read (simulates "seeing" it)
READ_DELAY: float = float(os.environ.get("AUTO_REPLY_READ_DELAY", "10.0"))
# Seconds to show "typing..." after reading, before sending
TYPING_DELAY: float = float(os.environ.get("AUTO_REPLY_TYPING_DELAY", "4.0"))

# Comma-separated usernames (without @) and/or numeric user IDs to never reply to.
# Example: AUTO_REPLY_BLOCKLIST=john_doe,987654321,jane123
_raw_blocklist = os.environ.get("AUTO_REPLY_BLOCKLIST", "")
BLOCKLIST: set[str] = {
    item.strip().lstrip("@").lower()
    for item in _raw_blocklist.split(",")
    if item.strip()
}


def _log(msg: str) -> None:
    print(f"[auto_reply] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------


async def _call_llm(messages: list[dict]) -> Optional[str]:
    if not LLM_API_KEY:
        _log("ERROR: AUTO_REPLY_LLM_API_KEY is not set.")
        return None

    payload = {
        "model": LLM_MODEL,
        "messages": messages,
        "max_tokens": LLM_MAX_TOKENS,
        "temperature": LLM_TEMPERATURE,
    }

    _log(f"Calling LLM: {LLM_BASE_URL} model={LLM_MODEL}")
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
            _log(f"LLM response status: {resp.status_code}")
            if resp.status_code != 200:
                _log(f"LLM error body: {resp.text[:500]}")
                resp.raise_for_status()
            data = resp.json()
            text = data["choices"][0]["message"]["content"].strip()
            _log(f"LLM reply generated ({len(text)} chars)")
            return text
    except httpx.HTTPStatusError as exc:
        _log(f"LLM HTTP error {exc.response.status_code}: {exc.response.text[:300]}")
    except Exception as exc:
        _log(f"LLM call failed: {exc}")
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
    history_messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    try:
        fetched = await client.get_messages(peer, limit=HISTORY_LIMIT)
        for msg in reversed(fetched):
            text = getattr(msg, "message", "") or ""
            if not text:
                continue
            sender_id = getattr(msg, "sender_id", None)
            role = "assistant" if sender_id == me_id else "user"
            history_messages.append({"role": role, "content": text})
        _log(f"Built context with {len(history_messages) - 1} history messages")
    except Exception as exc:
        _log(f"Could not fetch history (replying with no context): {exc}")

    history_messages.append({"role": "user", "content": incoming_text})
    return history_messages


# ---------------------------------------------------------------------------
# Per-account handler
# ---------------------------------------------------------------------------


def _is_bot(sender) -> bool:
    return isinstance(sender, User) and bool(getattr(sender, "bot", False))


def register_auto_reply(client: TelegramClient, label: str) -> None:

    @client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
    async def _handle_dm(event: events.NewMessage.Event) -> None:
        text: str = getattr(event.message, "message", "") or ""
        _log(f"[{label}] DM event fired. text={repr(text[:80])}")

        if not text.strip():
            _log(f"[{label}] Skipping: media-only message.")
            return

        sender = await event.get_sender()

        if _is_bot(sender):
            _log(f"[{label}] Skipping: sender is a bot.")
            return

        me = await client.get_me()
        if sender and getattr(sender, "id", None) == me.id:
            _log(f"[{label}] Skipping: message from self.")
            return

        # Blocklist check — match by username or numeric user ID
        if BLOCKLIST:
            sender_username = (getattr(sender, "username", None) or "").lower()
            sender_id_str = str(getattr(sender, "id", ""))
            if sender_username in BLOCKLIST or sender_id_str in BLOCKLIST:
                _log(f"[{label}] Skipping: sender is on the blocklist.")
                return

        sender_name = getattr(sender, "first_name", None) or str(getattr(sender, "id", "?"))
        _log(f"[{label}] Incoming DM from {sender_name!r} — waiting {READ_DELAY}s before reading...")

        # Step 1: sit unread for a natural delay
        await asyncio.sleep(READ_DELAY)

        # Step 2: mark as read
        await client.send_read_acknowledge(event.chat_id, event.message)
        _log(f"[{label}] Marked as read — generating reply...")

        # Step 3: generate reply while "typing" indicator is shown
        llm_messages = await _build_messages(client, event.peer_id, me.id, text)

        async with client.action(event.chat_id, "typing"):
            reply_text = await _call_llm(llm_messages)
            if not reply_text:
                _log(f"[{label}] No reply generated — aborting.")
                return
            # Hold the typing indicator for a natural extra pause
            if TYPING_DELAY > 0:
                await asyncio.sleep(TYPING_DELAY)

        await client.send_message(event.chat_id, reply_text)
        _log(f"[{label}] Reply sent to {sender_name!r}.")

    _log(f"[{label}] Handler registered (private DMs only).")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def setup_auto_reply(clients: dict[str, TelegramClient]) -> None:
    # Dump all AUTO_REPLY_* env vars for debugging (mask the key value)
    for k, v in os.environ.items():
        if k.startswith("AUTO_REPLY_"):
            masked = (v[:6] + "***") if k == "AUTO_REPLY_LLM_API_KEY" and v else repr(v)
            _log(f"ENV {k}={masked}")

    if not AUTO_REPLY_ENABLED:
        _log("AUTO_REPLY_ENABLED is not set — skipping.")
        return

    if not LLM_API_KEY:
        _log("WARNING: AUTO_REPLY_ENABLED=true but AUTO_REPLY_LLM_API_KEY is not set — skipping.")
        return

    for label, client in clients.items():
        register_auto_reply(client, label)

    _log(
        f"Auto-reply active on {len(clients)} account(s) "
        f"using model '{LLM_MODEL}' at {LLM_BASE_URL}"
    )
