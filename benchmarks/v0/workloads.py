"""Handwritten, inspectable workloads for DataMind-Bench v0.1."""
from __future__ import annotations

import json
from dataclasses import replace

from benchmarks.runner import WorkloadResult
from datamind.adapters import DOCUMENT_ARTIFACT_MEDIA_TYPE
from datamind.dataops import (
    ApplyMutation,
    BindingPredicate,
    ComparisonOperator,
    Compose,
    DataPlan,
    Filter,
    Fuse,
    InvokeSkill,
    Join,
    OutputRef,
    Project,
    ProposeMutation,
    Query,
    Recall,
    ResolveSkill,
    Search,
    Traverse,
    ValueBinding,
)
from datamind.kernel import (
    ArtifactChange,
    ArtifactManifest,
    ArtifactRef,
    AssertMemory,
    Budget,
    ChangeKind,
    ChangeSet,
    EffectLevel,
    MemoryKind,
    MemoryMutationDraft,
    SnapshotSet,
    SupersedeMemory,
    sha256_checksum,
)


def _ref(environment, key):
    return environment.state[key].descriptor.ref


def _plan(operation, *, plan_id, max_effect=EffectLevel.READ):
    return DataPlan(
        operations=(operation,),
        output=OutputRef(operation.op_id),
        plan_id=plan_id,
        max_effect=max_effect,
        budget=Budget(max_actions=1),
    )


async def _execute(environment, engine, task, run_id, plan, suffix="main"):
    context = environment.context(task, run_id, suffix=suffix)
    try:
        result = await engine.execute(plan, context=context)
        return WorkloadResult(
            plans=(plan,),
            result=result,
            trace_ids=(context.trace_id,),
            replay_trace_id=context.trace_id,
        )
    except Exception as error:
        return WorkloadResult(
            plans=(plan,),
            error=error,
            trace_ids=(context.trace_id,),
            replay_trace_id=context.trace_id,
        )


async def document_search(environment, engine, task, run_id):
    operation = Search(
        source=_ref(environment, "documents"),
        query="travel reimbursement policy",
        filters={"department": "sales"},
        limit=5,
        op_id="search-policy",
    )
    return await _execute(
        environment,
        engine,
        task,
        run_id,
        _plan(operation, plan_id="bench-document-search"),
    )


async def table_query(environment, engine, task, run_id):
    operation = Query(
        source=_ref(environment, "table"),
        statement=(
            "SELECT category, amount FROM expenses "
            "WHERE employee_id = 7 ORDER BY category"
        ),
        op_id="query-expenses",
    )
    return await _execute(
        environment,
        engine,
        task,
        run_id,
        _plan(operation, plan_id="bench-table-query"),
    )


async def graph_traverse(environment, engine, task, run_id):
    operation = Traverse(
        source=_ref(environment, "graph"),
        starts=("Acme",),
        relations=("located_in", "in_country"),
        max_hops=2,
        op_id="traverse-location",
    )
    return await _execute(
        environment,
        engine,
        task,
        run_id,
        _plan(operation, plan_id="bench-graph-traverse"),
    )


async def memory_recall(environment, engine, task, run_id):
    operation = Recall(
        source=_ref(environment, "memory"),
        query="communication preference",
        scopes=(environment.state["principal_scope"],),
        op_id="recall-preference",
    )
    return await _execute(
        environment,
        engine,
        task,
        run_id,
        _plan(operation, plan_id="bench-memory-recall"),
    )


async def skill_resolve(environment, engine, task, run_id):
    resolve = ResolveSkill(
        source=_ref(environment, "skills"),
        query="risk label",
        limit=2,
        op_id="resolve-risk-skill",
    )
    resolve_plan = _plan(
        resolve,
        plan_id="bench-skill-resolve",
    )
    resolved = await _execute(
        environment,
        engine,
        task,
        run_id,
        resolve_plan,
        "resolve",
    )
    if resolved.error is not None:
        return resolved
    spec = environment.state["risk_skill"]
    invoke = InvokeSkill(
        source=_ref(environment, "skills"),
        skill=spec.ref,
        governed_effect=spec.effect_level,
        arguments={"risk": "critical"},
        reversible=spec.reversible,
        requires_approval=spec.requires_approval,
        op_id="invoke-risk-skill",
    )
    invoke_plan = _plan(
        invoke,
        plan_id="bench-skill-invoke",
        max_effect=EffectLevel.PURE,
    )
    invoked = await _execute(
        environment,
        engine,
        task,
        run_id,
        invoke_plan,
        "invoke",
    )
    return WorkloadResult(
        plans=(resolve_plan, invoke_plan),
        result=invoked.result,
        error=invoked.error,
        trace_ids=resolved.trace_ids + invoked.trace_ids,
        replay_trace_id=invoked.replay_trace_id,
    )


