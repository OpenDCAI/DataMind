"""Parallel edges are distinct evidence paths, including after persistence."""
import pytest

from datamind.capabilities.graph.providers.networkx_store import NetworkXGraphStore
from datamind.core.protocols import GraphTriple


def triple(src, rel, dst, source=None, confidence=1.0, **props):
    return GraphTriple(subject=src, relation=rel, object=dst, source=source,
                       confidence=confidence, properties=props)


@pytest.mark.asyncio
async def test_parallel_relations_extend_into_distinct_multihop_paths(tmp_path):
    store = NetworkXGraphStore(persist_path=tmp_path / 'g.json')
    await store.upsert_triples([
        triple('A', 'authored', 'B', confidence=0.8),
        triple('A', 'reviewed', 'B', confidence=0.6),
        triple('B', 'published_in', 'C'),
    ])
    paths = await store.traverse('A', max_hops=2)
    assert {tuple(e.relation for e in p.edges) for p in paths} == {
        ('authored',), ('reviewed',),
        ('authored', 'published_in'), ('reviewed', 'published_in'),
    }
    scores = {tuple(e.relation for e in p.edges): p.score for p in paths}
    assert scores[('authored', 'published_in')] == pytest.approx(0.9)
    assert [p.score for p in paths] == sorted(scores.values(), reverse=True)


@pytest.mark.asyncio
async def test_same_relation_different_origins_survive_reload_and_upsert(tmp_path):
    path = tmp_path / 'g.json'
    store = NetworkXGraphStore(persist_path=path)
    triples = [triple('A', 'cites', 'B', source='doc1'),
               triple('A', 'cites', 'B', source='doc2'),
               triple('A', 'cites', 'B', source='doc1', _profile_managed=True)]
    await store.upsert_triples(triples)
    await store.upsert_triples(triples)  # Exact edge identities still overwrite.
    before = await store.traverse('A')
    assert len(before) == 3
    assert {(p.edges[0].properties['source'],
             p.edges[0].properties.get('_profile_managed', False)) for p in before} == {
        ('doc1', False), ('doc2', False), ('doc1', True),
    }
    await store.persist()
    after = await NetworkXGraphStore(persist_path=path).traverse('A')
    assert [p.model_dump() for p in before] == [p.model_dump() for p in after]


@pytest.mark.asyncio
@pytest.mark.parametrize('cap', [1, 3, 100])
async def test_insertion_order_does_not_change_results_even_when_capped(tmp_path, cap):
    triples = [triple('A', 'reviewed', 'B', source='z'),
               triple('A', 'authored', 'B', source='b'),
               triple('A', 'authored', 'B', source='a'),
               triple('B', 'next', 'C')]
    outputs = []
    for i, ordered in enumerate([triples, list(reversed(triples))]):
        store = NetworkXGraphStore(persist_path=tmp_path / f'{i}.json')
        await store.upsert_triples(ordered)
        paths = await store.traverse('A', max_results=cap)
        assert len(paths) <= cap
        outputs.append([p.model_dump() for p in paths])
    assert outputs[0] == outputs[1]


@pytest.mark.asyncio
async def test_filters_hops_and_cycles_remain_bounded(tmp_path):
    store = NetworkXGraphStore(persist_path=tmp_path / 'g.json')
    await store.upsert_triples([
        triple('A', 'skip', 'B'), triple('A', 'keep', 'B'),
        triple('B', 'keep', 'C'), triple('C', 'keep', 'A'),
        triple('A', 'keep', 'A'),
    ])
    paths = await store.traverse('A', max_hops=10, relation_filter=['keep'])
    assert [p.nodes for p in paths] == [['A', 'B'], ['A', 'B', 'C']]
    assert all(e.relation == 'keep' for p in paths for e in p.edges)
    assert len(await store.traverse('A', max_hops=1, relation_filter=['keep'])) == 1
    assert await store.traverse('A', relation_filter=['missing']) == []
    assert await store.traverse('missing') == []


@pytest.mark.asyncio
@pytest.mark.parametrize('cap', [0, -1, 1, 7])
async def test_dense_parallel_graph_obeys_result_budget(tmp_path, cap):
    store = NetworkXGraphStore(persist_path=tmp_path / 'g.json')
    await store.upsert_triples([
        triple(str(i), f'relation{j}', str(i+1))
        for i in range(8) for j in range(8)
    ])
    paths = await store.traverse('0', max_hops=8, max_results=cap)
    assert len(paths) == max(0, cap)
    assert await store.traverse('0', max_hops=0) == []
