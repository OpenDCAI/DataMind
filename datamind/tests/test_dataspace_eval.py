"""Transport configuration and durable evidence required by isolated evaluation."""
import json,os
from pathlib import Path
from datamind.config import Settings
from datamind.host import CodexHost
from datamind.operations import OperationStore

def test_video_settings_reach_mcp_without_losing_nested_secrets(tmp_path):
    settings=Settings(_env_file=None,data={'base_dir':tmp_path},video={'vision':{'api_key':'fixture-secret','model':'fixture','protocol':'openai_chat_completions'},'asr_model_dir':tmp_path/'asr'})
    session=CodexHost(settings)._session('session','request','write')
    env=session.config['env']
    assert json.loads(env['DATAMIND__VIDEO__VISION'])['api_key']=='fixture-secret'
    assert env['DATAMIND__VIDEO__ASR_MODEL_DIR']==str(tmp_path/'asr')
    assert 'DATAMIND__VIDEO__VISION' in str(session.config['config_overrides'])

def test_opt_in_evidence_survives_host_consumption(tmp_path):
    log_root=Path(os.environ.get('DATAMIND_TEST_LOG_ROOT',str(tmp_path/'logs')))/tmp_path.name
    settings=Settings(_env_file=None,data={'base_dir':tmp_path/'data'},logging={'directory':log_root,'event_trace':True})
    store=OperationStore(settings)
    record={'name':'db_export_sql','result':{'complete':True},'auxiliary_usage':{'embedding_usage':[{'model':'fixture','total_tokens':12}]}}
    store.record_event('request',record)
    assert store.take_events('request')==[record]
    assert store.take_events('request')==[]
    saved=json.loads((settings.log_dir/'tool-events.jsonl').read_text())
    assert saved=={'request_id':'request',**record}
    normal=Settings(_env_file=None,data={'base_dir':tmp_path/'normal'},logging={'directory':log_root})
    OperationStore(normal).record_event('request',record)
    assert not (normal.log_dir/'tool-events.jsonl').exists()
