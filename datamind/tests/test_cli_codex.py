import pytest
from typer.testing import CliRunner
from datamind import cli
from datamind.config import DataSettings


def test_cli_info_uses_no_agent_or_model(monkeypatch,tmp_path):
    monkeypatch.chdir(tmp_path)
    def forbidden(*a,**kw):pytest.fail('info must not construct a Codex session')
    monkeypatch.setattr(cli,'CodexHost',forbidden)
    result=CliRunner().invoke(cli.app,['info'])
    assert result.exit_code==0,result.output
    assert 'codex' in result.output
    assert 'operation_begin' in result.output


def test_cli_ask_store_chat_use_the_same_host_with_access_modes(monkeypatch,tmp_path):
    calls=[]
    class Host:
        def __init__(self,cfg):self.settings=cfg
        async def stream(self,message,**kwargs):
            calls.append((message,kwargs['mode'],len(kwargs.get('history') or [])))
            yield {'type':'text','delta':'ok'}
            yield {'type':'done','answer':'ok','receipts':[]}
        async def aclose(self):calls.append('closed')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli,'CodexHost',Host)
    runner=CliRunner()
    for command in (['ask','question'],['store','save']):
        result=runner.invoke(cli.app,command)
        assert result.exit_code==0,result.output
    assert calls[:4]==[('question','read',0),'closed',('save','write',0),'closed']
    calls.clear()
    result=runner.invoke(cli.app,['chat'],input='/store save\n/new\nquestion\n/exit\n')
    assert result.exit_code==0,result.output
    assert calls[0]==('save','write',0)
    assert calls[1]==('question','read',0)
    assert calls[-1]=='closed'


def test_cli_displays_completion_when_sdk_sends_no_text_deltas(monkeypatch,tmp_path):
    class Host:
        def __init__(self,cfg):self.settings=cfg
        async def stream(self,*args,**kwargs):
            yield {'type':'done','answer':'Completed without deltas','receipts':[]}
        async def aclose(self):pass
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli,'CodexHost',Host)
    result=CliRunner().invoke(cli.app,['ask','question'])
    assert result.exit_code==0,result.output
    assert 'Completed without deltas' in result.output


def test_root_cli_launcher_uses_data_only_info_without_legacy_dependencies(tmp_path):
    import subprocess,sys
    from pathlib import Path
    root=Path(__file__).resolve().parents[2]
    result=subprocess.run([sys.executable,str(root/'main.py'),'info'],cwd=tmp_path,
        capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stderr
    assert 'operation_begin' in result.stdout
    assert 'codex' in result.stdout


def test_root_http_launcher_exports_the_same_app():
    import runpy
    from pathlib import Path
    from datamind.server import app
    root=Path(__file__).resolve().parents[2]
    module=runpy.run_path(str(root/'server.py'),run_name='datamind_root_server_test')
    assert module['app'] is app


def test_workspace_cli_policy_persists_and_gc_defaults_to_preview(monkeypatch,tmp_path):
    import json
    cfg=DataSettings(_env_file=None,data={'base_dir':tmp_path})
    monkeypatch.setattr(cli,'_settings',lambda:cfg)
    runner=CliRunner()
    result=runner.invoke(cli.app,['workspace-policy','recent','--keep-last','3'])
    assert result.exit_code==0,result.output
    status=runner.invoke(cli.app,['workspace-status','--usage'])
    assert status.exit_code==0,status.output
    value=json.loads(status.output)
    assert value['retention']=={'mode':'recent','keep_last':3}
    preview=runner.invoke(cli.app,['workspace-gc'])
    assert preview.exit_code==0,preview.output
    assert json.loads(preview.output)['dry_run'] is True
    assert runner.invoke(cli.app,['workspace-policy','invalid']).exit_code!=0
