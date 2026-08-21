"""Interactive CLI.

    python -m datamind chat              # REPL, streaming answers
    python -m datamind ask "question"    # one-shot
    python -m datamind ingest            # build KB index for current profile
    python -m datamind info              # show active config and tool inventory

Uses `typer` for arg parsing and `rich` for pretty output. Reads are delegated
to RetrieveAgent and writes to StoreAgent.
"""
from __future__ import annotations

import asyncio
import json

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from datamind.agent import build_datamind
from datamind.config import Settings
from datamind.core.context import RequestContext
from datamind.core.logging import bind_context
from datamind.core.logging import setup_logging

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="DataMind — cooperating StoreAgent and RetrieveAgent over five data surfaces.",
)
console = Console()


def _load_settings() -> Settings:
    try:
        return Settings()
    except Exception as exc:  # noqa: BLE001
        console.print(Panel.fit(
            f"[red]Configuration error:[/red] {exc}\n\n"
            f"Did you create .env.datamind from the template? "
            f"Copy .env.datamind.example to .env.datamind and fill in "
            f"DATAMIND__LLM__API_KEY at minimum.",
            title="Cannot start",
        ))
        raise typer.Exit(1)


# ---------------------------------------------------------------- info ---


@app.command()
def info() -> None:
    """Show active configuration and registered tools."""
    setup_logging("WARNING")
    settings = _load_settings()

    t = Table(title="DataMind configuration", show_lines=False)
    t.add_column("Field")
    t.add_column("Value")
    t.add_row("profile", settings.data.profile)
    t.add_row("data_dir", str(settings.data.data_dir))
    t.add_row("storage_dir", str(settings.data.storage_dir))
    t.add_row("llm.api_base", str(settings.llm.api_base))
    t.add_row("llm.protocol", settings.llm.protocol)
    t.add_row("llm.model", settings.llm.model)
    t.add_row("llm.fallback_model", settings.llm.fallback_model)
    t.add_row("embedding.provider", settings.embedding.provider)
    t.add_row("embedding.model", settings.embedding.model)
    t.add_row("retrieval.strategy", settings.retrieval.strategy)
    t.add_row("db.dialect", settings.db.dialect)
    t.add_row("db.dsn", settings.db.dsn or "(auto)")
    t.add_row("graph.backend", settings.graph.backend)
    t.add_row("memory.backend", settings.memory.backend)
    console.print(t)

    async def _run():
        system = await build_datamind(settings)
        tools_table = Table(title="Registered tools", show_lines=False)
        tools_table.add_column("Role")
        tools_table.add_column("Name")
        tools_table.add_column("Surface")
        tools_table.add_column("Group")
        tools_table.add_column("Description")
        for role, registry in (("retrieve", system.retrieve.tools), ("store", system.store.tools)):
            for name in registry.names():
                spec = registry.get(name)
                tools_table.add_row(
                    role,
                    spec.name,
                    spec.metadata.get("surface", "-"),
                    spec.metadata.get("group", "-"),
                    spec.description.split("\n", 1)[0][:80],
                )
        console.print(tools_table)

    asyncio.run(_run())


# ---------------------------------------------------------------- ingest


@app.command()
def ingest() -> None:
    """(Re)build the knowledge-base vector index for the active profile."""
    setup_logging("INFO")
    settings = _load_settings()
    settings.ensure_dirs()

    async def _run():
        system = await build_datamind(settings)
        console.print(Panel.fit(
            f"Indexing [bold]{settings.data.data_dir}[/bold]\n"
            f"into Chroma at [bold]{settings.data.storage_dir / 'chroma'}[/bold]\n"
            f"chunk_size={settings.retrieval.chunk_size} overlap={settings.retrieval.chunk_overlap}",
            title="KB ingest",
        ))
        receipt = await system.store.tools.get("kb_reindex").handler()
        console.print(f"[green]Stored:[/green] {json.dumps(receipt, ensure_ascii=False, indent=2)}")

    asyncio.run(_run())


# ---------------------------------------------------------------- ask ---


@app.command()
def ask(
    question: str = typer.Argument(..., help="The question to ask."),
    session: str = typer.Option("default", help="Session id for memory scoping."),
    show_tools: bool = typer.Option(False, help="Print tool calls and results."),
) -> None:
    """Run a one-shot question; print the streamed answer."""
    setup_logging("WARNING")
    settings = _load_settings()

    async def _run():
        system = await build_datamind(settings)
        agent = system.retrieve
        await agent.warmup()
        console.print(Panel.fit(question, title="Q", style="cyan"))
        context = RequestContext(session_id=session, profile=settings.data.profile)
        with bind_context(context):
            async for event in agent.loop.stream_turn(user_message=question):
                if event.type == "text":
                    console.print(event.data["delta"], end="")
                elif event.type == "tool_use" and show_tools:
                    console.print(
                        f"\n[dim][tool]:[/dim] [bold]{event.data['name']}[/bold] {json.dumps(event.data['input'], ensure_ascii=False)[:200]}"
                    )
                elif event.type == "tool_result" and show_tools:
                    marker = "[red]ERR[/red]" if event.data.get("is_error") else "[green]ok[/green]"
                    preview = (event.data.get("preview") or "")[:200].replace("\n", " ")
                    console.print(f"[dim][result {marker}][/dim] {preview}")
                elif event.type == "done":
                    console.print()
                    console.print(
                        f"[dim]done ({event.data.get('iterations')} iterations, "
                        f"stop={event.data.get('stop_reason')})[/dim]"
                    )

    asyncio.run(_run())