async def document_table_join(environment, engine, task, run_id):
    search = Search(
        source=_ref(environment, "documents"),
        query="travel reimbursement policy",
        filters={"department": "sales"},
        op_id="search-policy",
    )
    query = Query(
        source=_ref(environment, "table"),
        statement=(
            "SELECT department, SUM(amount) AS amount "
            "FROM expenses WHERE employee_id = 7 GROUP BY department"
        ),
        op_id="query-expenses",
    )
    left = Project(
        inputs=(OutputRef(search.op_id),),
        fields=("document_id", "metadata.department"),
        op_id="project-policy",
    )
    right = Project(
        inputs=(OutputRef(query.op_id),),
        fields=("department", "amount"),
        op_id="project-expenses",
    )
    filtered = Filter(
        inputs=(OutputRef(right.op_id),),
        predicate=BindingPredicate(
            field="amount",
            operator=ComparisonOperator.GT,
            value=500,
        ),
        op_id="filter-material-expenses",
    )
    join = Join(
        inputs=(OutputRef(left.op_id), OutputRef(filtered.op_id)),
        left_on=("metadata.department",),
        right_on=("department",),
        left_alias="policy",
        right_alias="expense",
        op_id="join-policy-expenses",
    )
    plan = DataPlan(
        operations=(search, query, left, right, filtered, join),
        output=OutputRef(join.op_id),
        plan_id="bench-document-table-join",
        budget=Budget(max_actions=6),
    )
    return await _execute(environment, engine, task, run_id, plan)


async def table_graph_binding(environment, engine, task, run_id):
    query = Query(
        source=_ref(environment, "table"),
        statement=(
            "SELECT entity FROM projects WHERE project_id = 'p1'"
        ),
        op_id="query-project-entity",
    )
    project = Project(
        inputs=(OutputRef(query.op_id),),
        fields=("entity",),
        op_id="project-entity",
    )
    traverse = Traverse(
        source=_ref(environment, "graph"),
        start_binding=ValueBinding(
            ref=OutputRef(project.op_id),
            field="entity",
        ),
        relations=("located_in",),
        max_hops=1,
        op_id="traverse-project-location",
    )
    plan = DataPlan(
        operations=(query, project, traverse),
        output=OutputRef(traverse.op_id),
        plan_id="bench-table-graph-binding",
        budget=Budget(max_actions=3),
    )
    return await _execute(environment, engine, task, run_id, plan)


async def memory_assert_read(environment, engine, task, run_id):
    memory_ref = _ref(environment, "memory")
    scope = environment.state["principal_scope"]
    draft = MemoryMutationDraft(
        scope=scope,
        changes=(
            AssertMemory(
                MemoryKind.PREFERENCE,
                "Travel seat preference is window.",
            ),
        ),
        idempotency_key="bench-window-seat",
    )
    propose = ProposeMutation(
        source=memory_ref,
        draft=draft,
        op_id="propose-window-seat",
    )
    propose_plan = _plan(
        propose,
        plan_id="bench-propose-window-seat",
    )
    propose_run = await _execute(
        environment,
        engine,
        task,
        run_id,
        propose_plan,
        "propose",
    )
    if propose_run.error is not None:
        return propose_run
    apply = ApplyMutation(
        source=memory_ref,
        proposal=propose_run.result.value,
        op_id="apply-window-seat",
    )
    apply_plan = _plan(
        apply,
        plan_id="bench-apply-window-seat",
        max_effect=EffectLevel.INTERNAL_WRITE,
    )
    apply_run = await _execute(
        environment,
        engine,
        task,
        run_id,
        apply_plan,
        "apply",
    )
    if apply_run.error is not None:
        return WorkloadResult(
            plans=(propose_plan, apply_plan),
            error=apply_run.error,
            trace_ids=propose_run.trace_ids + apply_run.trace_ids,
            replay_trace_id=apply_run.replay_trace_id,
        )
    recall = Recall(
        source=memory_ref,
        query="travel seat preference window",
        scopes=(scope,),
        op_id="recall-window-seat",
    )
    recall_plan = _plan(
        recall,
        plan_id="bench-recall-window-seat",
    )
    recall_run = await _execute(
        environment,
        engine,
        task,
        run_id,
        recall_plan,
        "recall",
    )
    return WorkloadResult(
        plans=(propose_plan, apply_plan, recall_plan),
        result=recall_run.result,
        error=recall_run.error,
        trace_ids=(
            propose_run.trace_ids
            + apply_run.trace_ids
            + recall_run.trace_ids
        ),
        replay_trace_id=recall_run.replay_trace_id,
    )


