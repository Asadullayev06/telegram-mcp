"""Application entrypoints for the Telegram MCP server."""

import os

from telegram_mcp.install_guard import UnsafeInstallationError, assert_safe_distribution

try:
    assert_safe_distribution()
except UnsafeInstallationError as exc:
    raise SystemExit(str(exc)) from None

from telegram_mcp import runtime as _runtime
from telegram_mcp.runtime import *
import telegram_mcp.tools  # noqa: F401 - registers MCP tools via decorators


async def _connect_authorized_client(label, client) -> None:
    await client.connect()
    if await client.is_user_authorized():
        return

    raise RuntimeError(
        f"Telegram client '{label}' is not authorized. Interactive phone login "
        "is disabled for the MCP server. Generate a session string with "
        "`uv run session_string_generator.py`, then set TELEGRAM_SESSION_STRING "
        "or TELEGRAM_SESSION_STRING_<LABEL> in the environment. "
        "For existing file sessions, run the login outside the MCP server first."
    )


def _resolve_transport() -> str:
    """Pick MCP transport. Defaults to SSE when PORT is set (remote deploy)."""
    explicit = os.environ.get("MCP_TRANSPORT")
    if explicit:
        return explicit.lower()
    if os.environ.get("PORT"):
        return "sse"
    return "stdio"


async def _main() -> None:
    try:
        labels = ", ".join(clients.keys())
        print(f"Starting {len(clients)} Telegram client(s) ({labels})...", file=sys.stderr)
        await asyncio.gather(
            *(_connect_authorized_client(label, cl) for label, cl in clients.items())
        )

        # Warm entity caches — StringSession has no persistent cache,
        # so fetch all dialogs once per client to populate them.
        # Runs in background: blocking startup on this (e.g. under a
        # GetDialogsRequest flood wait) makes MCP clients time out, and
        # resolve_entity() re-warms the cache on miss anyway.
        print("Warming entity caches (background)...", file=sys.stderr)

        async def _warm_caches() -> None:
            try:
                await asyncio.gather(*(cl.get_dialogs() for cl in clients.values()))
                print("Entity caches warmed.", file=sys.stderr)
            except Exception as warm_exc:
                print(f"Entity cache warm failed: {warm_exc}", file=sys.stderr)

        warm_task = asyncio.create_task(_warm_caches())

        transport = _resolve_transport()
        if transport in ("sse", "streamable-http", "http"):
            port = int(os.environ.get("PORT", "8000"))
            host = os.environ.get("HOST", "0.0.0.0")
            mcp.settings.host = host
            mcp.settings.port = port
            # FastMCP defaults its DNS-rebinding allowlist to localhost only,
            # which 421s any request reaching the container through a PaaS
            # edge proxy (Railway, Fly, etc). Disable the check unless the
            # operator explicitly enables it.
            if os.environ.get("MCP_DISABLE_HOST_CHECK", "1") not in ("0", "false", "False"):
                mcp.settings.transport_security.enable_dns_rebinding_protection = False
        if transport == "sse":
            print(
                f"Telegram client(s) started ({labels}). "
                f"Running MCP server over SSE on http://{host}:{port}/sse ...",
                file=sys.stderr,
            )
            await mcp.run_sse_async()
        elif transport in ("streamable-http", "http"):
            print(
                f"Telegram client(s) started ({labels}). "
                f"Running MCP server over Streamable HTTP on http://{host}:{port}/mcp ...",
                file=sys.stderr,
            )
            await mcp.run_streamable_http_async()
        else:
            print(
                f"Telegram client(s) started ({labels}). Running MCP server over stdio...",
                file=sys.stderr,
            )
            await mcp.run_stdio_async()
    except Exception as e:
        print(f"Error starting client: {e}", file=sys.stderr)
        if isinstance(e, sqlite3.OperationalError) and "database is locked" in str(e):
            print(
                "Database lock detected. Please ensure no other instances are running.",
                file=sys.stderr,
            )
        sys.exit(1)
    finally:
        try:
            await asyncio.gather(
                *(cl.disconnect() for cl in clients.values()), return_exceptions=True
            )
        except Exception:
            pass


def main() -> None:
    _configure_allowed_roots_from_cli(sys.argv[1:])
    _runtime._apply_exposed_tools_mode()
    nest_asyncio.apply()
    asyncio.run(_main())


if __name__ == "__main__":
    main()
