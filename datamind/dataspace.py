"""Explicit, task-isolated DataSpace test entry point; ordinary runs keep their defaults."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import shutil
from pathlib import Path

from datamind.config import Settings
from datamind.workspace import Workspace


def task_settings(settings, dataset_root, task_id, runtime_root):
    if not re.fullmatch(r'task_[1-9][0-9]*', task_id):
        raise ValueError('Expected a task_N identifier')
    root = Path(dataset_root).resolve()
    task = root / 'input' / task_id
    context = (task / 'context').resolve()
    if not context.is_dir() or not context.is_relative_to(root):
        raise ValueError('Task context is missing or outside the dataset')
    question = json.loads((task / 'task.json').read_text(encoding='utf-8'))['question']
    cfg = settings.model_copy(deep=True)
    cfg.data.base_dir = Path(runtime_root).resolve()
    cfg.data.profile = 'dataspace_' + task_id
    cfg.workspace.input_policy = 'dataspace'
    cfg.db.row_limit = None
    cfg.hooks.path_allowlist_extra = [str(context)]
    cfg.codex.workspace = cfg.data.data_dir / 'agent'
    return cfg, context, question


def save_prediction(cfg, result_id, revision, output, *, request_id=None):
    if not re.fullmatch(r'[a-f0-9]{32}', result_id):
        raise ValueError('Expected a managed SQL export result_id')
    root = cfg.data.storage_dir / 'query_results'
    if request_id is not None:
        from datamind.capabilities.db.query_contract import validate_answer_export
        validate_answer_export(root, result_id, profile=cfg.data.profile, scope=request_id)
    metadata = json.loads((root / (result_id + '.json')).read_text())
    if not metadata.get('complete') or metadata.get('truncated') or metadata.get('workspace_revision') != revision:
        raise ValueError('Prediction must be complete and belong to the pinned task revision')
    if request_id is not None and (metadata.get('request_id') != request_id or metadata.get('profile') != cfg.data.profile):
        raise ValueError('Prediction must belong to this task profile and request')
    source = root / (result_id + '.csv')
    sha = hashlib.sha256()
    with source.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            sha.update(block)
    if sha.hexdigest() != metadata['sha256']:
        raise ValueError('Query result integrity check failed')
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix('.csv.tmp')
    shutil.copyfile(source, temporary)
    temporary.replace(output)
    return {'prediction': str(output), 'rows': metadata['row_count'], 'workspace_revision': revision}


async def solve_on_demand(cfg, context, question, task_id, runtime_root, surfaces, host_factory):
    """One main-model session selects ordinary DataMind tools over live stores."""
    files = [{'path': str(p.relative_to(context)), 'bytes': p.stat().st_size}
             for p in sorted(context.rglob('*')) if p.is_file() and p.resolve().is_relative_to(context)]
    prompt = (
        f'Answer this DataSpace question using only input files under {context}.\n'
        f'Question: {question}\nInput files: {json.dumps(files, ensure_ascii=False)}\n'
        'Use DataMind on-demand tools in this single write-enabled request. Choose KB, DB, Graph '
        'or direct file/video reads as useful for this question. There is no requirement to build all '
        'surfaces, process every file, extract graph triples or publish a workspace. '
        'Independent live stores are ready for writes and reads in the same operation. '
        'For CSV/XLSX or JSON envelopes {table, records}, db_import_path imports the chosen file '
        'without retyping rows; inspect returned table/column mappings. For SQLite, db_import_path '
        'registers a read-only source; use the returned database_id with db_list_tables, '
        'db_describe_table, db_query_sql and db_export_sql. Omit database_id for imported tables. '
        'Use raw_file_read for small documents or kb_add_path/kb_search when retrieval is useful; '
        'kb_search only searches files already indexed in this task; an empty KB is not evidence that source documents lack the answer. '
        'Use Graph only if relationships help answer the question. '
        'For MP4, video_prepare extracts frames with optional ocr/asr. kb_video_read and '
        'kb_video_frame work directly without publication. View original frame images when visual '
        'evidence is relevant; OCR alone can miss charts or layout. Disabled OCR/ASR is not evidence '
        'of absent text/speech. A failed tool is not successful processing: inspect the error, retry '
        'when appropriate, or select another available evidence tool. '
        'Do not read other tasks, reference answers or evaluation configs, or save facts to memory. '
        'Wait for tool completion before reporting the final result; task_status reports unfinished jobs. '
        'Return all and only the columns requested by the question, without extra evidence/provenance '
        'columns. Preserve all requested rows, duplicates, order, original units and values. '
        'Use the generic db_answer_schema and query intent mechanism, then db_export_sql with purpose=answer '
        'to export the complete final answer; a SELECT of literal values is permitted '
        'for document/video answers. A preview is not the full CSV. Return its result_id using the schema.'
    )
    host = host_factory(cfg, surfaces=set(surfaces) | {'skills'})
    try:
        from datamind.capabilities.db.query_contract import MANAGED_ANSWER_SCHEMA, ManagedAnswerExportValidator
        result = await host.ingest(prompt, final_contract=MANAGED_ANSWER_SCHEMA,
            final_validator=ManagedAnswerExportValidator(cfg.data.storage_dir / 'query_results', cfg.data.profile))
    finally:
        await host.aclose()
    result_id = json.loads(result['answer'])['result_id']
    return save_prediction(cfg, result_id, None,
        Path(runtime_root) / 'predictions' / task_id / 'prediction.csv', request_id=result['request_id'])


async def run_task(settings, dataset_root, task_id, runtime_root, *, phase='run', surfaces=('kb', 'graph', 'db'), sql=None, database_id=None, host_factory=None):
    from datamind.host import CodexHost
    host_factory = host_factory or CodexHost
    cfg, context, question = task_settings(settings, dataset_root, task_id, runtime_root)
    workspace = Workspace(cfg)
    selected = set(surfaces)
    if phase not in {'build', 'query', 'run'} or not selected or not selected <= {'kb', 'graph', 'db'}:
        raise ValueError('Invalid test phase or workspace surfaces')
    cfg.codex.workspace.mkdir(parents=True, exist_ok=True)
    if phase == 'run' and sql is None:
        return await solve_on_demand(cfg, context, question, task_id, runtime_root, surfaces, host_factory)
    parent = workspace.current_revision
    if phase in {'build', 'run'}:
        if set(surfaces) == {'db'}:
            await workspace.update(str(context), surfaces=['db'])
        else:
            host = host_factory(cfg, surfaces=set(surfaces) | {'skills'})
            try:
                await host.ingest(
                    f'Build the complete task workspace at {context}. Use workspace-ingest and publish surfaces {list(surfaces)}. '
                    'Use workspace_prepare inventory. structured_table sources get deterministic schema/foreign-key evidence; '
                    'extract graph_sources for the remaining documents and MP4 using the existing video workflow. '
                    'Do not read reference answers, evaluation configs, other tasks, or save task facts to memory.')
            finally:
                await host.aclose()
    revision = workspace.current_revision
    if revision is None or workspace.manifest(revision)['source_root'] != str(context):
        raise ValueError('No published workspace for this task')
    manifest = workspace.manifest(revision)
    if manifest.get('input_policy') != 'dataspace':
        raise ValueError('Rebuild this task using the DataSpace input policy')
    sqlite_sources = {s['uri'] for s in manifest['sources'] if Path(s['uri']).suffix.lower() in {'.sqlite', '.sqlite3'}}
    registered = {e['source_uri'] for e in manifest.get('databases', {}).values() if e['storage'] == 'reference'}
    if sqlite_sources != registered:
        raise ValueError('Rebuild this DataSpace workspace to register native SQLite references')
    if phase in {'build', 'run'}:
        # The benchmark inputs are immutable. Check task coverage, not versions.
        expected = await asyncio.to_thread(workspace._inventory, context, fingerprint=False)
        if revision == parent or not selected <= set(manifest['surfaces']):
            raise ValueError('Build did not publish the requested workspace surfaces')
        if {s['uri'] for s in expected} != {s['uri'] for s in manifest['sources']}:
            raise ValueError('Published workspace does not contain the complete current task inputs')
    if phase == 'build':
        return {'task_id': task_id, 'workspace_revision': revision, 'sources': len(manifest['sources'])}
    if sql is not None:
        async with workspace.reader(revision, enable={'db'}) as reader:
            exported = await reader.call('db_export_sql', sql=sql, database_id=database_id)
        result_id = exported['result_id']
    else:
        host = host_factory(cfg, revision=revision, surfaces=set(manifest['surfaces']) | {'skills'})
        try:
            result = await host.query(
                question + '\n\nUse only this task context and the pinned DataMind surfaces. Do not consult other tasks, reference answers, evaluation configs or memory. '
                'This is a DataSpace test with immutable inputs: call db_list_databases to discover native SQLite sources. Pass database_id to db_list_tables, db_describe_table, db_query_sql and db_export_sql; use original SQLite table and column names. SQLite files are read-only references, not imported tables; do not recheck source versions. Omitting database_id selects the managed CSV/JSON database. '
                'The DB result row cap is disabled. Preserve all answer rows, duplicates, requested order, columns, units and values. '
                'Use db_export_sql for the final answer table and return its result_id only in the requested JSON schema. '
                'The export preview is not the full result. For answers obtained from documents/video, a SELECT of literal values is also allowed. '
                'Use the existing video retrieval Skill; do not assume OCR contains all visual information.',
                final_contract={'type': 'object', 'properties': {'result_id': {'type': 'string'}},
                                'required': ['result_id'], 'additionalProperties': False})
        finally:
            await host.aclose()
        result_id = json.loads(result['answer'])['result_id']
    return save_prediction(cfg, result_id, revision, Path(runtime_root) / 'predictions' / task_id / 'prediction.csv')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset-root', type=Path, required=True)
    parser.add_argument('--runtime-root', type=Path, required=True)
    parser.add_argument('--task', required=True)
    parser.add_argument('--phase', choices=['build', 'query', 'run'], default='run')
    parser.add_argument('--surfaces', default='kb,graph,db')
    parser.add_argument('--sql', help='Explicit read-only SQL for offline validation instead of model solving')
    parser.add_argument("--database-id", help="Registered native SQLite ID for --sql")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run_task(Settings(), args.dataset_root, args.task, args.runtime_root,
        phase=args.phase, surfaces=tuple(args.surfaces.split(',')), sql=args.sql, database_id=args.database_id)), ensure_ascii=False))


if __name__ == '__main__':
    main()
