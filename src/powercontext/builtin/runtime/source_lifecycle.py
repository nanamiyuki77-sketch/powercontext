# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# ruff: noqa: TRY003
"""Scope-local evidence dependencies and transactional availability propagation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator
from sqlalchemy import insert, select, update

from powercontext.artifacts import ArtifactRef, MemoryCitation
from powercontext.builtin.persistence.tables import SOURCE_LIFECYCLE_TABLE as GRAPH
from powercontext.sources import SourceRef

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncConnection

    from powercontext.builtin.runtime.relational import _ScopedServices


class SourceLifecycleError(ValueError):
    """Invalid dependency registration, event or use of a registered node."""


class SourceLifecycleEvent(BaseModel):
    """One sequenced availability decision for a source revision."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    node_id: str
    reason: str
    revision: int = Field(gt=0)
    sequence: int = Field(gt=0)
    available: bool

    @field_validator("node_id", "reason")
    @classmethod
    def _nonblank(cls, value: str) -> str:
        if not value.strip():
            raise SourceLifecycleError("expected nonblank text")
        return value


Reference = SourceRef | MemoryCitation | ArtifactRef
Policy = Literal["all", "any"]


class _Node(BaseModel):
    reference: Reference
    parents: tuple[str, ...] = ()
    policy: Policy = "all"
    revision: int | None = None
    available: bool = True
    event: SourceLifecycleEvent | None = None

    def supported(self, graph: dict[str, _Node]) -> bool:
        combine = all if self.policy == "all" else any
        return combine(graph[parent].available for parent in self.parents)


_GRAPH = TypeAdapter(dict[str, _Node])


async def _load(connection: AsyncConnection, scope: str) -> dict[str, _Node]:
    payload = await connection.scalar(select(GRAPH.c.payload).where(GRAPH.c.scope_id == scope))
    return {} if payload is None else _GRAPH.validate_json(payload)


async def unavailable_topics(connection: AsyncConnection, scope: str) -> set[tuple[str, int]]:
    graph = await _load(connection, scope)
    return {
        (node.reference.artifact_id, node.reference.revision)
        for node in graph.values()
        if isinstance(node.reference, ArtifactRef) and not node.available
    }


class SourceLifecycleApplication:
    def __init__(self, for_scope: Callable[[str], ScopedSourceLifecycle]) -> None:
        self.for_scope = for_scope


class ScopedSourceLifecycle:
    def __init__(self, services: _ScopedServices) -> None:
        self.services = services

    async def register_source(self, node_id: str, source_ref: SourceRef, *, revision: int) -> None:
        if type(revision) is not int or revision <= 0 or not isinstance(source_ref, SourceRef):
            raise SourceLifecycleError("invalid source registration")
        await self._register(node_id, _Node(reference=source_ref, revision=revision))

    async def register_memory(
        self, node_id: str, citation: MemoryCitation, *, parents: tuple[str, ...], policy: Policy
    ) -> None:
        if not isinstance(citation, MemoryCitation):
            raise SourceLifecycleError("expected a memory citation")
        await self._derived(node_id, citation, parents, policy)

    async def register_topic(
        self, node_id: str, artifact_ref: ArtifactRef, *, parents: tuple[str, ...], policy: Policy
    ) -> None:
        if not isinstance(artifact_ref, ArtifactRef):
            raise SourceLifecycleError("expected a topic reference")
        await self._derived(node_id, artifact_ref, parents, policy)

    async def _derived(self, node_id: str, ref: Reference, parents: tuple[str, ...], policy: Policy) -> None:
        if policy not in ("all", "any") or not isinstance(parents, (list, tuple)) or not parents:
            raise SourceLifecycleError("expected a policy and at least one parent")
        if any(not isinstance(p, str) for p in parents) or len(set(parents)) != len(parents):
            raise SourceLifecycleError("invalid or duplicate parent")
        await self._register(node_id, _Node(reference=ref, parents=tuple(sorted(parents)), policy=policy))

    async def _register(self, node_id: str, node: _Node) -> None:
        if not isinstance(node_id, str) or not node_id.strip():
            raise SourceLifecycleError("expected a nonblank node ID")
        scope = self.services.scope_id
        async with self.services.database.transaction() as connection:
            graph = await _load(connection, scope)
            if node_id in graph:
                existing = graph[node_id]
                if existing.model_copy(update={"available": True, "event": None}) != node:
                    raise SourceLifecycleError("conflicting node binding")
                return
            if any(parent not in graph for parent in node.parents):
                raise SourceLifecycleError("parent must already be registered")
            ref = node.reference
            if node.parents:
                node.available = node.supported(graph)
            try:
                if isinstance(ref, SourceRef):
                    await self.services.repositories.sources.get(connection, scope, ref)
                elif isinstance(ref, ArtifactRef):
                    await self.services.repositories.topic_memories.get_exact(connection, scope, ref)
                else:
                    await self._sync_memory(connection, ref, node.available)
            except (ValueError, LookupError) as exc:
                raise SourceLifecycleError("unreadable lifecycle binding") from exc
            first = not graph
            graph[node_id] = node
            statement = (
                insert(GRAPH).values(scope_id=scope) if first else update(GRAPH).where(GRAPH.c.scope_id == scope)
            )
            await connection.execute(statement.values(payload=_GRAPH.dump_json(graph)))

    async def _sync_memory(self, connection: AsyncConnection, ref: MemoryCitation, available: bool) -> None:
        _, sources = self.services.sources(connection)
        memory = self.services.memory(sources, connection)
        entry = await memory.validate_citation(ref)
        head = await memory.head(ref.memory_ref.artifact_id)
        change = memory.reactivate if available else memory.forget
        await change(head, entries=(entry,))

    async def apply(self, event: SourceLifecycleEvent) -> list[str]:
        scope = self.services.scope_id
        async with self.services.database.transaction() as connection:
            graph = await _load(connection, scope)
            root = graph.get(event.node_id)
            if root is None or not isinstance(root.reference, SourceRef) or root.revision != event.revision:
                raise SourceLifecycleError("unknown source or lifecycle revision")
            if event != root.event:
                if root.event is not None and event.sequence <= root.event.sequence:
                    raise SourceLifecycleError("stale or conflicting event")
                root.event = event
                root.available = event.available
                # Parents must be registered first. The serialized dictionary retains registration order.
                for node in graph.values():
                    if node.parents and node.available != node.supported(graph):
                        node.available = not node.available
                        if isinstance(node.reference, MemoryCitation):
                            await self._sync_memory(connection, node.reference, node.available)
                await connection.execute(
                    update(GRAPH).where(GRAPH.c.scope_id == scope).values(payload=_GRAPH.dump_json(graph))
                )
            return sorted(key for key, node in graph.items() if node.available)

    async def resolve(self, node_id: str, *, purpose: str) -> Reference:
        async with self.services.database.transaction() as connection:
            graph = await _load(connection, self.services.scope_id)
            node = graph.get(node_id)
            if node is None or purpose not in ("audit", "generation"):
                raise SourceLifecycleError("unknown node or purpose")
            if purpose == "generation" and not node.available:
                raise SourceLifecycleError("node unavailable for generation")
            return node.reference
