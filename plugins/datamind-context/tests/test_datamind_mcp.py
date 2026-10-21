from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from datamind.mcp import definitions, handle


def test_catalogue_exposes_direct_tools_without_nested_agents():
    tools = definitions()
    assert {'datamind_operation_begin', 'datamind_graph_schema', 'datamind_graph_neighbors',
            'datamind_db_query_sql', 'datamind_skill_get', 'datamind_workspace_publish'} <= tools.keys()
    assert not {'datamind_ask', 'datamind_store', 'datamind_graph_query', 'datamind_workspace_ask'} & tools.keys()
    assert tools['datamind_graph_neighbors']['annotations']['readOnlyHint']
    assert not tools['datamind_graph_upsert_triples']['annotations']['readOnlyHint']


def test_initialize_teaches_codex_main_agent_contract():
    result = handle({'jsonrpc':'2.0','id':1,'method':'initialize'})['result']
    assert 'main agent' in result['instructions']
    assert result['serverInfo']['version'] == '2.0.0'


def test_plugin_and_wheel_entrypoints_run_real_stdio_without_llm(tmp_path):
    plugin = Path(__file__).parents[1] / 'src' / 'datamind_mcp.py'
    repo = plugin.parents[3]
    env = {k:v for k,v in os.environ.items() if not k.startswith('DATAMIND')}
    env.update(DATAMIND_REPO_ROOT=str(repo), DATAMIND_DATA_ROOT=str(tmp_path),
               PYTHONPATH=str(repo))
    requests = [
        {'jsonrpc':'2.0','id':1,'method':'initialize'},
        {'jsonrpc':'2.0','method':'notifications/initialized'},
        {'jsonrpc':'2.0','id':2,'method':'tools/list'},
        {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{
            'name':'datamind_operation_begin','arguments':{'mode':'write'}}},
        {'jsonrpc':'2.0','id':4,'method':'tools/call','params':{
            'name':'datamind_graph_schema','arguments':{'operation_id':'invalid'}}},
    ]
    for command in ([sys.executable, str(plugin)], [sys.executable, '-m', 'datamind.mcp']):
        process = subprocess.run(command, input='\n'.join(json.dumps(r) for r in requests)+'\n',
                                 text=True, capture_output=True, cwd=tmp_path, env=env, timeout=30)
        assert process.returncode == 0, process.stderr
        replies = [json.loads(line) for line in process.stdout.splitlines()]
        assert [r['id'] for r in replies] == [1,2,3,4]
        assert len(replies[1]['result']['tools']) > 20
        assert replies[2]['result']['structuredContent']['operation_id']
        assert replies[3]['result']['isError']
