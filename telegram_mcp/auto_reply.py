"""Automatic DM reply bot using an LLM API.

Listens for incoming private messages on all configured Telegram accounts
and replies automatically, mimicking the account owner's tone based on
recent conversation history.

Activated when AUTO_REPLY_ENABLED=true is set in the environment.
"""

from __future__ import annotations

import asyncio
import os
import random
import sys
from typing import Optional

import httpx
from telethon import TelegramClient, events
from telethon.tl.types import User

from telegram_mcp.apartment_search import build_apartment_context

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

DEFAULT_SYSTEM_PROMPT = """You are a helpful assistant for a Telegram channel about affordable apartments in Tashkent — only from homeowners, no agents.

## Your personality
- Talk like a real human friend texting, not a formal assistant
- Keep it SHORT — 1 to 3 sentences max most of the time
- No bullet points, no dashes, no structured lists ever
- No dramatic words like "afsus", "afsuski", "unfortunately" etc
- Warm, casual, natural tone

## Channel promotion rule — VERY IMPORTANT
- Mention the channel https://t.me/bezmakler_arzon_kv only ONCE or TWICE at the very start of the conversation
- After that, if they ask about more options just say something like "kanalda variantlar bor, ko'rib turing" — NO link again
- Never repeat the link more than twice in the whole conversation

## If user sends a voice message
- Say you can't listen to voice messages, ask them to write it out
- Keep it short and friendly, like: "Ovozli xabarni eshita olmayman, yozib yuborsangiz 🙏"

## If user asks for a specific owner's number
- You don't have it, say so simply in one sentence
- Don't give a list of suggestions, just say something natural like "raqam menda yo'q, kanaldan egasi bilan bog'lanishingiz mumkin"

## If the topic is not about apartments
- Just answer naturally like a normal person
- Don't force the channel into unrelated topics

## If matching apartments are provided below (under "Available apartments matching…")
- Share the LINK(s) from that list directly — those are specific post links, not the channel link, and they DO NOT count toward the once-or-twice channel-link rule
- Pick 1–2 best matches and drop their links into your reply naturally, one per line
- Never invent apartments that aren't in the list; if the list is empty just say nothing matches right now

## Key facts (use naturally when relevant, not all at once)
- All listings are free
- Only from homeowners, zero agents
- Owner numbers available instantly
- New listings daily
- Channel: https://t.me/bezmakler_arzon_kv

## Rules
- Never say you are AI or a bot unless directly asked
- Never use bullet points or dashes in your replies
- Never write long paragraphs
- Always sound like a human friend, not a support agent
"""
SYSTEM_PROMPT: str = os.environ.get("AUTO_REPLY_SYSTEM_PROMPT", "") or DEFAULT_SYSTEM_PROMPT

# Random read delay range in seconds (min and max)
READ_DELAY_MIN: float = float(os.environ.get("AUTO_REPLY_READ_DELAY_MIN", "4.0"))
READ_DELAY_MAX: float = float(os.environ.get("AUTO_REPLY_READ_DELAY_MAX", "14.0"))
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
    system_content = SYSTEM_PROMPT
    try:
        apartment_context = await build_apartment_context(incoming_text)
    except Exception as exc:
        _log(f"apartment context failed: {exc}")
        apartment_context = None
    if apartment_context:
        system_content = f"{SYSTEM_PROMPT}\n\n---\n{apartment_context}"

    history_messages = [{"role": "system", "content": system_content}]

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
        msg = event.message
        _log(f"[{label}] DM event fired. text={repr(text[:80])}")

        # Detect media type if no text
        if not text.strip():
            if getattr(msg, "voice", None) is not None:
                text = "[the person sent you a voice message — acknowledge it naturally, say you'll listen]"
            elif getattr(msg, "video_note", None) is not None:
                text = "[the person sent you a video message (circle) — acknowledge it naturally]"
            elif getattr(msg, "photo", None) is not None:
                text = "[the person sent you a photo — react to it naturally]"
            elif getattr(msg, "video", None) is not None:
                text = "[the person sent you a video — react to it naturally]"
            elif getattr(msg, "audio", None) is not None:
                text = "[the person sent you an audio file — acknowledge it naturally]"
            elif getattr(msg, "sticker", None) is not None:
                text = "[the person sent you a sticker — react briefly and naturally]"
            elif getattr(msg, "gif", None) is not None:
                text = "[the person sent you a GIF — react briefly and naturally]"
            elif getattr(msg, "document", None) is not None:
                text = "[the person sent you a file/document — acknowledge it naturally]"
            else:
                _log(f"[{label}] Skipping: empty message with no recognizable media.")
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
        read_delay = random.uniform(READ_DELAY_MIN, READ_DELAY_MAX)
        _log(f"[{label}] Incoming DM from {sender_name!r} — waiting {read_delay:.1f}s before reading...")

        # Step 1: sit unread for a random delay
        await asyncio.sleep(read_delay)

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
