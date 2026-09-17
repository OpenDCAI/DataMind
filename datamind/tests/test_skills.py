"""Skills tests — loader + code skills (no network required)."""
from __future__ import annotations

from pathlib import Path

import pytest

from datamind.capabilities.skills import (
    SkillManifest,
    SkillsService,
    discover_skills,
    load_skill,
)
from datamind.capabilities.skills.code_skills import build_code_skills


# ------------------------------------------------------------- loader ---


def test_load_skill_with_frontmatter(tmp_path: Path):
    sk = tmp_path / "my-skill"
    sk.mkdir()
    (sk / "SKILL.md").write_text(
        "---\n"
        "name: my-skill\n"
        'description: "A test skill"\n'
        "keywords: [alpha, beta]\n"
        "---\n\n"
        "# Body\n\nSome content.\n",
        encoding="utf-8",
    )
    m = load_skill(sk / "SKILL.md")
    assert isinstance(m, SkillManifest)
    assert m.name == "my-skill"
    assert m.description == "A test skill"
    assert m.keywords == ("alpha", "beta")
    assert "Body" in m.body


def test_load_skill_without_frontmatter_falls_back(tmp_path: Path):
    sk = tmp_path / "no-fm"
    sk.mkdir()
    (sk / "SKILL.md").write_text("# First heading\n\nBody.\n", encoding="utf-8")
    m = load_skill(sk / "SKILL.md")
    assert m is not None
    assert m.name == "no-fm"
    # Description falls back to the first non-empty markdown line.
    assert "First heading" in m.description


def test_discover_skills_scans_subdirs(tmp_path: Path):
    for name in ("a", "b"):
        d = tmp_path / name
        d.mkdir()
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Skill {name}\n---\n\nBody {name}\n",
            encoding="utf-8",
        )
    # Also a directory without SKILL.md — must be skipped silently.
    (tmp_path / "c").mkdir()

    ms = discover_skills(tmp_path)
    assert [m.name for m in ms] == ["a", "b"]


class _SkillEmbedding:
    dimension = 1

    async def embed_texts(self, texts):
        return [[float(index)] for index, _ in enumerate(texts)]


class _SkillVectorStore:
    def __init__(self):
        self.items = {}

    async def reset(self):
        self.items.clear()

    async def add(self, ids, texts, embeddings, metadatas):
        self.items = {
            item_id: (text, metadata)
            for item_id, text, metadata in zip(ids, texts, metadatas)
        }


@pytest.mark.asyncio
async def test_skill_reload_clears_index_when_all_manifests_are_removed(tmp_path: Path):
    skill_dir = tmp_path / 'alpha'
    skill_dir.mkdir()
    manifest = skill_dir / 'SKILL.md'
    manifest.write_text(
        '---\nname: alpha\ndescription: Alpha\n---\n\nBody\n',
        encoding='utf-8',
    )
    store = _SkillVectorStore()
    service = SkillsService(
        skills_dir=tmp_path,
        profile_skills_dir=tmp_path / 'profile',
        embedding=_SkillEmbedding(),
        vector_store=store,
    )

    await service.load()
    assert sorted(store.items) == ['alpha']

    manifest.unlink()
    stats = await service.load()

    assert stats['manifests'] == 0
    assert store.items == {}


# ---------------------------------------------------------- code skills ---


@pytest.mark.asyncio
async def test_calculator_evaluates():
    spec = next(s for s in build_code_skills() if s.name == "calculator")
    out = await spec.handler(expression="2 * (3 + sqrt(16))")
    assert out["result"] == 14


@pytest.mark.asyncio
async def test_calculator_rejects_escape():
    spec = next(s for s in build_code_skills() if s.name == "calculator")
    with pytest.raises(ValueError):
        await spec.handler(expression="__import__('os').system('echo boom')")


@pytest.mark.asyncio
async def test_unit_convert_round_trip():
    spec = next(s for s in build_code_skills() if s.name == "unit_convert")
    a = await spec.handler(value=1.0, from_unit="km", to_unit="m")
    assert a["result"] == 1000
    b = await spec.handler(value=1000.0, from_unit="m", to_unit="km")
    assert b["result"] == 1.0


@pytest.mark.asyncio
async def test_unit_convert_unknown_pair_raises():
    spec = next(s for s in build_code_skills() if s.name == "unit_convert")
    with pytest.raises(ValueError):
        await spec.handler(value=1.0, from_unit="parsec", to_unit="m")


@pytest.mark.asyncio
async def test_analyze_text():
    spec = next(s for s in build_code_skills() if s.name == "analyze_text")
    out = await spec.handler(text="Hello world\n\nSecond paragraph.")
    assert out["paragraphs"] == 2
    assert out["words"] == 4


@pytest.mark.asyncio
async def test_get_current_time_fields():
    spec = next(s for s in build_code_skills() if s.name == "get_current_time")
    out = await spec.handler()
    assert "iso" in out and "weekday" in out