# ---------------------------------------------------------------- store ---


@app.command("store")
def store_command(
    instruction: str = typer.Argument(..., help="What data should be stored and where."),
    session: str = typer.Option("default", help="Session id for memory scoping."),
    show_tools: bool = typer.Option(True, help="Print tool calls and receipt previews."),
) -> None:
    """Route data into KB / DB / Graph / Skills / Memory through StoreAgent."""
    setup_logging("WARNING")
    settings = _load_settings()

    async def _run():
        system = await build_datamind(settings)
        console.print(Panel.fit(instruction, title="Store", style="magenta"))
        context = RequestContext(session_id=session, profile=settings.data.profile)
        with bind_context(context):
            async for event in system.store.loop.stream_turn(user_message=instruction):
                if event.type == "text":
                    console.print(event.data["delta"], end="")
                elif event.type == "tool_use" and show_tools:
                    console.print(
                        f"\n[dim][store]:[/dim] [bold]{event.data['name']}[/bold] "
                        f"{json.dumps(event.data['input'], ensure_ascii=False)[:240]}"
                    )
                elif event.type == "tool_result" and show_tools:
                    marker = "[red]ERR[/red]" if event.data.get("is_error") else "[green]receipt[/green]"
                    preview = (event.data.get("preview") or "")[:300].replace("\n", " ")
                    console.print(f"[dim][{marker}][/dim] {preview}")
                elif event.type == "done":
                    console.print()

    asyncio.run(_run())


# ---------------------------------------------------------------- chat ---


@app.command()
def chat(
    session: str = typer.Option("default", help="Session id for memory scoping."),
    show_tools: bool = typer.Option(True, help="Print tool calls and results."),
) -> None:
    """Interactive REPL. `exit`, `quit`, or Ctrl-D to leave; `new` to reset history."""
    setup_logging("WARNING")
    settings = _load_settings()

    async def _run():
        system = await build_datamind(settings)
        agent = system.retrieve
        warmup = await agent.warmup()
        console.print(Panel.fit(
            f"[bold]DataMind[/bold] ready · profile=[cyan]{settings.data.profile}[/cyan] · "
            f"model=[cyan]{settings.llm.model}[/cyan]\n"
            f"tools={len(agent.tools)} · kb_chunks={warmup['kb_chunks']} · "
            f"graph_triples={warmup.get('graph', {}).get('triples_loaded', 0)} · "
            f"skills={warmup['skills']['manifests']}\n\n"
            f"type /exit to quit, /new to reset history",
            title="Chat",
        ))
        history: list[dict] = []
        while True:
            try:
                q = console.input("[cyan]you ›[/cyan] ").strip()
            except (EOFError, KeyboardInterrupt):
                console.print()
                break
            if not q:
                continue
            if q in {"/exit", "/quit"}:
                break
            if q in {"/new", "/reset"}:
                history = []
                console.print("[dim](history cleared)[/dim]")
                continue
            if q.startswith("/"):
                console.print(f"[yellow]unknown command {q!r}[/yellow]")
                continue

            console.print("[green]ai  ›[/green] ", end="")
            collected = []
            context = RequestContext(session_id=session, profile=settings.data.profile)
            with bind_context(context):
                async for event in agent.loop.stream_turn(user_message=q, history=history):
                    if event.type == "text":
                        delta = event.data["delta"]
                        collected.append(delta)
                        console.print(delta, end="")
                    elif event.type == "tool_use" and show_tools:
                        console.print(
                            f"\n[dim][tool {event.data['name']}] "
                            f"{json.dumps(event.data['input'], ensure_ascii=False)[:160]}[/dim]"
                        )
                    elif event.type == "tool_result" and show_tools:
                        marker = "[red]ERR[/red]" if event.data.get("is_error") else "[green]ok[/green]"
                        preview = (event.data.get("preview") or "")[:160].replace("\n", " ")
                        console.print(f"[dim][result {marker}] {preview}[/dim]")
                    elif event.type == "done":
                        console.print()
                        # Save a simplified turn in history.
                        answer_text = "".join(collected)
                        history.append({"role": "user", "content": q})
                        history.append({"role": "assistant", "content": answer_text or "…"})

    asyncio.run(_run())


if __name__ == "__main__":
    app()
