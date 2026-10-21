"""Opt-in benchmark faults at the real provider boundary, shared across MCP calls.

Only a trusted experiment host supplies this environment plan. There is no model
or document-facing tool for enabling faults. Normal execution does not create state.
"""
import asyncio
import json
import os
import random
import sqlite3
import time
from pathlib import Path


def install(kb):
    raw=os.environ.get('DATAMIND_TEST_READ_FAULT')
    if not raw or kb is None:return
    plan=json.loads(raw)
    if plan['component'] not in {'embedding','vector'}:
        raise ValueError('Unsupported experiment component')
    probability=float(plan['probability'])
    if not 0<=probability<=1:raise ValueError('Invalid fault probability')
    path=Path(plan['state']);path.parent.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE IF NOT EXISTS fault_state(singleton INTEGER PRIMARY KEY, started REAL, attempts INTEGER, injected INTEGER)')
        db.execute('INSERT OR IGNORE INTO fault_state VALUES(1,?,0,0)',(time.monotonic(),))
    provider=kb.embedding if plan['component']=='embedding' else kb.vector_store
    method='embed_query' if plan['component']=='embedding' else 'query'
    original=getattr(provider,method)
    async def faulty(*args,**kwargs):
        with sqlite3.connect(path,timeout=30) as db:
            db.execute('BEGIN IMMEDIATE')
            started,attempts,injected=db.execute('SELECT started,attempts,injected FROM fault_state WHERE singleton=1').fetchone()
            fail=(time.monotonic()-started<float(plan.get('outage_ms',0))/1000 or
                  random.Random(f"{plan['seed']}:{plan['task']}:{attempts}").random()<probability)
            db.execute('UPDATE fault_state SET attempts=?,injected=? WHERE singleton=1',(attempts+1,injected+int(fail)))
        if fail:
            await asyncio.sleep(float(plan.get('delay_ms',0))/1000)
            raise TimeoutError('injected experiment provider outage')
        return await original(*args,**kwargs)
    setattr(provider,method,faulty)


def counts(path):
    if not Path(path).exists():return {'attempts':0,'injected':0}
    with sqlite3.connect(path) as db:
        attempts,injected=db.execute('SELECT attempts,injected FROM fault_state WHERE singleton=1').fetchone()
    return {'attempts':attempts,'injected':injected}
