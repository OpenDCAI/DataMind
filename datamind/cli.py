"""Codex CLI host plus direct DataMind data operations."""
from __future__ import annotations
import asyncio
import json
import uuid

import typer
from rich.console import Console
from datamind.config import DataSettings
from datamind.core.context import RequestContext
from datamind.core.logging import bind_context
from datamind.data_plane import build_data_plane
from datamind.host import CodexHost

app = typer.Typer(no_args_is_help=True, add_completion=False,
                  help='DataMind — shared workspace data tools for the main Codex agent.')
console = Console()


def _settings():
    return DataSettings()


async def _show(host, message, *, session, history=None, mode='read', show_tools=False):
    answer = ''
    displayed = ''
    with bind_context(RequestContext(session_id=session,profile=host.settings.data.profile)):
        async for event in host.stream(message,history=history,mode=mode):
            if event['type'] == 'text':
                delta = event.get('delta','')
                displayed += delta
                console.print(delta,end='',markup=False)
            elif event['type'] == 'tool_use' and show_tools:
                console.print('\n[tool] '+event['name'],markup=False)
            elif event['type'] == 'tool_result' and show_tools:
                console.print('\n[result] '+event.get('preview',''),markup=False)
            elif event['type'] == 'done':
                answer=event.get('answer','')
                if answer and not displayed.endswith(answer):
                    if displayed:
                        console.print()
                    console.print(answer,end='',markup=False)
                for receipt in event.get('receipts',[]):
                    console.print('\n[receipt] '+json.dumps(receipt,ensure_ascii=False),markup=False)
    console.print()
    return answer


@app.command()
def info():
    """Show data configuration and direct tools without starting a model."""
    cfg=_settings()
    from datamind.mcp import definitions
    console.print(json.dumps({'backend':'codex','model':cfg.codex.model,'profile':cfg.data.profile,
        'data_dir':str(cfg.data.data_dir),'storage_dir':str(cfg.data.storage_dir),
        'embedding_provider':cfg.embedding.provider,'tools':list(definitions())},ensure_ascii=False,indent=2),markup=False)


@app.command()
def ingest():
    """Rebuild the current profile KB directly, with a checked receipt."""
    async def run():
        cfg=_settings()
        data=await build_data_plane(cfg,enable={'kb'})
        try:
            with bind_context(RequestContext.new(profile=cfg.data.profile)):
                result=await data.call('kb_reindex')
            console.print(json.dumps(result,ensure_ascii=False,indent=2),markup=False)
            if any(item['status']=='failed' for item in result.get('results',[])):
                raise typer.Exit(1)
        finally:
            await data.aclose()
    asyncio.run(run())


def _one(message, session, show_tools, mode):
    async def run():
        host=CodexHost(_settings())
        try:
            await _show(host,message,session=session,mode=mode,show_tools=show_tools)
        finally:
            await host.aclose()
    asyncio.run(run())


@app.command()
def ask(question: str, session: str=typer.Option('default'),show_tools: bool=typer.Option(False)):
    """Let the main Codex agent answer with read-only data access."""
    _one(question,session,show_tools,'read')


@app.command('store')
def store_command(instruction: str,session: str=typer.Option('default'),show_tools: bool=typer.Option(True)):
    """Let the same Codex agent ingest through explicit data tools."""
    _one(instruction,session,show_tools,'write')


@app.command()
def chat(session: str=typer.Option('default'),show_tools: bool=typer.Option(True)):
    """Chat with Codex; /store permits writes, /new resets context."""
    async def run():
        host=CodexHost(_settings());history=[];current=session
        try:
            while True:
                try:
                    message=console.input('you › ').strip()
                except (EOFError,KeyboardInterrupt):
                    break
                if message in {'/quit','/exit'}:break
                if message in {'/new','/reset'}:
                    history=[];current=uuid.uuid4().hex;continue
                if not message:continue
                mode='write' if message.startswith('/store ') else 'read'
                message=message.removeprefix('/store ') if mode=='write' else message
                answer=await _show(host,message,session=current,history=history,mode=mode,show_tools=show_tools)
                history.extend([{'role':'user','content':message},{'role':'assistant','content':answer}])
        finally:await host.aclose()
    asyncio.run(run())


def _workspace():
    from datamind.workspace import Workspace
    return Workspace(_settings())


def _print(value):
    console.print(json.dumps(value, ensure_ascii=False, indent=2, default=str), markup=False, soft_wrap=True)


@app.command('workspace-status')
def workspace_status(usage: bool = False):
    """Show history, retention and optionally allocated storage."""
    workspace = _workspace()
    result = workspace.history()
    if usage: result['storage'] = workspace.storage_usage()
    _print(result)


@app.command('workspace-policy')
def workspace_policy(mode: str, keep_last: int = typer.Option(10, min=1)):
    """Set latest, recent N, or all. Cleanup runs after publication or workspace-gc."""
    if mode not in {'latest', 'recent', 'all'}:
        raise typer.BadParameter('Use latest, recent, or all')
    _print(_workspace().configure_retention(mode, keep_last))


@app.command('workspace-gc')
def workspace_gc(apply: bool = False, compact: bool = False):
    """Preview cleanup; --apply removes unreferenced history; --compact reclaims provider storage."""
    _print(asyncio.run(_workspace().collect(dry_run=not apply, compact=compact)))


@app.command('workspace-rollback')
def workspace_rollback(revision: str, expected_revision: str = typer.Option(None)):
    """Select a retained revision atomically."""
    _print(_workspace().rollback(revision, expected_revision=expected_revision))


@app.command('workspace-protect')
def workspace_protect(revision: str, remove: bool = False):
    """Protect a retained version from cleanup; --remove releases protection."""
    _print(_workspace().protect(revision, not remove))


@app.command('workspace-migrate')
def workspace_migrate():
    """Migrate the frozen current revision to incremental storage without model calls."""
    _print(asyncio.run(_workspace().migrate()))


if __name__=='__main__':app()
