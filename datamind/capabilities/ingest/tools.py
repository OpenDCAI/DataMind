"""ToolSpec wrappers for the ingest capability.

Write tools registered under the "ingest"
group. They share the same shape as the rest of DataMind's tools so the
agent loop / system prompt grouper picks them up automatically.

Tool naming convention:
    kb_add_file              — single-file ingest into the KB
    kb_add_path              — file or directory ingest into the KB
    db_import_csv            — CSV → SQL table
    graph_add_triples_from_text — LLM extracts triples from prose

We keep them in the `ingest` group (separate from kb/db/graph read groups)
so they're easy to disable wholesale via permission policies later.
"""
from __future__ import annotations

from datamind.core.tools import ToolSpec

from .service import IngestService


def build_ingest_tools(svc: IngestService) -> list[ToolSpec]:
    """Build StoreAgent ingest tools bound to a concrete IngestService."""

    async def _kb_add_text(
        text: str,
        source: str | None = None,
        persist: bool = True,
    ) -> dict:
        return await svc.kb_add_text(text=text, source=source, persist=persist)

    async def _kb_add_file(path: str, copy_to_profile: bool = True) -> dict:
        return await svc.kb_add_file(path=path, copy_to_profile=copy_to_profile)

    async def _kb_add_path(
        path: str, recursive: bool = True, copy_to_profile: bool = True
    ) -> dict:
        return await svc.kb_add_path(
            path=path, recursive=recursive, copy_to_profile=copy_to_profile
        )

    async def _db_import_csv(
        path: str, table: str, if_exists: str = "append", delimiter: str = ","
    ) -> dict:
        return await svc.db_import_csv(
            path=path, table=table, if_exists=if_exists, delimiter=delimiter
        )

    async def _db_import_records(
        table: str,
        records: list[dict],
        if_exists: str = "append",
    ) -> dict:
        return await svc.db_import_records(
            table=table,
            records=records,
            if_exists=if_exists,
        )

    async def _graph_add_triples_from_text(text: str, max_triples: int = 30) -> dict:
        return await svc.graph_add_triples_from_text(text=text, max_triples=max_triples)

    return [
        ToolSpec(
            name="kb_add_text",
            description=(
                "Store inline unstructured text in the knowledge base. The text "
                "is chunked, embedded, immediately searchable, and persisted "
                "under the active profile by default."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "source": {"type": "string", "description": "Optional source filename."},
                    "persist": {"type": "boolean", "default": True},
                },
                "required": ["text"],
            },
            handler=_kb_add_text,
            metadata={"group": "ingest", "surface": "kb", "access": "write"},
        ),
        ToolSpec(
            name="kb_add_file",
            description=(
                "Ingest a single text file (.md / .markdown / .txt) into the "
                "knowledge base — chunked, embedded, and immediately searchable. "
                "Use this when the user asks to add a specific file. The path "
                "must be inside an allowed root (the active profile's data dir "
                "or the current working directory). By default the file is also "
                "copied under the profile's `uploads/` subdirectory so a future "
                "kb_reindex picks it up too."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the file. Absolute or relative to cwd.",
                    },
                    "copy_to_profile": {
                        "type": "boolean",
                        "description": "If true, also copy the file under the profile's uploads/ dir.",
                        "default": True,
                    },
                },
                "required": ["path"],
            },
            handler=_kb_add_file,
            metadata={"group": "ingest", "surface": "kb", "access": "write"},
        ),
        ToolSpec(
            name="kb_add_path",
            description=(
                "Ingest one file OR every supported file under a directory. "
                "Use this when the user asks to add a folder or a path that "
                "may be either. Supported extensions: .md, .markdown, .txt. "
                "Returns aggregate counts and per-file detail. Set recursive "
                "to false to only scan the immediate directory."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to a file or directory.",
                    },
                    "recursive": {
                        "type": "boolean",
                        "description": "Recurse into subdirectories.",
                        "default": True,
                    },
                    "copy_to_profile": {
                        "type": "boolean",
                        "description": "If true, also copy each file under the profile's uploads/ dir.",
                        "default": True,
                    },
                },
                "required": ["path"],
            },
            handler=_kb_add_path,
            metadata={"group": "ingest", "surface": "kb", "access": "write"},
        ),
        ToolSpec(
            name="db_import_csv",
            description=(
                "Import a CSV file into a SQL table. The first row is treated "
                "as the header — columns are created as TEXT, since this is an "
                "ad-hoc loader (run a follow-up SQL ALTER if you need typed "
                "columns). `if_exists` controls behaviour when the target "
                "table already exists: 'append' (default) inserts into the "
                "existing table, 'replace' drops and recreates, 'fail' raises. "
                "Use this when the user asks to import a CSV / spreadsheet."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the CSV file.",
                    },
                    "table": {
                        "type": "string",
                        "description": "Target table name (letters, digits, underscore only).",
                    },
                    "if_exists": {
                        "type": "string",
                        "enum": ["append", "replace", "fail"],
                        "default": "append",
                    },
                    "delimiter": {
                        "type": "string",
                        "description": "Field delimiter; default comma. Use '\\t' for TSV.",
                        "default": ",",
                    },
                },
                "required": ["path", "table"],
            },
            handler=_db_import_csv,
            metadata={"group": "ingest", "surface": "db", "access": "write"},
        ),
        ToolSpec(
            name="db_import_records",
            description=(
                "Store an array of JSON objects in a SQL table. Use this for "
                "structured records supplied directly in the conversation."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "table": {"type": "string"},
                    "records": {
                        "type": "array",
                        "items": {"type": "object", "additionalProperties": True},
                    },
                    "if_exists": {
                        "type": "string",
                        "enum": ["append", "replace", "fail"],
                        "default": "append",
                    },
                },
                "required": ["table", "records"],
            },
            handler=_db_import_records,
            metadata={"group": "ingest", "surface": "db", "access": "write"},
        ),
        ToolSpec(
            name="graph_add_triples_from_text",
            description=(
                "Extract knowledge-graph triples (subject, relation, object) "
                "from free-form text via the LLM, then upsert them into the "
                "graph store. Use this when the user dictates relationships "
                "in natural language (e.g. \"Alice leads Search Team and "
                "reports to Bob\"). For pre-structured triple lists, use "
                "graph_upsert_triples instead."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Natural-language description of relationships.",
                    },
                    "max_triples": {
                        "type": "integer",
                        "description": "Cap on number of triples to extract.",
                        "default": 30,
                        "minimum": 1,
                        "maximum": 200,
                    },
                },
                "required": ["text"],
            },
            handler=_graph_add_triples_from_text,
            metadata={"group": "ingest", "surface": "graph", "access": "write"},
        ),
    ]


__all__ = ["build_ingest_tools"]
