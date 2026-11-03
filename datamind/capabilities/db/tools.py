"""DB tools exposed to the agent."""
from __future__ import annotations

from typing import Any
from dataclasses import replace
from .query_contract import INTENT_SCHEMA, ANSWER_SCHEMA, AnswerSchemas, query_guard, validate_columns, validate_final_rows

from datamind.core.tools import ToolSpec, tool_provider_registry

from .service import DBService
from .native_sqlite import databases, database_target


def build_db_tools(db: DBService) -> list[ToolSpec]:
    async def _answer_schema(question, columns, row_shape, revision_reason=None):
        return AnswerSchemas(db.result_dir).define(question, columns, row_shape, revision_reason)

    async def _list_databases() -> dict:
        return {"databases": [
            {k: v for k, v in entry.items() if k not in {"path", "schema_objects", "tables"}}
            | {"tables": [t['native_table'] for t in entry['tables']]}
            for entry in databases(db).values()], "default_database": "managed"}

    async def _list_tables(database_id: str | None = None) -> dict:
        async with database_target(db, database_id) as target:
            return {"tables": await target.list_tables(), "database_id": database_id}

    async def _list_tables_at_version(version: str) -> dict:
        return {"tables": await db.list_tables_at_version(version=version)}

    async def _describe(table: str, database_id: str | None = None) -> dict:
        async with database_target(db, database_id) as target:
            schema = await target.describe(table)
            value = schema.model_dump()
            if database_id is not None:
                entry = databases(db)[database_id]
                value['source_metadata'] = next((t for t in entry['tables'] if t['native_table'] == table), None)
                value['schema_objects'] = [obj for obj in entry['schema_objects'] if obj['table'] == table]
                value['database_id'] = database_id
            return value

    async def _describe_at_version(table: str, version: str) -> dict:
        schema = await db.describe_at_version(table, version=version)
        return schema.model_dump()

    async def _query_sql(sql: str, database_id: str | None = None, database_ids: list[str] | None = None) -> dict:
        async with database_target(db, database_id, database_ids) as target:
            result = await target.query_sql(sql)
        payload = result.model_dump()
        payload["returned_count"] = len(result.rows)
        payload["total_count"] = None
        payload["next_cursor"] = None
        return payload

    async def _export_sql(sql: str, database_id: str | None = None, database_ids: list[str] | None = None) -> dict:
        async with database_target(db, database_id, database_ids) as target:
            return await target.export_sql(sql)

    async def _query_sql_at_version(sql: str, version: str) -> dict:
        result = await db.query_sql_at_version(sql, version=version)
        payload = result.model_dump()
        payload["returned_count"] = len(result.rows)
        payload["total_count"] = None
        payload["next_cursor"] = None
        return payload

    async def _query_nl(question: str, tables: list[str] | None = None, database_id: str | None = None) -> dict:
        async with database_target(db, database_id) as target:
            return await target.query_nl(question, tables=tables)

    async def _query_nl_at_version(question: str, version: str, tables: list[str] | None = None) -> dict:
        return await db.query_nl_at_version(question, tables=tables, version=version)

    async def _list_versions() -> dict:
        return await db.list_versions()

    async def _rollback(version: str) -> dict:
        return await db.rollback(version=version)

    tools = [
        ToolSpec(name='db_answer_schema',
                 description='Before final-answer SQL, declare only the outputs requested by the user. Each column must explain its requirement; filters, IDs and evidence are not automatically outputs. Keep exploratory columns separate. Returns a request-bound schema_id; changes require an explicit revision_reason and invalidate old final exports. This is not an assessment of semantic correctness.',
                 input_schema=ANSWER_SCHEMA, handler=_answer_schema,
                 metadata={'group': 'db', 'surface': 'db', 'access': 'read'}),
        ToolSpec(name='db_list_databases',
                 description='List registered native SQLite databases, their IDs, source versions, storage mode and original table names. Omit database_id on other DB tools to query the managed CSV/JSON database.',
                 input_schema={'type': 'object', 'properties': {}}, handler=_list_databases,
                 metadata={'group': 'db', 'surface': 'db', 'access': 'read'}),
        ToolSpec(name='db_export_sql',
                 description='Execute read-only SQL and stream its rows to a managed CSV. Returns a small preview, full row count, file hash and revision. Check complete/truncated before using the file as an answer. The configured row limit still applies.',
                 input_schema={'type': 'object', 'properties': {'sql': {'type': 'string'}}, 'required': ['sql']},
                 handler=_export_sql, metadata={'group': 'db', 'surface': 'db', 'access': 'read'}),
        ToolSpec(
            name="db_list_tables",
            description="List the tables available in the active SQL database.",
            input_schema={"type": "object", "properties": {}},
            handler=_list_tables,
            metadata={"group": "db", "surface": "db", "access": "read"},
        ),
        ToolSpec(
            name="db_describe_table",
            description=(
                "Describe a table: column names, types, primary keys, and an estimated row count. "
                "Use this before writing SQL by hand so you reference existing columns. SQLite "
                "TEXT affinity does not prove that values are non-numeric; inspect representative "
                "rows before extrema or arithmetic when the business field is numeric."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "table": {"type": "string", "description": "Table name."},
                },
                "required": ["table"],
            },
            handler=_describe,
            metadata={"group": "db", "surface": "db", "access": "read"},
        ),
        ToolSpec(
            name="db_query_sql",
            description=(
                "Execute a single read-only SELECT against the database. "
                "The runtime applies the configured row limit, when enabled, and rejects DML/DDL. "
                "Prefer this when you already know the SQL you want. For numeric-looking TEXT, "
                "cast explicitly before ordering or arithmetic. Treat Unicode-whitespace-only "
                "text as empty, preserve stored percentage scale unless conversion is requested, "
                "and never invent an unstated numeric encoding for categories."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "A single SELECT statement."},
                },
                "required": ["sql"],
            },
            handler=_query_sql,
            metadata={"group": "db", "surface": "db", "access": "read"},
        ),
        ToolSpec(
            name="db_query_nl",
            description=(
                "Answer a question against the SQL database. The runtime generates a SELECT from your question "
                "and returns the rows plus the generated SQL. Prefer this when you don't know the schema cold. "
                "Generated SQL applies numeric casts, empty-cell normalization, and stored-unit preservation "
                "where the question requires them."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "Natural-language question."},
                    "tables": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional subset of tables to focus on.",
                    },
                },
                "required": ["question"],
            },
            handler=_query_nl,
            metadata={"group": "db", "surface": "db", "access": "read"},
        ),
    ]

    for spec in tools:
        if spec.name in {'db_list_tables', 'db_describe_table', 'db_query_sql', 'db_export_sql', 'db_query_nl'}:
            spec.input_schema['properties']['database_id'] = {
                'type': 'string', 'description': 'Optional native SQLite ID from db_list_databases. Uses original table names; omit for managed tables.'}
            if spec.name in {'db_query_sql', 'db_export_sql'}:
                spec.input_schema['properties']['database_ids'] = {
                    'type': 'array', 'items': {'type': 'string'},
                    'description': 'Additional registered SQLite IDs for cross-file joins. Reference each as "database_id"."original_table". Omit database_id to join these with managed CSV/JSON tables. No arbitrary paths or ATTACH statements.'}

    if db is not None and hasattr(db, "dialect") and db.dialect.name == "dolt":
        # Versioned DB Tools
        tools.extend([
            ToolSpec(
                name="db_list_versions",
                description=(
                    "List the commit history of the Dolt-backed database, "
                    "most recent first. Each entry has "
                    "{commit, author, timestamp, message}. "
                    "The list includes the initial repository commit. "
                    "Use the returned `commit` value as the `version` argument "
                    "for the other db_*_at_version tools and for db_rollback."
                ),
                input_schema={"type": "object", "properties": {}},
                handler=_list_versions,
                metadata={"group": "db", "surface": "db", "access": "read"},
            ),
            ToolSpec(
                name="db_list_tables_at_version",
                description=(
                    "List tables that existed in a specific historical version "
                    "of the database. Use this to see what tables were available "
                    "at a point in time. Call db_list_versions first to obtain "
                    "a version identifier."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "version": {
                            "type": "string",
                            "description": "The string of version identifier. Use db_list_versions to see available versions.",
                        },
                    },
                    "required": ["version"],
                },
                handler=_list_tables_at_version,
                metadata={"group": "db", "surface": "db", "access": "read"},
            ),
            ToolSpec(
                name="db_describe_table_at_version",
                description=(
                    "Describe a table at a specific historical version: column "
                    "names, types, primary key flags, and an estimated row count. "
                    "Call this before hand-writing SQL at that version so you "
                    "reference existing columns only. Call db_list_versions and "
                    "db_list_tables_at_version first if needed."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "table": {"type": "string", "description": "Table name."},
                        "version": {
                            "type": "string",
                            "description": "The string of version identifier. Use db_list_versions to see available versions.",
                        },
                    },
                    "required": ["table", "version"],
                },
                handler=_describe_at_version,
                metadata={"group": "db", "surface": "db", "access": "read"},
            ),
            ToolSpec(
                name="db_query_sql_at_version",
                description=(
                    "Execute a single read-only SELECT against a specific "
                    "historical version of the database. The runtime enforces a "
                    "row limit and rejects anything that looks like DML/DDL. "
                    "You do NOT need to write `AS OF '<version>'` yourself — the "
                    "runtime injects it. For numeric-looking TEXT columns, cast "
                    "explicitly before ordering or arithmetic."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "sql": {"type": "string", "description": "A single SELECT statement."},
                        "version": {
                            "type": "string",
                            "description": "The string of version identifier. Use db_list_versions to see available versions.",
                        },
                    },
                    "required": ["sql", "version"],
                },
                handler=_query_sql_at_version,
                metadata={"group": "db", "surface": "db", "access": "read"},
            ),
            ToolSpec(
                name="db_query_nl_at_version",
                description=(
                    "Answer a natural-language question against a specific "
                    "historical version of the database. The runtime generates "
                    "a SELECT from the question (using that version's schema) "
                    "and returns the rows plus the generated SQL. Prefer this "
                    "when you don't know the schema cold."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "question": {"type": "string", "description": "Natural-language question."},
                        "tables": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional subset of tables to focus on.",
                        },
                        "version": {
                            "type": "string",
                            "description": "The string of version identifier. Use db_list_versions to see available versions.",
                        },
                    },
                    "required": ["question", "version"],
                },
                handler=_query_nl_at_version,
                metadata={"group": "db", "surface": "db", "access": "read"},
            ),
            ToolSpec(
                name="db_rollback",
                description=(
                    "Roll the database's *data* back to the snapshot of a specific "
                    "historical version, identified by its commit hash. "
                    "The full commit history is preserved: a new revert commit is "
                    "appended on top of HEAD for every commit between the target "
                    "version and the current HEAD. The target version and everything "
                    "after it remain visible in db_list_versions. "
                    "This is destructive to the *current working data* — call only "
                    "after the user confirms they want to discard changes made after "
                    "the target version."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "version": {
                            "type": "string",
                            "description": "The string of version identifier. Use db_list_versions to see available versions.",
                        },
                    },
                    "required": ["version"],
                },
                handler=_rollback,
                metadata={"group": "db", "surface": "db", "access": "write", "destructive": True,},
            ),
        ])

    guarded_names = {'db_query_sql', 'db_export_sql', 'db_query_nl', 'db_query_sql_at_version', 'db_query_nl_at_version'}
    for index, spec in enumerate(tools):
        if spec.name not in guarded_names:
            continue
        async def guarded(intent=None, _handler=spec.handler, **arguments):
            dialect = 'sqlite' if arguments.get('database_id') or arguments.get('database_ids') else db.dialect.name
            with query_guard(db, intent, sql=arguments.get('sql'), dialect_name=dialect):
                result = await _handler(**arguments)
                validate_columns(result['columns'])
                validate_final_rows(result.get('row_count', len(result.get('rows', []))), result.get('truncated', False))
                if intent is not None:
                    result['query_intent'] = intent
                return result
        schema = {**spec.input_schema, 'properties': {**spec.input_schema['properties'], 'intent': INTENT_SCHEMA}}
        # Python data-plane compatibility is retained. MCP makes this required,
        # and query_guard also enforces it for contexts bound by an agent host.
        tools[index] = replace(spec, input_schema=schema, handler=guarded,
            description=spec.description + ' Declare intent (purpose, question, exact ordered return_columns) before querying. '
            'Computed expressions need AS aliases. purpose=answer also needs the schema_id from db_answer_schema; '
            'return only that schema, without filter/evidence columns. Metadata discovery tools need no intent.')
    return tools

@tool_provider_registry.register("db")
class _DBToolProvider:
    def build(self, **services: Any) -> list[ToolSpec]:
        db = services.get("db_service")
        if db is None:
            raise ValueError("db tool provider requires 'db_service'")
        return build_db_tools(db)


__all__ = ["build_db_tools"]
