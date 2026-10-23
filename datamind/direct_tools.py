"""Tool definitions shared by plugin, Python callers, and the Codex SDK host."""
from types import SimpleNamespace
from pathlib import Path

from datamind.capabilities.db import build_db_tools
from datamind.capabilities.graph import build_graph_tools
from datamind.capabilities.ingest import build_ingest_tools
from datamind.capabilities.kb import build_kb_tools
from datamind.capabilities.memory import build_memory_tools
from datamind.capabilities.skills import build_skills_tools, build_skills_store_tools
from datamind.core.tools import ToolSpec

DIRECT_NAMES = frozenset('''
raw_file_read workspace_inspect build_start build_status build_freeze build_verify build_export
kb_search kb_count kb_list_documents kb_add_text kb_add_file kb_add_path kb_delete_file
 db_list_tables db_describe_table db_query_sql db_import_path db_import_records db_update_path db_delete_path
 graph_schema graph_search_entities graph_neighbors graph_traverse graph_upsert_triples graph_build_lineage
 skill_list skill_get skill_search skill_upsert memory_save memory_recall memory_forget
'''.split())


def catalogue():
    """Schema discovery does not open databases, vector stores or model clients."""
    stub = SimpleNamespace(data_dir=Path('.'), ingest_mode='direct', code_tools=[])
    specs = (build_db_tools(None) + build_graph_tools(None) + build_kb_tools(stub)
             + build_ingest_tools(stub) + build_memory_tools(None)
             + build_skills_tools(stub) + build_skills_store_tools(None))
    return {**{s.name: s for s in specs if s.name in DIRECT_NAMES},
            **{s.name: s for s in graph_source_tools(None)}}


