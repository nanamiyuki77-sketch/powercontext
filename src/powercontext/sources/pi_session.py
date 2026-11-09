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

"""Capture complete Pi v3 branch evidence with durable lifecycle checkpoints."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from powercontext.sources.connectors import ConnectorRunCompletion, ConnectorRunSession, ConnectorRunStatus
from powercontext.sources.definitions import SourceDefinition, SourceProjection
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence


class PiSessionInput(BaseModel):
    content: bytes
    leaf_id: str | None = None


class PiEventInput(BaseModel):
    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]


class PiEventSource(Source, PiEventInput):
    """The complete original record, independent of Memory text limits."""


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _identity(session_id: str, event_id: str) -> str:
    if not session_id or not event_id:
        raise ValueError("Pi session and event IDs must be non-empty")  # noqa: TRY003
    return f"pi:{_digest([session_id, event_id])}"


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Pi visible text must be a string")  # noqa: TRY003, TRY004
    return value


def _blocks(value: object, *, tool_calls: bool = False) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        raise ValueError("Pi content must be a string or an array of blocks")  # noqa: TRY003, TRY004
    parts = []
    for block in value:
        if not isinstance(block, dict):
            raise ValueError("Pi content blocks must be objects")  # noqa: TRY003, TRY004
        if block.get("type") == "text":
            parts.append(_text(block.get("text")))
        elif tool_calls and block.get("type") == "toolCall":
            name, identifier = _text(block.get("name")), _text(block.get("id"))
            arguments = json.dumps(
                block.get("arguments", {}), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            parts.append(f"[tool_call {name} {identifier}] {arguments}")
    return "\n".join(parts)


def _event(session_id: str, record: dict[str, Any]) -> PiEventInput | None:
    event_type = record["type"]
    if event_type == "message":
        message = record.get("message")
        if not isinstance(message, dict):
            raise ValueError("Pi message must be an object")  # noqa: TRY003
        role = _text(message.get("role"))
        if role == "bashExecution":
            command, output = _text(message.get("command", "")), _text(message.get("output", ""))
            body = f"$ {command}\n{output}" if command or output else ""
        else:
            body = _blocks(message.get("content", []), tool_calls=True)
            if role == "toolResult" and body:
                name, identifier = _text(message.get("toolName", "")), _text(message.get("toolCallId", ""))
                body = f"[tool_result {name} {identifier}] {body}"
    elif event_type in {"compaction", "branch_summary"}:
        role, body = event_type, _text(record.get("summary", ""))
    elif event_type == "custom_message":
        role, body = "custom", _blocks(record.get("content", []))
    else:
        return None
    if not body:
        return None
    return PiEventInput(
        session_id=session_id,
        event_id=record["id"],
        parent_id=record["parentId"],
        event_type=event_type,
        role=role,
        content=f"{role}: {body}",
        record=record,
    )


def _invalid_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant: {value}")  # noqa: TRY003


def _parse(value: PiSessionInput) -> tuple[str, dict[str, dict[str, Any]], tuple[PiEventInput, ...]]:
    records = [
        json.loads(line, parse_constant=_invalid_constant)
        for line in value.content.decode("utf-8-sig").split("\n")
        if line.strip()
    ]
    if not records or not isinstance(records[0], dict):
        raise ValueError("Pi input requires a session header")  # noqa: TRY003
    header = records[0]
    session_id = header.get("id")
    if (
        header.get("type") != "session"
        or header.get("version") != 3
        or not isinstance(session_id, str)
        or not session_id
    ):
        raise ValueError("Pi input requires a version 3 session with a non-empty ID")  # noqa: TRY003
    nodes: dict[str, dict[str, Any]] = {}
    for record in records[1:]:
        if not isinstance(record, dict):
            raise ValueError("Pi records must be objects")  # noqa: TRY003, TRY004
        identifier, parent = record.get("id"), record.get("parentId")
        if not isinstance(identifier, str) or not identifier or identifier in nodes:
            raise ValueError("Pi event IDs must be non-empty and unique")  # noqa: TRY003
        if "parentId" not in record or (parent is not None and (not isinstance(parent, str) or parent not in nodes)):
            raise ValueError("Pi parents must be null or reference an earlier node")  # noqa: TRY003
        if not isinstance(record.get("type"), str) or not record["type"]:
            raise ValueError("Pi records require an event type")  # noqa: TRY003
        nodes[identifier] = record
    leaf = value.leaf_id if value.leaf_id is not None else next(reversed(nodes), None)
    if leaf is not None and leaf not in nodes:
        raise ValueError("Pi leaf does not exist")  # noqa: TRY003
    chain = []
    while leaf is not None:
        record = nodes[leaf]
        chain.append(record)
        leaf = record["parentId"]
    events = tuple(event for record in reversed(chain) if (event := _event(session_id, record)) is not None)
    return session_id, nodes, events


def parse_pi_session(value: PiSessionInput) -> tuple[PiEventInput, ...]:
    """Return all visible ancestors, without applying model-context compaction."""
    return _parse(value)[2]


class _PiTextProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class: type[PiEventSource] = PiEventSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PiEventSource, /) -> TextEvidence:
        return TextEvidence(
            source_type="pi-event",
            source_id=source.name,
            content=source.content,
            metadata={
                "session_id": source.session_id,
                "event_id": source.event_id,
                "parent_id": source.parent_id,
                "event_type": source.event_type,
                "role": source.role,
            },
        )


class _PiEventDefinition:
    name = "pi-event"
    version = "1"
    input_class: type[PiEventInput] = PiEventInput
    source_class: type[PiEventSource] = PiEventSource
    projections: tuple[SourceProjection[PiEventSource], ...] = (_PiTextProjection(),)

    async def resolve(self, value: PiEventInput, /) -> PiEventSource:
        return PiEventSource(
            name=_identity(value.session_id, value.event_id),
            materialization=SourceMaterialization.CAPTURED,
            **value.model_dump(),
        )

    async def read(self, source: PiEventSource, /) -> PiEventInput:
        return PiEventInput.model_validate(source.model_dump())


PI_EVENT_SOURCE_DEFINITION: SourceDefinition[PiEventInput, PiEventSource, PiEventInput] = _PiEventDefinition()


class _Checkpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    session_id: str
    imported: dict[str, str] = Field(default_factory=dict)


class PiSessionConnector:
    """Submit new branch events; the lifecycle owns the durable checkpoint."""

    name = "pi-session-history"
    version = "1"
    source_definitions = frozenset({"pi-event"})

    def __init__(self, path: str | Path, leaf_id: str | None = None) -> None:
        self.path = Path(path)
        self.leaf_id = leaf_id

    async def run(self, session: ConnectorRunSession, /) -> ConnectorRunCompletion:
        content = await asyncio.to_thread(self.path.read_bytes)
        session_id, nodes, events = _parse(PiSessionInput(content=content, leaf_id=self.leaf_id))
        previous = (
            _Checkpoint(session_id=session_id)
            if session.checkpoint is None
            else _Checkpoint.model_validate(session.checkpoint)
        )
        if previous.session_id != session_id:
            raise ValueError("Pi checkpoint belongs to another session")  # noqa: TRY003
        # Check every imported branch before submitting anything from the selected branch.
        for identifier, fingerprint in previous.imported.items():
            if identifier not in nodes or _digest(nodes[identifier]) != fingerprint:
                raise ValueError("Previously imported Pi record was changed or removed")  # noqa: TRY003
        imported = dict(previous.imported)
        for event in events:
            if event.event_id not in imported:
                await session.submit(_identity(session_id, event.event_id), "pi-event", event)
                imported[event.event_id] = _digest(event.record)
        checkpoint = _Checkpoint(session_id=session_id, imported=imported)
        return ConnectorRunCompletion(status=ConnectorRunStatus.COMPLETE, checkpoint=checkpoint.model_dump(mode="json"))