async def memory_supersede_history(environment, engine, task, run_id):
    memory = environment.state["memory"]
    memory_ref = memory.descriptor.ref
    scope = environment.state["principal_scope"]
    initial = await memory.current_snapshot()
    draft = MemoryMutationDraft(
        scope=scope,
        changes=(
            SupersedeMemory(
                target_id="communication-pref",
                content="Communication preference is phone.",
            ),
        ),
        idempotency_key="bench-correct-communication",
    )
    propose = ProposeMutation(
        source=memory_ref,
        draft=draft,
        op_id="propose-communication",
    )
    propose_plan = _plan(
        propose,
        plan_id="bench-propose-communication",
    )
    propose_run = await _execute(
        environment, engine, task, run_id, propose_plan, "propose"
    )
    if propose_run.error is not None:
        return propose_run
    apply = ApplyMutation(
        source=memory_ref,
        proposal=propose_run.result.value,
        op_id="apply-communication",
    )
    apply_plan = _plan(
        apply,
        plan_id="bench-apply-communication",
        max_effect=EffectLevel.INTERNAL_WRITE,
    )
    apply_run = await _execute(
        environment, engine, task, run_id, apply_plan, "apply"
    )
    if apply_run.error is not None:
        return WorkloadResult(
            plans=(propose_plan, apply_plan),
            error=apply_run.error,
            trace_ids=propose_run.trace_ids + apply_run.trace_ids,
            replay_trace_id=apply_run.replay_trace_id,
        )
    current = Recall(
        source=memory_ref,
        query="communication preference",
        scopes=(scope,),
        op_id="recall-current-communication",
    )
    current_plan = _plan(
        current,
        plan_id="bench-current-communication",
    )
    current_run = await _execute(
        environment, engine, task, run_id, current_plan, "current"
    )
    historical = Recall(
        source=memory_ref,
        query="communication preference",
        scopes=(scope,),
        known_at=initial.observed_at,
        op_id="recall-historical-communication",
    )
    historical_plan = _plan(
        historical,
        plan_id="bench-historical-communication",
    )
    historical_context = replace(
        environment.context(task, run_id, suffix="historical"),
        snapshots=SnapshotSet((initial,)),
    )
    try:
        historical_result = await engine.execute(
            historical_plan,
            context=historical_context,
        )
        historical_error = None
    except Exception as error:
        historical_result = None
        historical_error = error
    environment.state["historical_result"] = historical_result
    return WorkloadResult(
        plans=(
            propose_plan,
            apply_plan,
            current_plan,
            historical_plan,
        ),
        result=current_run.result,
        error=current_run.error or historical_error,
        trace_ids=(
            propose_run.trace_ids
            + apply_run.trace_ids
            + current_run.trace_ids
            + (historical_context.trace_id,)
        ),
        replay_trace_id=current_run.replay_trace_id,
    )


async def governed_skill_denied(environment, engine, task, run_id):
    spec = environment.state["write_skill"]
    invoke = InvokeSkill(
        source=_ref(environment, "skills"),
        skill=spec.ref,
        governed_effect=spec.effect_level,
        arguments={"project_id": "p1"},
        reversible=spec.reversible,
        requires_approval=spec.requires_approval,
        approval_key="record-approval",
        idempotency_key="bench-record-p1",
        op_id="invoke-record-approval",
    )
    return await _execute(
        environment,
        engine,
        task,
        run_id,
        _plan(
            invoke,
            plan_id="bench-governed-skill-denied",
            max_effect=EffectLevel.INTERNAL_WRITE,
        ),
    )


