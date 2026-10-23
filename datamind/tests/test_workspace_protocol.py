"""Exercise recovery and input auditing without a paid model or a live gateway."""
import asyncio
from types import SimpleNamespace as NS

import pytest

from benchmark.workspace_protocol import blind_pair_order, run_recoverable_turn, source_audit


class Thread:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.prompts = []
        self.interrupted = False
    async def turn(self, prompt, **kwargs):
        self.prompts.append(prompt)
        events = next(self.responses)
        async def stream():
            for event in events:
                if isinstance(event, float):
                    await asyncio.sleep(event)
                else:
                    yield event
        async def interrupt():
            self.interrupted = True
        return NS(stream=stream, interrupt=interrupt)


def completed(status, error=None):
    return NS(method="turn/completed", payload={"turn": {"status": status, "error": error}})


@pytest.mark.asyncio
async def test_transient_failure_recovers_in_same_thread_and_accounts_all_events():
    thread = Thread([[completed("failed", {"httpStatus": 429})], [completed("completed")]])
    events = []
    outcome = await run_recoverable_turn(thread, "original task", events.append,
                                          timeout_s=1, retry_delay_s=0)
    assert outcome["status"] == "completed" and len(outcome["attempts"]) == 2
    assert len(thread.prompts) == 2 and thread.prompts[0] == "original task"
    assert len(events) == 2 and outcome["seconds"] < 1


@pytest.mark.asyncio
async def test_one_deadline_covers_recovery_and_interrupts_hanging_turn():
    thread = Thread([[completed("failed", {"httpStatus": 503})], [1.0]])
    result = await run_recoverable_turn(thread, "task", lambda event: None,
                                        timeout_s=.02, retry_delay_s=0)
    assert result["status"] == "timeout" and thread.interrupted
    assert len(result["attempts"]) == 2 and result["seconds"] < .5


@pytest.mark.asyncio
@pytest.mark.parametrize("code,retries,category", [(400, 1, "agent_error"), (429, 3, "infrastructure_error")])
async def test_retry_limit_and_error_category(code, retries, category):
    thread = Thread([[completed("failed", {"httpStatus": code})]] * 3)
    result = await run_recoverable_turn(thread, "task", lambda event: None,
                                        timeout_s=1, retry_delay_s=0, max_retries=2)
    assert len(result["attempts"]) == retries and result["category"] == category
    assert result["quality_eligible"] == (category != "infrastructure_error")


def test_source_versions_and_incorrect_syntax_labels_are_flagged_without_regrading(tmp_path):
    workspace = tmp_path / "workspace"; workspace.mkdir()
    refs = tmp_path / "refs"; refs.mkdir()
    (workspace / "bug.py").write_text("print('valid code')\n")
    (refs / "bug.py").write_text("print('valid code')\n")
    meta = {"data_manifest": [{"filename": "bug.py", "stored_relpath": "bug.py"}],
            "rubrics": ["Identify the missing closing brace"]}
    audit = source_audit(meta, workspace, refs)
    assert audit["sources"][0]["exact_match"]
    assert audit["validity_flags"][0]["rubric_index"] == 0
    assert audit["official_scores_modified"] is False
    (workspace / "bug.py").write_text("print('different version')\n")
    assert not source_audit(meta, workspace, refs)["sources"][0]["exact_match"]
    assert blind_pair_order(["a", "b"], seed=3) == blind_pair_order(["a", "b"], seed=3)
