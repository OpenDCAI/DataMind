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

from datamind.core.tools import ToolSpec, tool_provider_registry

from .service import IngestService


def build_ingest_tools(svc: IngestService) -> list[ToolSpec]:
    """Build StoreAgent ingest tools bound to a concrete IngestService."""

    async def _kb_add_text(
        text: str,
        source: str | None = None,
        persist: bool = True,
    ) -> dict:
        return await svc.kb_add_text(text=text, source=source, persist=persist)

    async def _workspace_inspect(
        path: str,
        recursive: bool = True,
        include_hash: bool = True,
        max_files: int = 2000,
    ) -> dict:
        return await svc.workspace_inspect(
            path=path,
            recursive=recursive,
            include_hash=include_hash,
            max_files=max_files,
        )

    async def _graph_build_lineage(
        path: str, recursive: bool = True, max_files: int = 2000,
        dependencies: list[dict[str, str]] | None = None,
    ) -> dict:
        return await svc.graph_build_lineage(
            path=path, recursive=recursive, max_files=max_files, dependencies=dependencies,
        )

    async def _raw_file_read(path: str, offset: int = 0, max_chars: int = 20000) -> dict:
        return await svc.raw_file_read(path=path, offset=offset, max_chars=max_chars)

    async def _build_status(build_id: str) -> dict:
        return await svc.build_status(build_id=build_id)

    async def _build_start(path: str) -> dict:
        return await svc.build_start(path=path)

    async def _build_freeze(build_id: str) -> dict:
        return await svc.build_freeze(build_id=build_id)

    async def _build_verify(build_id: str) -> dict:
        return await svc.build_verify(build_id=build_id)

    async def _build_export(build_id: str, output_path: str) -> dict:
        return await svc.build_export(build_id=build_id, output_path=output_path)

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

    async def _db_import_path(
        path: str,
        table_prefix: str | None = None,
        if_exists: str = "append",
        delimiter: str = ",",
    ) -> dict:
        return await svc.db_import_path(
            path=path, table_prefix=table_prefix, if_exists=if_exists, delimiter=delimiter
        )

    async def _surface_ingest_path(
        path: str, surfaces: list[str] | None = None, recursive: bool = True
    ) -> dict:
        return await svc.surface_ingest_path(path=path, surfaces=surfaces, recursive=recursive)

    async def _graph_add_triples_from_text(text: str, max_triples: int = 30) -> dict:
        return await svc.graph_add_triples_from_text(text=text, max_triples=max_triples)

    async def _graph_add_path(
        path: str,
        recursive: bool = True,
        max_triples_per_file: int = 30,
    ) -> dict:
        return await svc.graph_add_path(
            path=path,
            recursive=recursive,
            max_triples_per_file=max_triples_per_file,
        )

    return [
        ToolSpec(
            name="raw_file_read",
            description="Read paginated source text or extracted document text, with its source hash, without ingestion.",
            input_schema={"type": "object", "properties": {
                "path": {"type": "string"}, "offset": {"type": "integer", "minimum": 0, "default": 0},
                "max_chars": {"type": "integer", "minimum": 1, "maximum": 100000, "default": 20000}},
                "required": ["path"]},
            handler=_raw_file_read,
            metadata={"group": "workspace", "access": "utility"},
        ),
        ToolSpec(
            name="build_status",
            description="Inspect a build's lifecycle state and verify frozen artifacts if applicable.",
            input_schema={"type": "object", "properties": {"build_id": {"type": "string"}}, "required": ["build_id"]},
            handler=_build_status,
            metadata={"group": "workspace", "access": "utility"},
        ),
        ToolSpec(
            name="workspace_inspect",
            description=(
                "Read-only inventory of a file or workspace directory. Returns file types, "
                "sizes, SHA-256 hashes, and conservative candidate surfaces (RAG, table, "
                "or graph) without ingesting anything. Use this before planning a build."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Workspace file or directory."},
                    "recursive": {"type": "boolean", "default": True},
                    "include_hash": {"type": "boolean", "default": True},
                    "max_files": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 2000},
                },
                "required": ["path"],
            },
            handler=_workspace_inspect,
            metadata={"group": "workspace", "access": "utility"},
        ),
        ToolSpec(
            name="build_start",
            description="Start a workspace build run and capture the source inventory before ingestion.",
            input_schema={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            handler=_build_start,
            metadata={"group": "workspace", "access": "write"},
        ),
        ToolSpec(
            name="build_freeze",
            description="Freeze current DataMind artifacts for a build and record content hashes.",
            input_schema={"type": "object", "properties": {"build_id": {"type": "string"}}, "required": ["build_id"]},
            handler=_build_freeze,
            metadata={"group": "workspace", "access": "write"},
        ),
        ToolSpec(
            name="build_verify",
            description="Verify that frozen DataMind artifacts still match their recorded hashes.",
            input_schema={"type": "object", "properties": {"build_id": {"type": "string"}}, "required": ["build_id"]},
            handler=_build_verify,
            metadata={"group": "workspace", "access": "utility"},
        ),
        ToolSpec(
            name="build_export",
            description="Export a verified frozen build to a directory without copying raw source files.",
            input_schema={"type": "object", "properties": {
                "build_id": {"type": "string"}, "output_path": {"type": "string"}},
                "required": ["build_id", "output_path"]},
            handler=_build_export,
            metadata={"group": "workspace", "access": "write"},
        ),
        ToolSpec(
            name="surface_ingest_path",
            description=(
                "Inspect and route a workspace into selected DataMind surfaces: documents to KB, "
                "CSV/TSV/Excel to DB, and deterministic file lineage to Graph."
            ),
            input_schema={"type": "object", "properties": {
                "path": {"type": "string"},
                "surfaces": {"type": "array", "items": {"type": "string", "enum": ["kb", "db", "graph"]}},
                "recursive": {"type": "boolean", "default": True}}, "required": ["path"]},
            handler=_surface_ingest_path,
            metadata={"group": "workspace", "access": "write"},
        ),
        ToolSpec(
            name="graph_build_lineage",
            description=(
                "Build or replace a deterministic file-lineage graph for a workspace. "
                "It records containment, textual file mentions, CSV/TSV schema overlap, "
                "version-name matches, duplicate content hashes, and source provenance."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Workspace file or directory."},
                    "dependencies": {"type": "array", "items": {
                        "type": "object", "required": ["source", "target"], "properties": {
                            "source": {"type": "string", "description": "Dependent workspace-relative file."},
                            "target": {"type": "string", "description": "File it depends on."},
                            "relation": {"type": "string", "enum": ["depends_on"]}}}},
                    "recursive": {"type": "boolean", "default": True},
                    "max_files": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 2000},
                },
                "required": ["path"],
            },
            handler=_graph_build_lineage,
            metadata={"group": "ingest", "surface": "graph", "access": "write"},
        ),
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
            name="db_import_path",
            description=(
                "Import a CSV/TSV file or every sheet in an XLSX workbook into the SQL surface. "
                "Each workbook sheet becomes a separate table with source metadata."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "table_prefix": {"type": ["string", "null"]},
                    "if_exists": {"type": "string", "enum": ["append", "replace", "fail"], "default": "append"},
                    "delimiter": {"type": "string", "default": ","},
                },
                "required": ["path"],
            },
            handler=_db_import_path,
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
        ToolSpec(
            name="graph_add_path",
            description=(
                "Read one text file or every supported text file under a directory, "
                "extract bounded knowledge-graph triples with the LLM, and persist "
                "them with the source path attached for provenance. Use this for "
                "folder-level graph ingest; use graph_upsert_triples for structured data."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Text file or directory to ingest."},
                    "recursive": {"type": "boolean", "default": True},
                    "max_triples_per_file": {
                        "type": "integer", "minimum": 1, "maximum": 200, "default": 30,
                    },
                },
                "required": ["path"],
            },
            handler=_graph_add_path,
            metadata={"group": "ingest", "surface": "graph", "access": "write"},
        ),
    ]


@tool_provider_registry.register("ingest")
class _IngestToolProvider:
    def build(self, **services: object) -> list[ToolSpec]:
        ingest = services.get("ingest_service")
        if not isinstance(ingest, IngestService):
            raise ValueError("ingest tool provider requires 'ingest_service'")
        return build_ingest_tools(ingest)


__all__ = ["build_ingest_tools"]