def workspace_tools(settings, active=None):
    from datamind.data_plane import build_data_plane
    from datamind.workspace import Workspace
    enabled = {'kb', 'db', 'graph'} if active is None else set(active) & {'kb', 'db', 'graph'}

    async def prepare(path):
        # Use the same policy as direct file tools before revealing inventory.
        from datamind.capabilities.ingest import build_ingest_service
        service = build_ingest_service(settings=settings, kb=None, db=None, graph=None, llm_client=None)
        from datamind.capabilities.ingest.service import _resolve_safe_path
        source = _resolve_safe_path(path, service._allowed_roots)
        workspace = Workspace(settings, builder=build_data_plane)
        import asyncio
        from datamind.workspace_incremental import incremental_enabled
        sources = await asyncio.to_thread(workspace._inventory, source)
        lease, revision = workspace.acquire_lease()
        try:
            previous = workspace.manifest(revision) if revision else None
            prior = {s['uri']: s['version'] for s in previous['sources']} if previous else {}
            with workspace._connect() as db:
                current, epoch = db.execute('SELECT revision,epoch FROM head WHERE singleton=1').fetchone()
            if current != revision:
                raise ValueError('Workspace changed during preparation; prepare again')
            return {'path': str(source), 'sources': sources, 'current_revision': revision,
                    'current_epoch': epoch, 'retention': workspace.retention,
                    'storage_mode': 'incremental' if incremental_enabled(workspace, enabled) else 'snapshot',
                    'changes': {'new': [s['uri'] for s in sources if s['uri'] not in prior],
                        'modified': [s['uri'] for s in sources if s['uri'] in prior and s['version'] != prior[s['uri']]],
                        'deleted': sorted(set(prior) - {s['uri'] for s in sources}),
                        'unchanged': [s['uri'] for s in sources if s['version'] == prior.get(s['uri'])]},
                    'instructions': 'In incremental mode extract graph assertions for new/changed documents only. Snapshot mode requires all documents. Use inventory SHA-256 versions; include explicit empty triples where none were found. Optional table_mappings can confirm header/data source rows and field names/units; bind them to these source hashes.'}
        finally:
            workspace.release_lease(lease)

    async def publish(path, surfaces=None, graph_sources=None, relation_types=None, table_mappings=None):
        await prepare(path)
        selected = surfaces if surfaces is not None else ['kb', 'graph', 'db']
        if not set(selected) <= enabled:
            raise ValueError('Requested workspace surfaces are not enabled in this data runtime')
        if 'graph' in selected and graph_sources is None:
            raise ValueError('graph_sources is required: Codex must submit extracted triples, including explicit empty lists')
        return await Workspace(settings, builder=build_data_plane).update(path, surfaces=selected, graph_sources=graph_sources,
                                      relation_types=relation_types, table_mappings=table_mappings)

    async def history():
        return Workspace(settings).history()

    async def catalog(**kwargs):
        async with Workspace(settings, builder=build_data_plane).reader(enable=set()) as reader:
            return await reader.call('workspace_catalog', **kwargs)

    async def read_asset(**kwargs):
        async with Workspace(settings, builder=build_data_plane).reader(enable=set()) as reader:
            return await reader.call('workspace_read_asset', **kwargs)

    async def export_bundle(**kwargs):
        async with Workspace(settings, builder=build_data_plane).reader(enable=set()) as reader:
            return await reader.call('workspace_export_bundle', **kwargs)

    async def policy(mode, keep_last=10):
        return Workspace(settings).configure_retention(mode, keep_last)

    async def gc(dry_run=True, compact=False):
        return await Workspace(settings).collect(dry_run=dry_run, compact=compact)

    async def rollback(revision, expected_revision=None):
        return Workspace(settings).rollback(revision, expected_revision=expected_revision)

    async def protect(revision, enabled=True):
        return Workspace(settings).protect(revision, enabled)

    async def migrate():
        manifest = Workspace(settings).manifest(Workspace(settings).current_revision)
        if not set(manifest['surfaces']) <= enabled:
            raise ValueError('Migration surfaces exceed this runtime capability set')
        return await Workspace(settings, builder=build_data_plane).migrate()

    metadata = {'access': 'write', 'workspace_management': True}
    management = [
        ToolSpec('workspace_catalog', 'Find published sources or SQL tables by filename/path, sheet or column. Paginated; no database/model call. Check coverage before repeating native reads.',
                 {'type': 'object', 'properties': {
                     'kind': {'enum': ['sources', 'tables'], 'default': 'sources'},
                     'query': {'type': 'string'}, 'source': {'type': 'string'},
                     'sheet': {'type': 'string'}, 'column': {'type': 'string'},
                     'offset': {'type': 'integer', 'minimum': 0, 'default': 0},
                     'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 20}}},
                 catalog, {'access': 'read'}),
        ToolSpec('workspace_read_asset', 'Read reusable published text or a bounded sheet row range, with source version and revision evidence. Does not re-parse cached assets or run SQL. Offsets count characters for text, data rows for sheets.',
                 {'type': 'object', 'properties': {
                     'path': {'type': 'string'}, 'sheet': {'type': 'string'},
                     'offset': {'type': 'integer', 'minimum': 0, 'default': 0},
                     'limit': {'type': 'integer', 'minimum': 1, 'maximum': 200, 'default': 100},
                     'max_chars': {'type': 'integer', 'minimum': 1, 'maximum': 100000, 'default': 20000},
                     'columns': {'type': 'array', 'maxItems': 100, 'uniqueItems': True,
                                 'items': {'type': 'string'}}}, 'required': ['path']},
                 read_asset, {'access': 'read'}),
        ToolSpec('workspace_export_bundle', 'Export multiple pinned sources/sheets once as local JSONL/text plus a field/cell evidence manifest. Reuse these files directly in Python and deliverables instead of parsing original files again. Requires a shared filesystem with agent code; derived cache writes only.',
                 {'type': 'object', 'properties': {
                     'requests': {'type': 'array', 'minItems': 1, 'maxItems': 20, 'items': {
                         'type': 'object', 'properties': {'path': {'type': 'string'}, 'sheet': {'type': 'string'},
                             'columns': {'type': 'array', 'minItems': 1, 'maxItems': 1000, 'uniqueItems': True, 'items': {'type': 'string'}}},
                         'required': ['path'], 'additionalProperties': False}},
                     'view_mode': {'enum': ['data', 'raw'], 'default': 'data'},
                     'max_rows': {'type': 'integer', 'minimum': 1, 'maximum': 1000000, 'default': 100000},
                     'max_bytes': {'type': 'integer', 'minimum': 1024, 'maximum': 134217728, 'default': 33554432}},
                  'required': ['requests']}, export_bundle, {'access': 'read'}),
        ToolSpec('workspace_history', 'List retained and pruned Workspace revisions and retention policy.',
                 {'type': 'object', 'properties': {}}, history, {'access': 'read'}),
        ToolSpec('workspace_retention', 'Choose latest, recent N, or all history. Applies to future cleanup; does not immediately delete data.',
                 {'type': 'object', 'properties': {'mode': {'enum': ['latest','recent','all']},
                  'keep_last': {'type': 'integer', 'minimum': 1}}, 'required': ['mode']}, policy, metadata),
        ToolSpec('workspace_gc', 'Preview or apply cleanup. Active readers and protected revisions are retained. compact also reclaims native provider history.',
                 {'type': 'object', 'properties': {'dry_run': {'type': 'boolean', 'default': True},
                  'compact': {'type': 'boolean', 'default': False}}}, gc, metadata),
        ToolSpec('workspace_rollback', 'Atomically select a retained revision; optional expected_revision prevents overwriting a concurrent publication.',
                 {'type': 'object', 'properties': {'revision': {'type': 'string'},
                  'expected_revision': {'type': 'string'}}, 'required': ['revision']}, rollback, metadata),
        ToolSpec('workspace_protect', 'Protect or unprotect a retained revision from history cleanup.',
                 {'type': 'object', 'properties': {'revision': {'type': 'string'},
                  'enabled': {'type': 'boolean', 'default': True}}, 'required': ['revision']}, protect, metadata),
        ToolSpec('workspace_migrate', 'Explicitly migrate the current frozen snapshot into shared incremental storage without model calls. Requires native providers.',
                 {'type': 'object', 'properties': {}}, migrate, metadata),
    ]
    return management + [
        ToolSpec('workspace_prepare', 'Inventory sources and hashes before Codex extracts graph assertions.',
                 {'type': 'object', 'properties': {'path': {'type': 'string'}}, 'required': ['path']},
                 prepare, {'access': 'read'}),
        ToolSpec('workspace_publish', 'Validate Codex assertions and atomically publish a consistent KB/Graph/DB revision. Does not mutate direct live stores.',
                 {'type': 'object', 'properties': {
                     'path': {'type': 'string'},
                     'surfaces': {'type': 'array', 'minItems': 1, 'uniqueItems': True,
                                  'items': {'enum': ['kb', 'graph', 'db']}},
                     'relation_types': {'type': 'array', 'items': {'type': 'string'}},
                     'graph_sources': {'type': 'array', 'items': {'type': 'object',
                         'properties': {'uri': {'type': 'string'}, 'version': {'type': 'string'},
                                        'triples': {'type': 'array', 'items': {'type': 'object'}},
                                        'truncated': {'type': 'boolean'}},
                         'required': ['uri', 'version', 'triples'], 'additionalProperties': False}},
                     'table_mappings': {'type': 'array', 'maxItems': 1000, 'items': {
                         'type': 'object', 'properties': {
                             'uri': {'type': 'string'}, 'version': {'type': 'string', 'pattern': '^sha256:[a-f0-9]{64}$'},
                             'sheets': {'type': 'array', 'maxItems': 100, 'items': {'type': 'object', 'properties': {
                                 'sheet': {'type': 'string'}, 'header_row': {'type': 'integer', 'minimum': 1},
                                 'data_start_row': {'type': 'integer', 'minimum': 2}, 'data_end_row': {'type': 'integer', 'minimum': 1},
                                 'fields': {'type': 'array', 'maxItems': 1000, 'items': {'type': 'object', 'properties': {
                                     'column': {'type': 'string'}, 'name': {'type': 'string', 'minLength': 1, 'maxLength': 200},
                                     'unit': {'type': ['string', 'null'], 'maxLength': 200}},
                                     'required': ['column', 'name'], 'additionalProperties': False}}},
                                 'required': ['sheet', 'header_row'], 'additionalProperties': False}}},
                         'required': ['uri', 'version', 'sheets'], 'additionalProperties': False}},
                 }, 'required': ['path']}, publish, {'access': 'write', 'surface': 'graph', 'multi_surface': True}),
    ]


def graph_source_tools(ingest):
    """Replace only one source's support; validate before retracting anything."""
    from datamind.capabilities.ingest.service import _resolve_safe_path
    from datamind.capabilities.graph.evidence import persist_bytes
    from datamind.core.protocols import GraphTriple, GraphEvidence
    import hashlib

    async def replace_source(path, expected_version, triples):
        source = _resolve_safe_path(path, ingest._allowed_roots)
        raw = source.read_bytes()
        version = 'sha256:' + hashlib.sha256(raw).hexdigest()
        if version != expected_version:
            raise ValueError('Source changed; read it again before graph replacement')
        parsed = [GraphTriple(**value) for value in triples]
        evidence_path, _ = persist_bytes(ingest._profile_dir, raw, source.suffix)
        for triple in parsed:
            triple.source = str(source)
            triple.properties.update(_source_managed=True, _source_path=str(source))
            triple.evidence = [GraphEvidence(source_id='file:' + hashlib.sha256(str(source).encode()).hexdigest(),
                source_version=version, locator={'uri': str(evidence_path), 'original_uri': str(source)},
                method='extracted', pipeline_version='codex-explicit-v1')]
        await ingest._commit_graph_source(str(source), parsed)
        return {'source': str(source), 'source_version': version, 'triples_added': len(parsed)}

    async def forget_source(path):
        source = _resolve_safe_path(path, ingest._allowed_roots)
        await ingest._commit_graph_source(str(source), [])
        return {'source': str(source), 'retracted': True, 'source_file_deleted': False}

    return [
        ToolSpec('graph_replace_source', 'Replace graph assertions from one source after Codex extraction; validates the original content hash. Empty triples retract that source only.',
                 {'type': 'object', 'properties': {'path': {'type': 'string'},
                     'expected_version': {'type': 'string', 'pattern': '^sha256:[a-f0-9]{64}$'},
                     'triples': {'type': 'array', 'maxItems': 10000, 'items': {'type': 'object'}}},
                  'required': ['path','expected_version','triples']}, replace_source,
                 {'surface': 'graph', 'access': 'write'}),
        ToolSpec('graph_forget_source', 'Retract one source’s managed graph support, preserving other sources and the original file.',
                 {'type': 'object', 'properties': {'path': {'type': 'string'}}, 'required': ['path']},
                 forget_source, {'surface': 'graph', 'access': 'write'}),
    ]
