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

"""Real Runtime checkpoints retain progress across partial failure and restart."""

import asyncio
import json
from typing import Any

import pytest

from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    CommitConnectorCheckpoint,
    SubmitSourceObservation,
    open_builtin_contexts,
    open_builtin_runtime,
)
from powercontext.builtin.scope import ScopeDraft
from powercontext.errors import ConnectorSubmissionRejectedError
from powercontext.sources import (
    ConnectorBinding,
    SourceDefinitionRegistry,
    manifest_for_definition,
    project_source_for_transport,
)
from powercontext.sources.connectors import ConnectorLifecycle, ConnectorRunStatus
from powercontext.sources.pi_session import PI_EVENT_SOURCE_DEFINITION, PiSessionConnector


class RuntimeCheckpoints:
    def __init__(self, runtime):
        self.runtime = runtime

    async def load(self, binding):
        return (await self.runtime.ingestion.checkpoint(binding)).checkpoint

    async def save(self, binding, checkpoint, *, expected):
        await self.runtime.ingestion.commit(
            CommitConnectorCheckpoint(binding=binding, checkpoint=checkpoint, expected=expected)
        )


class RuntimeSink:
    def __init__(self, runtime, failure):
        self.runtime, self.failure = runtime, failure
        self.registry = SourceDefinitionRegistry((PI_EVENT_SOURCE_DEFINITION,))

    async def submit(self, binding, item_id, definition_name, value):
        if value.event_id == "tail" and self.failure:
            raise self.failure
        source = await self.registry.resolve(value)
        receipt = await self.runtime.ingestion.submit(
            SubmitSourceObservation(
                scope_id=binding.scope_id,
                observation=project_source_for_transport(self.registry, source),
            )
        )
        return receipt.source_ref


@pytest.mark.parametrize("reject", [False, True])
def test_partial_failure_restart_off_branch_mutation_and_repair(tmp_path, reject):
    path = tmp_path / "pi.jsonl"
    config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'runtime.db'}"))
    records: list[dict[str, Any]] = [
        {"type": "message", "id": "root", "parentId": None, "message": {"role": "user", "content": "shared"}},
        {"type": "message", "id": "left", "parentId": "root", "message": {"role": "assistant", "content": "left"}},
        {"type": "message", "id": "right", "parentId": "root", "message": {"role": "assistant", "content": "right"}},
        {
            "type": "message",
            "id": "tail",
            "parentId": "right",
            "message": {
                "role": "toolResult",
                "toolName": "read",
                "toolCallId": "call",
                "content": "完整 output\n" * 2000,
            },
        },
    ]

    def write(session_id="session"):
        header = {"type": "session", "version": 3, "id": session_id}
        path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in [header, *records]), encoding="utf-8")

    async def scenario():
        async with open_builtin_runtime(config) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(ScopeDraft(title="Pi", summary="Recovery", idempotency_key="pi"))
            await runtime.ingestion.register(manifest_for_definition(PI_EVENT_SOURCE_DEFINITION))
        binding = ConnectorBinding(
            scope_id=scope.scope_id, binding_id="pi", connector_name="pi-session-history", connector_version="1"
        )

        async def run(leaf, failure=None):
            async with open_builtin_runtime(config) as runtime:
                return await ConnectorLifecycle(
                    sink=RuntimeSink(runtime, failure), checkpoints=RuntimeCheckpoints(runtime)
                ).run(
                    PiSessionConnector(path, leaf),
                    binding,
                )

        async def checkpoint():
            async with open_builtin_runtime(config) as runtime:
                return (await runtime.ingestion.checkpoint(binding)).checkpoint

        async def entries():
            async with open_builtin_contexts(config) as contexts:
                return await (await contexts.get(scope.scope_id)).sources.journal.entries()

        write()
        first = await run("left")
        assert len(first.items) == 2
        records[1]["message"]["content"] = "rewritten unselected branch"
        write()
        with pytest.raises(ValueError):
            await run("tail")
        assert await checkpoint() == first.committed_checkpoint
        assert len(await entries()) == 2
        records[1]["message"]["content"] = "left"
        write()
        if reject:
            result = await run("tail", ConnectorSubmissionRejectedError("temporary rejection"))
            assert result.status is ConnectorRunStatus.INCOMPLETE
        else:
            with pytest.raises(OSError, match="temporary failure"):
                await run("tail", OSError("temporary failure"))
        assert await checkpoint() == first.committed_checkpoint
        assert len(await entries()) == 3
        recovered = await run("tail")
        assert len(recovered.items) == 2
        persisted = await entries()
        assert len(persisted) == 4
        assert persisted[-1].source.payload["record"] == records[-1]
        assert (
            persisted[-1].source.projections[0].value["content"]
            == "toolResult: [tool_result read call] " + records[-1]["message"]["content"]
        )
        assert (await run("left")).items == ()
        assert (await run("tail")).items == ()
        for corruption in ("bad_json", "different_session", "removed_record"):
            write()
            if corruption == "bad_json":
                path.write_bytes(path.read_bytes() + b"\n{broken}")
            elif corruption == "different_session":
                write("different")
            else:
                saved = records.pop(1)
                write()
                records.insert(1, saved)
            with pytest.raises(ValueError):
                await run("tail")
            assert await checkpoint() == recovered.committed_checkpoint
            assert await entries() == persisted
        write()
        assert (await run("tail")).items == ()

    asyncio.run(scenario())
