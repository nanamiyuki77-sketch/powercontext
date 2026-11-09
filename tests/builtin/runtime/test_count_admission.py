"""Count discovery must page past quiet scopes and retain durable accepted work."""

import asyncio

import pytest

from powercontext.builtin.persistence.cursors import SourceCursorRepository
from powercontext.builtin.persistence.processing_intents import ArtifactProcessingIntentRepository
from powercontext.builtin.persistence.supervision import ArtifactProcessingLeaseRepository
from powercontext.builtin.runtime import BuiltinConfig, open_builtin_contexts
from powercontext.builtin.runtime.config import RuntimeConfig
from powercontext.builtin.runtime.processing_discovery import SourceProcessingPendingProvider
from powercontext.builtin.scope import ScopeDraft
from powercontext.builtin.sources import SourceCursor


@pytest.mark.parametrize("field", ["memory_source_count_threshold", "topic_memory_source_count_threshold"])
def test_count_configuration_is_strict_and_disabled_by_default(field):
    assert getattr(RuntimeConfig(), field) is None
    for value in (False, True, "2", 2.0, 0, -1):
        with pytest.raises(ValueError):
            RuntimeConfig.model_validate({field: value})
    assert getattr(RuntimeConfig.model_validate({field: 2}), field) == 2


def test_paged_count_admission_rechecks_successful_cursor():
    async def scenario():
        async with open_builtin_contexts(BuiltinConfig()) as contexts:
            scopes = sorted([
                (
                    await contexts.scopes.create(ScopeDraft(title=str(i), summary="count", idempotency_key=str(i)))
                ).scope_id
                for i in range(3)
            ])
            for index, scope in enumerate(scopes):
                for row in range(1 if index == 0 else 2):
                    await contexts.records.create_source(scope, "content", f"Record {row}")
            database = contexts.database
            intents = ArtifactProcessingIntentRepository()
            async with database.transaction() as connection:
                lease = await ArtifactProcessingLeaseRepository().start_single_process_term(connection, "count-test")
            fence = lease.fence("single-process")
            provider = SourceProcessingPendingProvider(database, "count-binding", "memory", 2)
            admitted = []
            for _ in range(4):
                admitted.extend(await provider.admit_count_ready(1, fence))
            assert admitted == scopes[1:]
            restarted = SourceProcessingPendingProvider(database, "count-binding", "memory", 2)
            assert await restarted.admit_count_ready(10, fence) == ()
            async with database.transaction() as connection:
                intent = await intents.load(connection, scopes[1], "count-binding")
                assert intent is not None and intent.requested_generation == 1
                await SourceCursorRepository().save(
                    connection, scopes[1], "count-binding", SourceCursor(sequence=1), expected_generation=None
                )
                await intents.acknowledge(connection, scopes[1], "count-binding", 1)
            assert await restarted.admit_count_ready(10, fence) == ()
            await contexts.records.create_source(scopes[1], "content", "New input after successful window")
            assert await restarted.admit_count_ready(10, fence) == (scopes[1],)
            async with database.transaction() as connection:
                intent = await intents.load(connection, scopes[1], "count-binding")
                assert intent is not None and intent.requested_generation == 2

    asyncio.run(scenario())
