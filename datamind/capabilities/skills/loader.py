"""Skill loader — parses SDK-style SKILL.md manifests.

Each skill lives at `.claude/skills/<name>/SKILL.md` and starts with a YAML
frontmatter block:

    ---
    name: code-review
    description: Short one-line summary used by the agent to decide WHEN to read the skill.
    keywords: [optional, list, of, search, terms]
    ---

    # The skill body (Markdown)
    ...

We don't require external YAML libs — the frontmatter subset we care about
(scalar values + simple string lists) is tiny and hand-parseable.
"""
from __future__ import annotations

import re
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from datamind.core.logging import get_logger

_log = get_logger("skills.loader")

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)


@dataclass(frozen=True)
class SkillManifest:
    name: str
    description: str
    body: str
    path: Path
    keywords: tuple[str, ...] = field(default_factory=tuple)
    version: str = "1.0.0"
    content_hash: str = ""

    @property
    def full_text(self) -> str:
        """description + body — what goes into the semantic index."""
        return f"{self.description}\n\n{self.body}"


def _parse_frontmatter(text: str) -> tuple[dict[str, object], str]:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    header, body = m.group(1), m.group(2)
    data: dict[str, object] = {}
    in_metadata = False
    for raw in header.splitlines():
        line = raw.rstrip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if raw[:1].isspace():
            # Codex-compatible metadata, with legacy top-level fields retained.
            # Ignore unrelated nested keys rather than overwriting name/description.
            if not in_metadata or key not in {"version", "keywords"}:
                continue
        else:
            in_metadata = key == "metadata" and not val
        if not val:
            data[key] = ""
            continue
        # Inline list: keywords: [a, b, c]
        if val.startswith("[") and val.endswith("]"):
            items = [
                s.strip().strip("'").strip('"')
                for s in val[1:-1].split(",")
                if s.strip()
            ]
            data[key] = items
        else:
            # Strip surrounding quotes if any
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
                val = val[1:-1]
            data[key] = val
    return data, body


def load_skill(path: Path) -> SkillManifest | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        _log.warning("skill_read_failed", extra={"path": str(path), "err": str(exc)})
        return None
    meta, body = _parse_frontmatter(text)
    name = str(meta.get("name") or path.parent.name).strip()
    desc = str(meta.get("description") or "").strip()
    if not desc:
        # Fall back to the first non-empty Markdown line so the skill is at
        # least searchable.
        for line in body.splitlines():
            s = line.strip().lstrip("#").strip()
            if s:
                desc = s
                break
    keywords_raw = meta.get("keywords")
    if isinstance(keywords_raw, list):
        keywords = tuple(str(k) for k in keywords_raw)
    elif isinstance(keywords_raw, str) and keywords_raw:
        keywords = tuple(s.strip() for s in keywords_raw.split(",") if s.strip())
    else:
        keywords = ()
    return SkillManifest(
        name=name,
        description=desc,
        body=body.strip(),
        path=path,
        keywords=keywords,
        version=str(meta.get("version") or "1.0.0"),
        content_hash="sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def discover_skills(skills_dir: Path) -> list[SkillManifest]:
    """Scan `<skills_dir>/<name>/SKILL.md` and return parsed manifests."""
    out: list[SkillManifest] = []
    if not skills_dir.is_dir():
        return out
    for child in sorted(skills_dir.iterdir()):
        if not child.is_dir():
            continue
        manifest_path = child / "SKILL.md"
        if not manifest_path.is_file():
            continue
        m = load_skill(manifest_path)
        if m is not None:
            out.append(m)
    _log.info("skills_discovered", extra={"count": len(out), "dir": str(skills_dir)})
    return out