async def terminal_source_failure(environment, engine, task, run_id):
    search = Search(
        source=environment.state["failing"].descriptor.ref,
        query="Acme backup policy",
        op_id="search-failing-policy",
    )
    return await _execute(
        environment,
        engine,
        task,
        run_id,
        _plan(search, plan_id="bench-terminal-source-failure"),
    )


async def replay_composition(environment, engine, task, run_id):
    first = Search(
        source=_ref(environment, "documents"),
        query="travel reimbursement policy",
        filters={"department": "sales"},
        op_id="search-travel",
    )
    second = Search(
        source=_ref(environment, "documents"),
        query="sales meals reimbursable dollars",
        filters={"department": "sales"},
        op_id="search-meals",
    )
    fuse = Fuse(
        inputs=(OutputRef(first.op_id), OutputRef(second.op_id)),
        limit=5,
        op_id="fuse-policy",
    )
    plan = DataPlan(
        operations=(first, second, fuse),
        output=OutputRef(fuse.op_id),
        plan_id="bench-replay-fuse",
        budget=Budget(max_actions=3),
    )
    return await _execute(environment, engine, task, run_id, plan)


async def lifecycle_snapshot_transition(
    environment,
    engine,
    task,
    run_id,
):
    source = environment.state["documents"]
    artifacts = environment.state["artifacts"]
    previous = await source.current_snapshot()
    content = json.dumps(
        {
            "document_id": "travel-policy",
            "content": (
                "Travel reimbursement policy: the current sales meal "
                "limit is 50 dollars."
            ),
            "metadata": {"department": "sales"},
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    artifact_ref = ArtifactRef("travel-policy", "artifact-v2")
    manifest = ArtifactManifest(
        ref=artifact_ref,
        source=source.descriptor.ref,
        checksum=sha256_checksum(content),
        locator="memory://policy-kb/travel-policy/artifact-v2",
        media_type=DOCUMENT_ARTIFACT_MEDIA_TYPE,
    )
    artifacts.put(manifest, content)
    change_set = ChangeSet(
        source=source.descriptor.ref,
        base_version=previous.version,
        changes=(
            ArtifactChange(
                ChangeKind.UPDATE,
                artifact_ref,
                manifest,
            ),
        ),
        idempotency_key="bench-policy-sync-v2",
        change_set_id="bench-change-policy-v2",
    )
    first_receipt = await engine.sync(change_set)
    second_receipt = await engine.sync(change_set)

    latest = Search(
        source=source.descriptor.ref,
        query="current sales meal limit",
        filters={"department": "sales"},
        op_id="search-current-policy",
    )
    latest_plan = _plan(
        latest,
        plan_id="bench-current-policy-snapshot",
    )
    latest_run = await _execute(
        environment,
        engine,
        task,
        run_id,
        latest_plan,
        "latest",
    )
    if latest_run.error is not None:
        return latest_run

    historical = Search(
        source=source.descriptor.ref,
        query="reimbursable 100 dollars",
        filters={"department": "sales"},
        op_id="search-historical-policy",
    )
    historical_plan = _plan(
        historical,
        plan_id="bench-historical-policy-snapshot",
    )
    historical_context = replace(
        environment.context(task, run_id, suffix="historical"),
        snapshots=SnapshotSet((previous,)),
    )
    try:
        historical_result = await engine.execute(
            historical_plan,
            context=historical_context,
        )
        historical_error = None
    except Exception as error:
        historical_result = None
        historical_error = error
    environment.state.update(
        {
            "lifecycle_first_receipt": first_receipt,
            "lifecycle_second_receipt": second_receipt,
            "lifecycle_historical_result": historical_result,
        }
    )
    return WorkloadResult(
        plans=(latest_plan, historical_plan),
        result=latest_run.result,
        error=historical_error,
        trace_ids=latest_run.trace_ids + (historical_context.trace_id,),
        replay_trace_id=latest_run.replay_trace_id,
    )


WORKLOADS = {
    "document-search": document_search,
    "table-query": table_query,
    "graph-traverse": graph_traverse,
    "memory-recall": memory_recall,
    "skill-resolve": skill_resolve,
    "document-table-join": document_table_join,
    "table-graph-binding": table_graph_binding,
    "memory-assert-read": memory_assert_read,
    "memory-supersede-history": memory_supersede_history,
    "governed-skill-denied": governed_skill_denied,
    "terminal-source-failure": terminal_source_failure,
    "replay-composition": replay_composition,
    "lifecycle-snapshot-transition": lifecycle_snapshot_transition,
}
