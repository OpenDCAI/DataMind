"""Compare managed DB rows with real DataSpace inputs without calling models."""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sqlite3
from pathlib import Path

from datamind.config import Settings
from datamind.dataspace import task_settings
from datamind.workspace import Workspace
from datamind.capabilities.ingest.tables import quote, _json_rows


async def verify(dataset, runtime, task):
    cfg, context, _ = task_settings(Settings(), dataset, task, runtime)
    workspace = Workspace(cfg)
    manifest = workspace.manifest(workspace.current_revision)
    database = workspace.root / 'revisions' / manifest['workspace_revision'] / manifest['snapshots']['db']['path']
    dest = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)
    checked = []
    try:
        for name, table in manifest['tables'].items():
            source = Path(table['source'])
            original = [m['original_name'] for m in table['column_mapping']]
            columns = [m['name'] for m in table['column_mapping']]
            if table.get('native_sqlite'):
                entry = manifest['databases'][table['database_id']]
                assert entry['storage'] == 'reference' and Path(entry['path']) == source
                from datamind.capabilities.db.native_sqlite import validate_entry
                validate_entry(entry)
                async with workspace.reader(enable={'db'}) as reader:
                    result = await reader.call('db_query_sql', database_id=table['database_id'],
                        sql='SELECT ' + ','.join(quote(c) for c in columns) + ' FROM ' + quote(table['native_table']))
                assert not result['truncated']
                actual = iter(result['rows'])
            else:
                actual = iter(dest.execute('SELECT ' + ','.join(quote(c) for c in columns) + ' FROM ' + quote(name)))
            connection = handle = None
            try:
                if source.suffix == '.json':
                    expected = ([row.get(c) for c in original] for row in _json_rows(source))
                elif source.suffix in {'.sqlite','.sqlite3'}:
                    connection = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)
                    expected = connection.execute('SELECT ' + ','.join(quote(c) for c in original) + ' FROM ' + quote(table['sheet']))
                else:
                    handle = source.open(encoding='utf-8-sig',newline='')
                    expected = csv.reader(handle, delimiter='\t' if source.suffix=='.tsv' else ',')
                    next(expected)
                n = 0
                for row in expected:
                    observed = next(actual, None)
                    if observed is None or list(observed) != list(row):
                        raise AssertionError(f'Row mismatch in {source.name}/{table["sheet"]} at {n + 1}')
                    n += 1
                if next(actual,None) is not None or table['rows'] is not None and n != table['rows']:
                    raise AssertionError('Row count mismatch: ' + table['sheet'])
                checked.append({'source':source.name,'table':table['sheet'],'rows':n})
            finally:
                if connection: connection.close()
                if handle: handle.close()
        async with workspace.reader(enable={'db'}) as reader:
            exported = await reader.call('db_export_sql', sql='WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<1505) SELECT x FROM n')
        assert exported['row_count'] == 1505 and exported['complete']
        return {'task':task,'native_databases':len(manifest.get('databases', {})), 'sqlite_copy_count':sum(e['storage'] != 'reference' for e in manifest.get('databases', {}).values()), 'tables':len(checked),'rows':sum(t['rows'] for t in checked),'checked':checked,
                'exported_rows':exported['row_count'],'export_path':exported['path']}
    finally:
        dest.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset-root',type=Path,required=True)
    parser.add_argument('--runtime-root',type=Path,required=True)
    parser.add_argument('--task',default='task_1')
    args=parser.parse_args()
    print(json.dumps(asyncio.run(verify(args.dataset_root,args.runtime_root,args.task)),ensure_ascii=False))


if __name__=='__main__':
    main()
