"""Apartment-ad matching for the auto-reply bot.

When a DM looks like an apartment inquiry, we call the LLM once to extract
structured filters, then query the Postgres `ads` table and hand the LLM
a short list of matches with links to the channel post.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, Optional

import asyncpg
import httpx


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DATABASE_URL: str = os.environ.get("DATABASE_URL", "")
ADS_TABLE: str = os.environ.get("APARTMENT_ADS_TABLE", "ads")
CHANNEL_USERNAME: str = os.environ.get(
    "APARTMENT_CHANNEL_USERNAME", "bezmakler_arzon_kv"
)

# Rough UZS per 1 USD; used only for price comparison, not display.
USD_TO_UZS: float = float(os.environ.get("USD_TO_UZS", "12500"))

# How many ads to hand to the reply LLM as context.
MAX_MATCHES: int = int(os.environ.get("APARTMENT_MAX_MATCHES", "5"))

# Only consider ads posted this many days back (0 = no filter).
FRESHNESS_DAYS: int = int(os.environ.get("APARTMENT_FRESHNESS_DAYS", "30"))

# Reuse the auto-reply LLM settings.
LLM_API_KEY: str = os.environ.get("AUTO_REPLY_LLM_API_KEY", "")
LLM_BASE_URL: str = os.environ.get(
    "AUTO_REPLY_LLM_BASE_URL", "https://api.deepseek.com/v1"
).rstrip("/")
# Use a small/fast model for extraction; falls back to the main model.
EXTRACT_MODEL: str = os.environ.get(
    "APARTMENT_EXTRACT_MODEL",
    os.environ.get("AUTO_REPLY_LLM_MODEL", "deepseek-chat"),
)


_pool: Optional[asyncpg.Pool] = None


def _log(msg: str) -> None:
    print(f"[apartment] {msg}", file=sys.stderr, flush=True)


async def _get_pool() -> Optional[asyncpg.Pool]:
    global _pool
    if _pool is not None:
        return _pool
    if not DATABASE_URL:
        _log("DATABASE_URL is not set — apartment search disabled.")
        return None
    try:
        _pool = await asyncpg.create_pool(
            DATABASE_URL, min_size=1, max_size=3, command_timeout=10
        )
        _log(f"Postgres pool created (table='{ADS_TABLE}').")
        return _pool
    except Exception as exc:
        _log(f"Postgres pool creation failed: {exc}")
        return None


# ---------------------------------------------------------------------------
# Filter extraction
# ---------------------------------------------------------------------------

_EXTRACT_SYSTEM_PROMPT = """You classify a Telegram DM in Uzbek, Russian, or English and extract apartment-rental search filters.

Return ONE JSON object, no prose:
{
  "is_apartment_query": true|false,
  "rooms": integer or null,
  "districts": [strings]  // Tashkent district or landmark names, empty [] if none
  "max_price_usd": number or null,
  "min_price_usd": number or null,
  "deal_type": "ijara" | "sotiladi" | null,   // rent vs sale; default null
  "property_type": "kvartira" | "hovli" | "ofis" | null,
  "keywords": [strings]   // free-text things to match against ad text/hashtags
}

