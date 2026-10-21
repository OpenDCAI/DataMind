import json
from types import SimpleNamespace
import pytest
from datamind.experiment_faults import install,counts


@pytest.mark.asyncio
async def test_fault_state_survives_provider_recreation_and_counts_actual_attempts(tmp_path,monkeypatch):
    class Embed:
        async def embed_query(self,text):return [1.,0.]
    path=tmp_path/'state.db'
    plan={'state':str(path),'component':'embedding','seed':0,'task':'a','probability':1}
    monkeypatch.setenv('DATAMIND_TEST_READ_FAULT',json.dumps(plan))
    first=SimpleNamespace(embedding=Embed())
    install(first)
    with pytest.raises(TimeoutError):await first.embedding.embed_query('x')
    second=SimpleNamespace(embedding=Embed())
    install(second)
    with pytest.raises(TimeoutError):await second.embedding.embed_query('x')
    assert counts(path)=={'attempts':2,'injected':2}
    plan['probability']=0
    monkeypatch.setenv('DATAMIND_TEST_READ_FAULT',json.dumps(plan))
    third=SimpleNamespace(embedding=Embed());install(third)
    assert await third.embedding.embed_query('x')==[1.,0.]
    assert counts(path)=={'attempts':3,'injected':2}


def test_normal_runtime_does_not_enable_faults(tmp_path,monkeypatch):
    monkeypatch.delenv('DATAMIND_TEST_READ_FAULT',raising=False)
    install(SimpleNamespace())
    assert list(tmp_path.iterdir())==[]
