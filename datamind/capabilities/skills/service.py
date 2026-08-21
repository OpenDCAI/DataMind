"""SkillsService: unifies knowledge skills (SKILL.md) + code skills.

Knowledge skills are indexed into a dedicated Chroma collection — reusing
the KB infrastructure — so the agent can semantically search across all
manifests to decide which one to apply.

Code skills are just ToolSpecs; they don't need an index.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from datamind.capabilities.embedding import build_embedding
from datamind.config import Settings
from datamind.core.errors import CapabilityError
from datamind.core.logging import get_logger
from datamind.core.protocols import EmbeddingProvider, VectorStore
from datamind.core.registry import vector_store_registry
from datamind.core.tools import ToolSpec

# Ensure the chroma vector store provider is registered even when the KB
# capability isn't imported separately.
from datamind.capabilities.kb.providers import chroma_store  # noqa: F401

from .code_skills import build_code_skills
from .loader import SkillManifest, discover_skills

_log = get_logger("skills.service")


class SkillsService:
    def __init__(
        self,
        *,
        skills_dir: Path,
        profile_skills_dir: Path | None = None,
        embedding: EmbeddingProvider | None,
        vector_store: VectorStore | None,
    ) -> None:
        self._skills_dir = Path(skills_dir)
        self._profile_skills_dir = Path(profile_skills_dir or skills_dir)
        self._embedding = embedding
        self._store = vector_store
        self._by_name: dict[str, SkillManifest] = {}
        # code skills aren't indexed; they just get exposed as tools.
        self._code_tools: list[ToolSpec] = build_code_skills()

    # ------------------------------------------------------------ lifecycle

    async def load(self) -> dict[str, int]:
        # Built-in/user skills load first; profile-scoped skills override a
        # same-named base skill without mutating the shared catalogue.
        manifests = discover_skills(self._skills_dir)
        if self._profile_skills_dir.resolve() != self._skills_dir.resolve():
            manifests.extend(discover_skills(self._profile_skills_dir))
        self._by_name = {m.name: m for m in manifests}
        manifests = list(self._by_name.values())
        indexed = 0
        if manifests and self._embedding and self._store:
            ids = [m.name for m in manifests]
            texts = [m.full_text for m in manifests]
            metas = [
                {
                    "source": str(m.path),
                    "description": m.description,
                    "keywords": ",".join(m.keywords),
                }
                for m in manifests
            ]
            vectors = await self._embedding.embed_texts(texts)
            await self._store.reset()
            await self._store.add(ids=ids, texts=texts, embeddings=vectors, metadatas=metas)
            indexed = len(ids)
        _log.info(
            "skills_loaded",
            extra={
                "manifests": len(manifests),
                "indexed": indexed,
                "code_tools": len(self._code_tools),
            },
        )
        return {
            "manifests": len(manifests),
            "indexed": indexed,
            "code_tools": len(self._code_tools),
        }

    # --------------------------------------------------------------- public

    def list_skills(self) -> list[dict[str, Any]]:
        return [
            {
                "name": m.name,
                "description": m.description,
                "keywords": list(m.keywords),
                "path": str(m.path),
                "body_chars": len(m.body),
            }
            for m in self._by_name.values()
        ]

    def get(self, name: str) -> dict[str, Any]:
        m = self._by_name.get(name)
        if m is None:
            return {"found": False, "name": name, "candidates": list(self._by_name)}
        return {
            "found": True,
            "name": m.name,
            "description": m.description,
            "keywords": list(m.keywords),
            "body": m.body,
            "path": str(m.path),
        }

    async def search(self, query: str, *, top_k: int = 3) -> list[dict[str, Any]]:
        if not self._embedding or not self._store:
            return []
        vec = await self._embedding.embed_query(query)
        hits = await self._store.query(vec, top_k=top_k)
        return [
            {
                "name": h.id,
                "score": h.score,
                "description": h.metadata.get("description", ""),
                "keywords": (h.metadata.get("keywords") or "").split(","),
                "snippet": h.text[:400],
            }
            for h in hits
        ]

    async def upsert(
        self,
        *,
        name: str,
        description: str,
        body: str,
        keywords: list[str] | None = None,
        overwrite: bool = True,
    ) -> dict[str, Any]:
        """Write a profile-scoped SKILL.md and refresh the live index."""
        normalized = name.strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", normalized):
            raise CapabilityError(
                "skills",
                "skill name must contain lowercase letters, digits, '-' or '_'",
            )
        if not description.strip():
            raise CapabilityError("skills", "skill description is required")
        if not body.strip():
            raise CapabilityError("skills", "skill body is required")

        target_dir = self._profile_skills_dir / normalized
        target = target_dir / "SKILL.md"
        existed = target.exists()
        if existed and not overwrite:
            raise CapabilityError("skills", f"skill '{normalized}' already exists")
        target_dir.mkdir(parents=True, exist_ok=True)

        safe_keywords = [
            str(k).strip() for k in (keywords or []) if str(k).strip()
        ]
        keyword_json = json.dumps(safe_keywords, ensure_ascii=False)
        description_json = json.dumps(description.strip(), ensure_ascii=False)
        text = (
            "---\n"
            f"name: {normalized}\n"
            f"description: {description_json}\n"
            f"keywords: {keyword_json}\n"
            "---\n\n"
            f"{body.strip()}\n"
        )
        target.write_text(text, encoding="utf-8")
        stats = await self.load()
        return {
            "name": normalized,
            "path": str(target),
            "created": not existed,
            "index": stats,
        }

    @property
    def code_tools(self) -> list[ToolSpec]:
        return list(self._code_tools)


def build_skills_service(
    settings: Settings,
    *,
    embedding: EmbeddingProvider | None = None,
) -> SkillsService:
    """Assemble a SkillsService from the root Settings.

    If no embedding API key is provided we still build the service — we
    just skip the semantic index and fall back to exact-name lookup.
    """
    skills_dir = settings.data.skills_dir
    store = None
    # Skills indexing only needs creds if we actually want semantic search.
    has_creds = bool(settings.embedding.api_key or settings.llm.api_key)
    if has_creds:
        try:
            embedding = embedding or build_embedding(settings.embedding, fallback_llm=settings.llm)
            storage = settings.data.storage_dir / "chroma_skills"
            store = vector_store_registry.create(
                "chroma",
                persist_dir=str(storage),
                collection_name="skills",
                dimension=embedding.dimension,
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("skills_index_disabled", extra={"err": repr(exc)})
            embedding = None
            store = None
    return SkillsService(
        skills_dir=skills_dir,
        profile_skills_dir=settings.data.profile_skills_dir,
        embedding=embedding,
        vector_store=store,
    )


__all__ = ["SkillsService", "build_skills_service"]