Rules:
- is_apartment_query=false only for pure greetings ("hi", "salom") or off-topic chit-chat with no rental intent.
- Prices in UZS/so'm: convert to USD at 1 USD = 12500 UZS.
- Prices like "500", "500$", "500 dollar", "500 у.е." → USD as-is.
- Districts: normalize to canonical Tashkent tumans if obvious (Chilonzor, Yunusobod, Mirzo-Ulugbek, Sergeli, Yashnobod, Yakkasaroy, Shayhontohur, Uchtepa, Bektemir, Olmazor, Yangihayot). Also accept landmarks (TTZ, Kadisheva, Chorsu, etc.) as-is.
- If user says "arenda"/"snyat"/"ijaraga" → deal_type="ijara". "kupit"/"sotib" → "sotiladi".
- Never invent filters that the user didn't mention. Leave nulls.
"""


async def _llm_json(system: str, user: str) -> Optional[dict]:
    if not LLM_API_KEY:
        return None
    payload = {
        "model": EXTRACT_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": 300,
        "temperature": 0.0,
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                f"{LLM_BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {LLM_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            if resp.status_code != 200:
                _log(f"extract LLM {resp.status_code}: {resp.text[:200]}")
                return None
            content = resp.json()["choices"][0]["message"]["content"]
            return json.loads(content)
    except Exception as exc:
        _log(f"extract LLM failed: {exc}")
        return None


async def extract_filters(text: str) -> Optional[dict]:
    """Return filter dict if the DM is an apartment query, None otherwise."""
    data = await _llm_json(_EXTRACT_SYSTEM_PROMPT, text)
    if not data or not data.get("is_apartment_query"):
        return None
    _log(f"filters: {data}")
    return data


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

async def search_ads(filters: dict) -> list[dict]:
    pool = await _get_pool()
    if pool is None:
        return []

    where: list[str] = ["published_msg_id IS NOT NULL"]
    args: list[Any] = []

    def add(clause: str, value: Any) -> None:
        args.append(value)
        where.append(clause.replace("?", f"${len(args)}"))

    rooms = filters.get("rooms")
    if isinstance(rooms, int):
        add("rooms = ?", rooms)

    deal_type = filters.get("deal_type")
    if deal_type in ("ijara", "sotiladi"):
        add("deal_type = ?", deal_type)

    property_type = filters.get("property_type")
    if property_type:
        add("property_type = ?", property_type)

    districts = filters.get("districts") or []
    if districts:
        # Match against structured `district` column and the hashtag list.
        or_parts: list[str] = []
        for d in districts:
            args.append(f"%{d}%")
            or_parts.append(f"district ILIKE ${len(args)}")
            args.append(f"%{d.lower()}%")
            or_parts.append(f"LOWER(hashtags) LIKE ${len(args)}")
            args.append(f"%{d}%")
            or_parts.append(f"address ILIKE ${len(args)}")
        where.append("(" + " OR ".join(or_parts) + ")")

    max_price_usd = filters.get("max_price_usd")
    if isinstance(max_price_usd, (int, float)):
        args.append(float(max_price_usd))
        args.append(float(max_price_usd) * USD_TO_UZS)
        where.append(
            f"((currency = 'USD' AND price <= ${len(args) - 1}) "
            f"OR (currency = 'UZS' AND price <= ${len(args)}))"
        )

    min_price_usd = filters.get("min_price_usd")
    if isinstance(min_price_usd, (int, float)):
        args.append(float(min_price_usd))
        args.append(float(min_price_usd) * USD_TO_UZS)
        where.append(
            f"((currency = 'USD' AND price >= ${len(args) - 1}) "
            f"OR (currency = 'UZS' AND price >= ${len(args)}))"
        )

    keywords = filters.get("keywords") or []
    for kw in keywords[:5]:
        args.append(f"%{kw}%")
        where.append(
            f"(redacted_text ILIKE ${len(args)} OR hashtags ILIKE ${len(args)})"
        )

    if FRESHNESS_DAYS > 0:
        where.append(f"created_at > now() - interval '{FRESHNESS_DAYS} days'")

    args.append(MAX_MATCHES)
    sql = f"""
        SELECT id, published_msg_id, rooms, district, address, area,
               price, currency, price_period, redacted_text, created_at
        FROM {ADS_TABLE}
        WHERE {" AND ".join(where)}
        ORDER BY created_at DESC
        LIMIT ${len(args)}
    """
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *args)
    except Exception as exc:
        _log(f"query failed: {exc}")
        return []

    results = [dict(r) for r in rows]
    _log(f"matched {len(results)} ad(s)")
    return results


# ---------------------------------------------------------------------------
# Rendering for the LLM
# ---------------------------------------------------------------------------

def _post_link(msg_id: int) -> str:
    return f"https://t.me/{CHANNEL_USERNAME}/{msg_id}"


_TAG_RE = re.compile(r"<[^>]+>")


def _short_snippet(text: Optional[str], limit: int = 220) -> str:
    if not text:
        return ""
    clean = _TAG_RE.sub("", text).strip()
    clean = re.sub(r"\s+", " ", clean)
    if len(clean) <= limit:
        return clean
    return clean[:limit].rstrip() + "…"


def format_ads_for_llm(ads: list[dict]) -> str:
    if not ads:
        return "No matching apartments were found in our channel right now."

    lines: list[str] = [
        "Available apartments matching the user's request "
        "(quote the LINK verbatim in your reply so the user can open the post):"
    ]
    for i, ad in enumerate(ads, 1):
        parts: list[str] = []
        if ad.get("rooms"):
            parts.append(f"{ad['rooms']}-xonali")
        if ad.get("area"):
            parts.append(str(ad["area"]))
        if ad.get("district"):
            parts.append(str(ad["district"]))
        if ad.get("address"):
            parts.append(str(ad["address"]))
        price = ad.get("price")
        currency = ad.get("currency") or ""
        period = ad.get("price_period") or ""
        if price is not None:
            parts.append(f"{price} {currency}/{period}".strip("/"))
        head = " · ".join(p for p in parts if p)
        snippet = _short_snippet(ad.get("redacted_text"))
        link = _post_link(ad["published_msg_id"])
        block = f"{i}. {head}\n   {snippet}\n   LINK: {link}"
        lines.append(block)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def build_apartment_context(text: str) -> Optional[str]:
    """Return a system-prompt appendix with matching ads, or None if not applicable."""
    if not DATABASE_URL or not LLM_API_KEY:
        return None
    filters = await extract_filters(text)
    if not filters:
        return None
    ads = await search_ads(filters)
    return format_ads_for_llm(ads)
