"""TerminusDB-backed graph storage with versioned reads and rollback support.

The provider persists graph entities and edges in TerminusDB, while a local
SQLite history records the DataMind ingest operations associated with each
TerminusDB commit.
"""
from __future__ import annotations

import asyncio
import difflib
import json
import os
import re
import threading
import sqlite3
import shutil
import subprocess
import time
import httpx

from pathlib import Path
from typing import Any, Sequence

from datamind.core.logging import get_logger
from datamind.core.protocols import Edge, Entity, GraphPath, GraphTriple
from datamind.core.registry import graph_registry

_log = get_logger("graph.terminusdb")


@graph_registry.register("terminusdb")
class TerminusDBGraphStore:

    def __init__(
        self,
        *,
        dsn: str,
        password: str,
        persist_path: str | Path,
    ) -> None:
        self._dsn = dsn
        self._password = password
        self._path = Path(persist_path)
        self._path.mkdir(parents=True, exist_ok=True)
        self._terminusdb_home = None
        self._db = "kg_default"
        self._team = "admin"
        self._init_terminusdb()
        self._client = self._get_client()
        self._schema()
        self._dirty = False
        self._mutation_revision = 0
        self._state_lock = threading.RLock()
        self._persist_lock = asyncio.Lock()
        self._persist_worker_lock = threading.Lock()
            
        self._history_path = self._path / "version_history.sqlite3"
        with sqlite3.connect(self._history_path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS history (database TEXT, version INTEGER, payload TEXT, PRIMARY KEY(database, version))")
        
        _log.info(
            "graph_loaded",
            extra={
                "path": str(self._path),
            },
        )

    def _is_terminusdb_running(self) -> bool:
        """Check whether TerminusDB is already available."""
        try:
            response = httpx.get(
                f"{self._dsn.rstrip('/')}/api/ok",
                timeout=2.0,
            )
            return response.is_success
        except httpx.HTTPError:
            return False
    
    def _init_terminusdb(self):
        """Clone official Compose configuration and start TerminusDB."""
        if self._is_terminusdb_running():
            return
    
        repo_url = "https://github.com/terminusdb/terminusdb"
        home = Path.home() / ".datamind" / "terminusdb"
        home = home.expanduser().resolve()
        home.parent.mkdir(parents=True, exist_ok=True)
        self._terminusdb_home = home
        
        # 1. Check prerequisites.
        if not shutil.which("docker"):
            raise RuntimeError("Docker is not installed or not in PATH.")

        subprocess.run(
            ["docker", "compose", "version"],
            check=True,
            capture_output=True,
            text=True,
        )

        # 2. Clone once; preserve the existing checkout and data.
        if not (home / ".git").exists():
            if home.exists() and any(home.iterdir()):
                raise RuntimeError(
                    f"{home} exists but is not a TerminusDB checkout. "
                    "Choose another directory."
                )
            subprocess.run(
                ["git", "clone", "--depth", "1", repo_url, str(home)],
                check=True,
            )

        # 3. Create credentials if this is a new installation.
        env_file = home / ".env"
        if not env_file.exists():
            port = self._dsn.split(":")[-1]
            fd = os.open(
                env_file,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(fd, "w") as f:
                f.write(f"TERMINUSDB_SERVER_DB_PATH={str(self._path)}\n")
                f.write(f"TERMINUSDB_SERVER_PORT={port}\n")

        # 4. Start in the background. Compose's storage directories
        #    are relative to the checkout directory.
        subprocess.run(
            ["docker", "compose", "up", "-d"],
            cwd=home,
            check=True,
        )
        
        # Wait for TerminusDB to become available (up to 60 seconds).
        timeout = 60
        interval = 2
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            if self._is_terminusdb_running():
                print(f"TerminusDB is ready at {self._dsn}")
                break
            time.sleep(interval)
        else:
            raise RuntimeError(
                f"TerminusDB failed to become ready within {timeout} seconds "
                f"at {self._dsn}. Check the Docker logs with "
                f"'docker compose -f {home / 'docker-compose.yml'} logs'."
            )

    def _get_client(self):
        from terminusdb_client import Client
        if not self._dsn:
            raise ValueError("Missing TerminusDB DSN. Please configure DATAMIND__GRAPH__DSN.")

        if not self._password:
            raise ValueError("Missing TerminusDB password. Please configure DATAMIND__GRAPH__PASSWORD.")

        try:
            client = Client(self._dsn)
            client.connect(
                team=self._team,
                user=self._team,
                key=self._password,
                server_url=self._dsn,
            )
        except Exception as e:
            raise RuntimeError(
                f"TerminusDB connection failed: "
                f"dsn={self._dsn}, password={self._password}, "
                f"error_type={type(e).__name__}, "
                f"error={e}"
            ) from e
        
        if not client.has_database(self._db, team=self._team):
            client.create_database(
                self._db,
                label=self._db,
                description="Graph of DataMind stores in TerminusDB",
                include_schema=False,
            )
            
        client.connect(
            team=self._team,
            db=self._db,
            user=self._team,
            key=self._password,
            server_url=self._dsn,
        )
        return client
    
    def _normalize(self, schema: dict) -> str:
        return json.dumps(
            schema,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    
    def _schema(self) -> None:
        schema_docs = [
            {
                "@type": "Class",
                "@id": "entity",
                "label": "xsd:string",
                "outgoing_edge": {
                    "@type": "Set",
                    "@class": "GraphEdge",
                },
            },
            {
                "@type": "Class",
                "@id": "GraphEdge",
                "subject": "entity",
                "object": "entity",
                "relation": "xsd:string",
                "key": "xsd:string",
                "weight": "xsd:double",
                "source": "xsd:string",
                "properties": "sys:JSON",
                "incoming_edge": {
                    "@type": "Optional",
                    "@class": "entity",
                },
            },
        ]

        to_sync = []

        for schema in schema_docs:
            doc_id = schema["@id"]

            if not self._has_doc(doc_id, graph_type="schema"):
                to_sync.append(schema)
                continue

            existing = self._client.get_document(
                doc_id,
                graph_type="schema",
            )
            if self._normalize(existing) != self._normalize(schema):
                to_sync.append(schema)

        if to_sync:
            self._client.replace_document(
                to_sync,
                graph_type="schema",
                create=True,
                commit_msg="Synchronize graph schema",
            )

        for schema in schema_docs:
            doc_id = schema["@id"]
            if not self._has_doc(doc_id, graph_type="schema"):
                raise RuntimeError(
                    f"TerminusDB schema registration failed: {doc_id}"
                )

    def _has_doc(self, doc_id: str, graph_type=None):
        if graph_type is None:
            graph_type = 'instance'

        client = self._client
        client._check_connection()

        response = client._session.get(
            client._documents_url(),
            headers=client._default_headers,
            params={
                "id": doc_id,
                "graph_type": graph_type,
            },
            auth=client._auth(),
        )
        if response.status_code == 200:
            return True
        if response.status_code == 404:
            try:
                body = response.json()
            except ValueError:
                return False
            error = body.get("api:error", {})
            if error.get("@type") == "api:DocumentNotFound":
                return False

            response.raise_for_status()
        response.raise_for_status()
        return False
    
    async def aclose(self) -> None:
        """Stop TerminusDB only if this instance started it."""
        if not self._is_terminusdb_running() or not self._terminusdb_home:
            # manually close
            return

        process = await asyncio.create_subprocess_exec(
            "docker",
            "compose",
            "stop",
            cwd=self._terminusdb_home,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=60,
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise TimeoutError("Timed out stopping TerminusDB") from None

        if process.returncode != 0:
            raise RuntimeError(
                f"Failed to stop TerminusDB: "
                f"{stderr.decode(errors='replace').strip()}"
            )
        
    # ------------------------------------------------------------ mutation

    def _upsert_documents(self, documents, commit_msg):
        from terminusdb_client.errors import DatabaseError
        
        to_insert = []
        to_replace = []
        for doc in documents:
            doc_id = doc["@id"]
            if not self._has_doc(doc):
                to_insert.append(doc)
                continue
            existing = self._client.get_document(doc_id)

            if self._normalize(existing) != self._normalize(doc):
                to_replace.append(doc)
        
        if to_insert:
            self._client.insert_document(to_insert, commit_msg=commit_msg)
        if to_replace:
           self._client.replace_document(to_replace, commit_msg=commit_msg)
        
        return {
            "inserted": len(to_insert),
            "replaced": len(to_replace),
        }
    
    def _edge_id(self, t):
        profile_managed = bool((t.properties or {}).get("_profile_managed"))
        origin = "profile" if profile_managed else (t.source or "runtime")
        edge_key = f"{t.relation}\x1f{origin}"
        subject = (
            t.subject[len(t.subject_type):].lstrip("/")
            if t.subject.startswith(t.subject_type)
            else t.subject
        )
        object = (
            t.object[len(t.object_type):].lstrip("/")
            if t.object.startswith(t.object_type)
            else t.object
        )
        return f"GraphEdge/{subject}_{object}_{edge_key}", edge_key
        
    async def upsert_triples(self, triples: Sequence[GraphTriple]) -> None:
        from terminusdb_client.woqlquery import WOQLQuery
        added_triples, updated_triples = [], []
        with self._state_lock:
            for t in triples:
                # Nodes
                nodes = []
                node_replace = False
                edge_replace = False
                for side, node_id, node_type in (
                    ("subject", t.subject, t.subject_type),
                    ("object", t.object, t.object_type),
                ):
                    entity_id = f"entity/{node_id}"
                    node = {"@type": node_type, "@id": entity_id, "label": node_id}
                    result = self._upsert_documents([node], commit_msg=f"Add Node {entity_id}")
                    node_replace = bool(result["replaced"])
                    nodes.append(node)
                
                # Profile snapshots and runtime writes have separate identities;
                # exact duplicates within either origin overwrite deterministically.
                edge_id, edge_key = self._edge_id(t)
                subject_id = f"entity/{t.subject}"
                object_id = f"entity/{t.object}"
                edge_doc = {
                    "@type": "GraphEdge",
                    "@id": edge_id,
                    # TerminusDB requires ``@type: @id`` to distinguish a
                    # document reference from an embedded document.
                    "subject": {"@id": subject_id, "@type": "@id"},
                    "object": {"@id": object_id, "@type": "@id"},
                    "relation": t.relation,
                    "key": edge_key,
                    "weight": float(t.confidence),
                    "source": t.source or "",
                    "properties": {f"p_{k}": v for k, v in (t.properties or {}).items()},
                }
                
                result = self._upsert_documents([edge_doc], commit_msg=f"Add Edge {t.subject} --{t.relation}--> {t.object}")
                node_replace = bool(result["replaced"])
                WOQLQuery().add_triple(
                    nodes[0]["@id"],
                    "outgoing_edge",
                    edge_doc["@id"],
                ).execute(self._client)
                
                WOQLQuery().add_triple(
                    edge_doc["@id"],
                    "incoming_edge",
                    nodes[1]["@id"],
                ).execute(self._client)
                if node_replace or edge_replace:
                    updated_triples.append(t)
                else:
                    added_triples.append(t)
            self._mutation_revision += 1
            self._dirty = True
            return self.get_triples_info(added=added_triples, updated=updated_triples)
    
    def _validate_key(self, doc, key, value=None):
        props = doc.get("properties") or {}
        if key == "profile":
            return bool(props.get("p__profile_managed"))
        elif key == "source":
            if not value:
                return False
            source_path = str(props.get("p__source_path") or "")
            return (
                source_path == value
                or re.fullmatch(
                    re.escape(value) + r"#part=\d+",
                    source_path,
                ) is not None
            )
        elif key == "lineage":
            return props.get("p__lineage_root") == value
        return True
          
    def _get_triples(self, key="profile", value=None) -> list[GraphTriple]:
        with self._state_lock:
            docs = self._client.get_documents_by_type(
                "GraphEdge",
                as_list=True,
            )

            result = []

            for doc in docs:
                if not self._validate_key(doc, key, value): continue
                props = doc.get("properties") or {}
                subject = doc.get("subject")
                obj = doc.get("object")
                if isinstance(subject, dict):
                    subject = subject.get("@id")
                if isinstance(obj, dict):
                    obj = obj.get("@id")
                if not subject or not obj:
                    continue
                result.append(
                    GraphTriple(
                        subject=subject,
                        relation=doc["relation"],
                        object=obj,
                        subject_type="entity",
                        object_type="entity",
                        confidence=float(doc.get("weight", 1.0)),
                        source=doc.get("source", ""),
                        properties=props,
                    )
                )
            return result
    
    def _diff_triples(self, previous_triples, triples):
        def edge_identity(t: GraphTriple) -> tuple:
            source_path = getattr(t, "source_path", "") or ""

            return (
                t.subject,
                t.relation,
                t.object,
                source_path,
            )
        
        def edge_payload(t: GraphTriple) -> str:
            payload = {
                "confidence": float(t.confidence),
                "source": getattr(t, "source", "") or "",
                "properties": dict(t.properties or {}),
            }

            return json.dumps(
                payload,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            )
        old = {edge_identity(t): t for t in previous_triples}
        new = {edge_identity(t): t for t in triples}

        added = [new[k] for k in new.keys() - old.keys()]
        removed = [old[k] for k in old.keys() - new.keys()]

        updated = [
            new[k]
            for k in old.keys() & new.keys()
            if edge_payload(old[k]) != edge_payload(new[k])
        ]
        return added, updated, removed

    def _delete_edge_document_and_links(self, edge_doc: dict) -> None:
        from terminusdb_client.woqlquery import WOQLQuery
        edge_id = edge_doc["@id"]
        subject = edge_doc.get("subject")
        obj = edge_doc.get("object")

        if isinstance(subject, dict):
            subject = subject.get("@id")
        if isinstance(obj, dict):
            obj = obj.get("@id")

        if not subject or not obj:
            raise ValueError(
                f"GraphEdge {edge_id} Lack of effective subject/object: "
                f"subject={subject}, object={obj}"
            )

        WOQLQuery().delete_triple(
            subject,
            "outgoing_edge",
            edge_id,
        ).execute(self._client)
        WOQLQuery().delete_triple(
            edge_id,
            "incoming_edge",
            obj,
        ).execute(self._client)
        if self._has_doc(edge_id):
            self._client.delete_document(
                edge_id,
                commit_msg=f"Delete GraphEdge {edge_id}",
            )
          
    def _delete_orphaned_nodes(
        self,
        candidate_node_ids: set[str],
        *,
        key: str = "profile",
        value: str | None = None,
    ) -> list[str]:
        if not candidate_node_ids:
            return []

        connected_node_ids = set()
        docs = self._client.get_all_documents(as_list=True)
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            doc_id = doc.get("@id", "")
            if not doc_id.startswith("GraphEdge/"):
                continue
                
            subject = doc.get("subject")
            obj = doc.get("object")

            if subject:
                if self._validate_key(subject, key, value):
                    connected_node_ids.add(subject.get("@id"))
            if obj:
                if self._validate_key(obj, key, value):
                    connected_node_ids.add(obj.get("@id"))

        orphaned_node_ids = sorted(
            node_id
            for node_id in candidate_node_ids
            if node_id not in connected_node_ids
        )

        deleted_node_ids = []
        for node_id in orphaned_node_ids:
            if not self._has_doc(node_id):
                continue
            self._client.delete_document(
                node_id,
                commit_msg=f"Delete orphaned profile node {node_id}",
            )
            deleted_node_ids.append(node_id)
        return deleted_node_ids

    async def _delete_triples(self, triples, key, value=None):
        candidate_nodes = set()
        for t in triples:
            candidate_nodes.add(t.subject)
            candidate_nodes.add(t.object)
        for t in triples:
            edge_id, _ = self._edge_id(t)
            edge_doc = self._client.get_document(edge_id)
            if edge_doc:
                self._delete_edge_document_and_links(edge_doc)

        if candidate_nodes:
            self._delete_orphaned_nodes(candidate_nodes, key=key, value=value)
    
    def _docs_to_triples(self, edge_docs: list[dict]) -> list[GraphTriple]:
        triples = []
        for doc in edge_docs:
            subject = doc.get("subject")
            obj = doc.get("object")
            if isinstance(subject, dict):
                subject = subject.get("@id")
            if isinstance(obj, dict):
                obj = obj.get("@id")
            if not subject or not obj:
                continue
            triples.append(
                GraphTriple(
                    subject=subject,
                    relation=doc["relation"],
                    object=obj,
                    subject_type="entity",
                    object_type="entity",
                    confidence=float(doc.get("weight", 1.0)),
                    source=doc.get("source", ""),
                    properties=doc.get("properties") or {},
                )
            )
        return triples

    def get_triples_info(self, added=[], updated=[], removed=[]):
        def to_dict(triple: GraphTriple) -> dict:
            props = triple.properties or {}
            return {
                "subject": triple.subject,
                "relation": triple.relation,
                "object": triple.object,
                "source": props.get("_source_path") or triple.source or "",
            }

        def serialize(triples) -> list[dict]:
            return sorted(
                (to_dict(triple) for triple in triples),
                key=lambda t: (t["source"], t["subject"], t["relation"], t["object"]),
            )

        return {
            "triples_added": len(added),
            "triples_updated": len(updated),
            "triples_deleted": len(removed),
            "added_triples": serialize(added),
            "updated_triples": serialize(updated),
            "deleted_triples": serialize(removed),
        }
    
    async def reconcile_profile_triples(self, triples):
        previous_triples = self._get_triples(key="profile")
        added, updated, removed = self._diff_triples(
            previous_triples,
            triples,
        )
        await self._delete_triples(removed, key="profile")
        changed = [*added, *updated]
        if changed:
            await self.upsert_triples(changed)

        return self.get_triples_info(added, updated, removed)
        
    async def reconcile_source_triples(
        self, source: str, triples: Sequence[GraphTriple]
    ) -> None:
        """Replace edges produced from one source file while preserving others."""
        previous_triples = self._get_triples(key="source", value=source)
        added, updated, removed = self._diff_triples(
            previous_triples,
            triples,
        )
        await self._delete_triples(removed, key="source", value=source)
        changed = [*added, *updated]
        if changed:
            await self.upsert_triples(changed)
        
        return self.get_triples_info(added, updated, removed)
        
    def source_paths(self) -> set[str]:
        """List document-managed origins, excluding lineage and explicit writes."""
        with self._state_lock:
            docs = self._client.get_documents_by_type(
                "GraphEdge",
                as_list=True,
            )

            paths = set()
            for doc in docs:
                props = doc.get("properties") or {}
                source_path = props.get("p__source_path")

                if not props.get("p__source_managed") or not source_path:
                    continue

                path = re.sub(r"#part=\d+$", "", str(source_path))
                paths.add(path)

            return paths
            
    async def reconcile_lineage_triples(
        self, root: str, triples: Sequence[GraphTriple]
    ) -> None:
        """Replace all deterministic lineage edges for one workspace root."""
        previous_triples = self._get_triples(key="lineage", value=root)
        added, updated, removed = self._diff_triples(
            previous_triples,
            triples,
        )
        await self._delete_triples(removed, key="lineage", value=root)
        changed = [*added, *updated]
        if changed:
            await self.upsert_triples(changed)
            
        return self.get_triples_info(added, updated, removed)
    
    async def delete_from_source(
        self,
        source: str,
        key: str,
        *,
        value: str | None = None,
    ) -> dict:
        if not source:
            raise ValueError("Source cannot be empty")

        with self._state_lock:
            edge_docs = self._client.get_documents_by_type(
                "GraphEdge",
                as_list=True,
            )
            def matches_source(doc):
                if doc.get("source") == source:
                    return True
                props = doc.get("properties") or {}
                if props.get("p__source_path") == source:
                    return True
                return False

            target_edges = [
                doc for doc in edge_docs
                if matches_source(doc)
            ]
            target_triples = self._docs_to_triples(target_edges)
            await self._delete_triples(target_triples, key, value)
            
        return {
            "triples_deleted": len(target_triples)
        }
        
    async def reset(self) -> None:
        await asyncio.to_thread(self._reset_sync)

    def _reset_sync(self) -> None:
        from terminusdb_client import Client

        admin_client = Client(self._dsn)
        admin_client.connect(
            team=self._team,
            user=self._team,
            key=self._password,
            server_url=self._dsn,
        )

        if admin_client.has_database(self._db, team=self._team):
            admin_client.delete_database(self._db, team=self._team)

        admin_client.create_database(
            self._db,
            team=self._team,
            label=self._db,
            description="Graph of DataMind stores in TerminusDB",
            include_schema=False,
        )

        self._client = Client(self._dsn)
        self._client.connect(
            team=self._team,
            db=self._db,
            user=self._team,
            key=self._password,
            server_url=self._dsn,
        )

        self._schema()
        with sqlite3.connect(self._history_path) as conn:
            conn.execute("DELETE FROM history WHERE database = ?", (self._db,))
        
    # ------------------------------------------------------------- lookup

    def _documents_at_version(self, version: str | None) -> list[dict]:
        """Read instance documents from HEAD or an immutable commit."""
        with self._state_lock:
            previous_ref = self._client.ref
            try:
                self._client.ref = version
                return self._client.get_all_documents(as_list=True)
            finally:
                self._client.ref = previous_ref

    @staticmethod
    def _entity_id(document_id: str) -> str:
        return document_id.removeprefix("entity/")

    async def search_entities(self, query: str, *, top_k: int = 5, version: str | None = None,) -> list[Entity]:
        q = query.lower().strip()
        if not q:
            return []
        scored: list[tuple[float, str, dict]] = []
        docs = self._documents_at_version(version)
        for data in docs:
            if data.get("@type") == "GraphEdge":
                continue
            nid = data["@id"]
            public_id = self._entity_id(nid)
            label = str(data.get("label", public_id))
            # Exact and substring hits beat fuzzy.
            if public_id.lower() == q or label.lower() == q:
                score = 1.0
            elif q in public_id.lower() or q in label.lower():
                score = 0.7
            else:
                ratio = difflib.SequenceMatcher(None, q, label.lower()).ratio()
                if ratio < 0.4:
                    continue
                score = 0.4 + 0.4 * ratio  # cap below substring score
            scored.append((score, public_id, data))
        scored.sort(key=lambda x: -x[0])
        return [
            Entity(
                id=nid,
                label=str(data.get("label", nid)),
                type=str(data.get("@type", "entity")),
                score=score,
                properties={
                    k: v for k, v in data.items() if k not in {"label", "@type"}
                },
            )
            for score, nid, data in scored[:top_k]
        ]

    async def neighbors(
        self,
        entity: str,
        *,
        direction: str = "both",
        relation_filter: list[str] | None = None,
        limit: int = 100,
        version: str | None = None,
    ) -> list[Edge]:
        if direction not in {"in", "out", "both"}:
            raise ValueError(f"Invalid direction: {direction}")
        if limit <= 0:
            return []

        docs = self._documents_at_version(version)

        allowed = set(relation_filter) if relation_filter else None
        edges: list[Edge] = []

        for doc in docs:
            if doc.get("@type") != "GraphEdge":
                continue
            subject = doc.get("subject")
            obj = doc.get("object")
            if isinstance(subject, dict):
                subject = subject.get("@id")
            if isinstance(obj, dict):
                obj = obj.get("@id")
            if not isinstance(subject, str) or not isinstance(obj, str):
                continue
            subject_id = self._entity_id(subject)
            object_id = self._entity_id(obj)
            relation = doc.get("relation", "related")

            if allowed is not None and relation not in allowed:
                continue
            matches_out = (
                direction in {"out", "both"} and subject_id == entity
            )
            matches_in = (
                direction in {"in", "both"} and object_id == entity
            )

            if matches_out or matches_in:
                edges.append(self._edge(subject_id, object_id, doc))
                if len(edges) >= limit:
                    break
        return edges

    async def traverse(
        self,
        start: str,
        *,
        max_hops: int = 3,
        relation_filter: list[str] | None = None,
        max_results: int = 100,
        version: str | None = None
    ) -> list[GraphPath]:
        from terminusdb_client import WOQLQuery
        
        if max_hops < 1 or max_results <= 0:
            return []

        internal_start = (
            start if start.startswith("entity/") else f"entity/{start}"
        )
        allowed = set(relation_filter) if relation_filter else None
        # Each business edge of the jump operation = Entity -> GraphEdge -> Entity
        pattern = (
            f"(outgoing_edge,incoming_edge){{1,{max_hops}}}"
        )
        if version is None:
            query = WOQLQuery().path(
                internal_start,
                pattern,
                "v:End",
                "v:Path",
            )
        else:
            query = WOQLQuery().using(
                f"{self._team}/{self._db}/local/commit/{version}"
            ).path(
                internal_start,
                pattern,
                "v:End",
                "v:Path",
            )
            
        docs = self._documents_at_version(version)
        
        result = self._client.query(query)
        bindings = result.get("bindings", [])
        
        edge_doc_cache: dict[str, dict] = {}
        def get_edge_doc(edge_id: str) -> dict:
            if edge_id not in edge_doc_cache:
                doc = next(
                    (d for d in docs if d.get("@id") == edge_id),
                    None,
                )
                if not doc:
                    raise ValueError(f"GraphEdge document not found: {edge_id}")
                edge_doc_cache[edge_id] = doc
            return edge_doc_cache[edge_id]
        
        candidates = []
        for binding in bindings:
            raw_path = binding.get("Path", [])
            if len(raw_path) % 2 != 0:
                continue
            internal_nodes = [internal_start]
            nodes = [self._entity_id(internal_start)]
            edges = []
            seen = {internal_start}
            bfs_keys = []
            valid = True
            for i in range(0, len(raw_path), 2):
                out_edge = raw_path[i]
                in_edge = raw_path[i + 1]

                if not (
                    out_edge.get("woql:predicate", "").endswith(
                        "outgoing_edge"
                    )
                    and in_edge.get("woql:predicate", "").endswith(
                        "incoming_edge"
                    )
                ):
                    valid = False
                    break

                source_id = out_edge.get("woql:subject")
                edge_id = out_edge.get("woql:object")
                intermediate_id = in_edge.get("woql:subject")
                target_id = in_edge.get("woql:object")

                if not edge_id or intermediate_id != edge_id:
                    valid = False
                    break
                if source_id != internal_nodes[-1] or not target_id:
                    valid = False
                    break
                if target_id in seen:
                    valid = False
                    break

                edge_doc = get_edge_doc(edge_id)
                relation = edge_doc.get("relation", "related")
                if allowed is not None and relation not in allowed:
                    valid = False
                    break
                edge_obj = self._edge(
                    self._entity_id(source_id),
                    self._entity_id(target_id),
                    edge_doc,
                )
                edges.append(edge_obj)
                internal_nodes.append(target_id)
                nodes.append(self._entity_id(target_id))
                seen.add(target_id)

                edge_key = str(
                    edge_doc.get("key")
                    or edge_doc.get("@id")
                    or edge_id
                )
                bfs_keys.append(
                    (target_id, str(relation), edge_key)
                )

            if not valid or not edges:
                continue
            if len(edges) > max_hops:
                continue
            score = sum(edge.weight for edge in edges) / len(edges)
            path_obj = GraphPath(
                nodes=nodes,
                edges=edges,
                score=score,
            )
            candidates.append(
                (len(edges), tuple(bfs_keys), path_obj)
            )

        candidates.sort(key=lambda item: (item[0], item[1]))
        selected = candidates[:max_results]
        selected.sort(key=lambda item: -item[2].score)
        return [item[2] for item in selected]

    async def rollback(self, version: str):
        before_version = self._get_latest_version()
        diff = self._client.diff_version(before_version, version)
        if not diff:
            return {
                "success": True,
                "before_version": before_version,
                "target_version": version,
                "message": "Current status matches the target version; no rollback is required.",
                "info": {
                    "triples_added": 0,
                    "triples_updated": 0,
                    "triples_deleted": 0,
                },
                "diff": [],
            }

        message = f"Rollback to version {version}"
        self._client.apply(
            before_version=before_version,
            after_version=version,
            message=message,
        )
        current = self._get_latest_version()
        info = {
            "success": True,
            "message": message,
            "version": current, 
            "diff": diff,
        }
        await self.add_version(info)
        return info
    
    async def add_version(self, info: dict):
        version = self._get_latest_version()
        with sqlite3.connect(self._history_path) as conn:
            conn.execute("INSERT OR REPLACE INTO history VALUES (?, ?, ?)",
                         (self._db, version, json.dumps(info, default=str)))
    
    async def list_versions(self):
        with sqlite3.connect(self._history_path) as conn:
            return [
                {"version": row[0], "payload": json.loads(row[1])}
                for row in conn.execute(
                    "SELECT version, payload FROM history "
                    "WHERE database = ? ORDER BY rowid",
                    (self._db,),
                )
            ]     
    # -------------------------------------------------------------- helpers

    @staticmethod
    def _edge(u: str, v: str, data: dict) -> Edge:
        return Edge(
            source=u,
            target=v,
            relation=str(data.get("relation", "related")),
            weight=float(data.get("weight", 1.0)),
            properties={
                k[2:] if k.startswith("p_") else k: val
                for k, val in data.items()
                if k not in {"relation", "weight"}
            },
        )

    def stats(self) -> dict[str, int]:
        """Return the number of graph nodes and edges in TerminusDB."""
        with self._state_lock:
            edge_docs = self._client.get_documents_by_type(
                "GraphEdge",
                as_list=True,
            )

            node_ids: set[str] = set()
            edge_count = 0

            for doc in edge_docs:
                if not isinstance(doc, dict):
                    continue

                subject = doc.get("subject")
                obj = doc.get("object")

                # TerminusDB may return references as {"@id": "..."}.
                if isinstance(subject, dict):
                    subject = subject.get("@id")
                if isinstance(obj, dict):
                    obj = obj.get("@id")

                # Count only valid graph edges.
                if not isinstance(subject, str) or not subject:
                    continue
                if not isinstance(obj, str) or not obj:
                    continue

                node_ids.add(subject)
                node_ids.add(obj)
                edge_count += 1

            return {
                "nodes": len(node_ids),
                "edges": edge_count,
            }

    def _get_latest_version(self):
        history = self._client.get_commit_history(max_history=1)

        if not history:
            raise RuntimeError("No commit history found")

        if isinstance(history, dict):
            first_key = next(iter(history))
            first_value = history[first_key]

            if isinstance(first_value, dict):
                return (
                    first_value.get("identifier")
                    or first_value.get("commit_id")
                    or first_key
                )

            return first_key

        if isinstance(history, list):
            item = history[0]
            return (
                item.get("identifier")
                or item.get("commit_id")
                or item.get("commit")
            )

        raise TypeError(f"Unexpected commit history: {type(history)}")



