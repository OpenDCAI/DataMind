"""Phase 0 smoke test — verify the Anthropic-compatible gateway is reachable
using the official `anthropic` Python SDK.

We deliberately do NOT depend on the `claude` CLI here: some installations
ship a vendor-rebranded binary that ignores ANTHROPIC_API_KEY. Going straight
to the /v1/messages endpoint gives us a 100% deterministic check.

Run:
    export DATAMIND__LLM__API_BASE=https://gateway.example.com
    export DATAMIND__LLM__API_KEY=sk-...
    # Optional override
    export DATAMIND__LLM__MODEL=claude-sonnet-4-6
    python -m datamind.scripts.hello_sdk

Exit 0 on success, 1 on failure. Prints the streamed text deltas.
"""
from __future__ import annotations

import asyncio
import os
import sys


_REQUIRED_ENV = ("DATAMIND__LLM__API_BASE", "DATAMIND__LLM__API_KEY")


def _check_env() -> tuple[str, str, str]:
    missing = [k for k in _REQUIRED_ENV if not os.environ.get(k)]
    # Accept ANTHROPIC_* as a fallback so users who already set those still work.
    if "DATAMIND__LLM__API_BASE" in missing and os.environ.get("ANTHROPIC_BASE_URL"):
        os.environ["DATAMIND__LLM__API_BASE"] = os.environ["ANTHROPIC_BASE_URL"]
        missing.remove("DATAMIND__LLM__API_BASE")
    if "DATAMIND__LLM__API_KEY" in missing and os.environ.get("ANTHROPIC_API_KEY"):
        os.environ["DATAMIND__LLM__API_KEY"] = os.environ["ANTHROPIC_API_KEY"]
        missing.remove("DATAMIND__LLM__API_KEY")

    if missing:
        sys.stderr.write(
            "[hello_sdk] Missing env vars: "
            + ", ".join(missing)
            + "\n[hello_sdk] Export them first (see .env.datamind.example).\n"
        )
        sys.exit(1)

    return (
        os.environ["DATAMIND__LLM__API_BASE"],
        os.environ["DATAMIND__LLM__API_KEY"],
        os.environ.get("DATAMIND__LLM__MODEL", "claude-sonnet-4-6"),
    )


async def _run(base_url: str, api_key: str, model: str) -> int:
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(base_url=base_url, api_key=api_key)

    print(f"[hello_sdk] gateway = {base_url}")
    print(f"[hello_sdk] model   = {model}")
    print("[hello_sdk] prompt  = 'Reply with just the single word: pong'")
    print("[hello_sdk] --- stream ---")

    got_pong = False
    full_text = ""
    try:
        async with client.messages.stream(
            model=model,
            max_tokens=64,
            messages=[{"role": "user", "content": "Reply with just the single word: pong"}],
        ) as stream:
            async for text in stream.text_stream:
                sys.stdout.write(text)
                sys.stdout.flush()
                full_text += text
            final = await stream.get_final_message()

        print()  # newline after stream

        if "pong" in full_text.lower():
            got_pong = True

        print(
            f"[hello_sdk] usage: input={final.usage.input_tokens} "
            f"output={final.usage.output_tokens} "
            f"cache_read={getattr(final.usage, 'cache_read_input_tokens', 0)} "
            f"cache_create={getattr(final.usage, 'cache_creation_input_tokens', 0)}"
        )
        print(f"[hello_sdk] stop_reason: {final.stop_reason}")
    except Exception as exc:  # noqa: BLE001 — surface everything on smoke test
        print(f"\n[hello_sdk] ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if not got_pong:
        print(
            "[hello_sdk] WARNING: model did not reply with 'pong' exactly. "
            "Transport works but response shape is unexpected.",
            file=sys.stderr,
        )
        return 1

    print("[hello_sdk] OK: gateway reachable, streaming works, model replied 'pong'.")
    return 0


def main() -> None:
    base_url, api_key, model = _check_env()
    rc = asyncio.run(_run(base_url, api_key, model))
    sys.exit(rc)


if __name__ == "__main__":
    main()
