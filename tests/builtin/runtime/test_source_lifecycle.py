"""Recovery of a withdrawal interrupted between two native Memory revisions."""

import asyncio
import sqlite3

import pytest
from sqlalchemy.exc import IntegrityError

from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import BuiltinConfig, CaptureSource, RememberMemoryRequest, open_builtin_runtime
from powercontext.builtin.runtime.source_lifecycle import SourceLifecycleError, SourceLifecycleEvent
from powercontext.builtin.scope import ScopeDraft


def test_interrupted_event_rolls_back_and_retries_after_restart(tmp_path):
    asyncio.run(_exercise_retry(tmp_path))


async def _exercise_retry(tmp_path):
    path = tmp_path / "lifecycle.db"
    config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{path}"))
    event = SourceLifecycleEvent(node_id="origin", revision=1, sequence=1, available=False, reason="withdrawn")
    async with open_builtin_runtime(config) as runtime:
        assert runtime.lifecycle is not None
        assert runtime.scopes is not None
        scopes = []
        for key in ("one", "two"):
            scope = await runtime.scopes.create(ScopeDraft(title=key, summary=key, idempotency_key=key))
            scopes.append(scope.scope_id)
            receipt = await runtime.sources.for_scope(scope.scope_id).capture(
                CaptureSource(source_id="origin", content=key, metadata={})
            )
            await runtime.lifecycle.for_scope(scope.scope_id).register_source("origin", receipt.source_ref, revision=1)
        service = runtime.lifecycle.for_scope(scopes[0])
        memory = runtime.memory.for_scope(scopes[0])
        for name in ("first", "second"):
            result = await memory.remember(RememberMemoryRequest(entries=(MemoryEntryInput(kind="fact", text=name),)))
            assert result.entry is not None
            await service.register_memory(name, result.entry.citation, parents=("origin",), policy="all")
        before = (await memory.list()).memory_ref
        assert before is not None
        with sqlite3.connect(path) as db:
            db.execute(f"""CREATE TRIGGER interrupt_write BEFORE INSERT ON pc_artifacts
                WHEN NEW.family = 'memory' AND NEW.revision = {before.revision + 2}
                BEGIN SELECT RAISE(ABORT, 'injected interruption'); END""")
        with pytest.raises(IntegrityError):
            await service.apply(event)
        assert (await memory.list()).memory_ref == before
        await service.resolve("first", purpose="generation")
        with sqlite3.connect(path) as db:
            db.execute("DROP TRIGGER interrupt_write")

    async with open_builtin_runtime(config) as runtime:
        assert runtime.lifecycle is not None
        service = runtime.lifecycle.for_scope(scopes[0])
        memory = runtime.memory.for_scope(scopes[0])
        assert await service.apply(event) == []
        after = (await memory.list(include_inactive=True)).memory_ref
        assert await service.apply(event) == []
        assert (await memory.list(include_inactive=True)).memory_ref == after
        with pytest.raises(SourceLifecycleError):
            await service.resolve("origin", purpose="generation")
        assert (
            await runtime.lifecycle.for_scope(scopes[1]).resolve("origin", purpose="generation") == receipt.source_ref
        )
