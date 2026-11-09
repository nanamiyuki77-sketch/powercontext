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

"""Captured CSV evidence and exact citations survive independent runtimes."""

import asyncio

from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.artifacts.memory.models import MemoryCitation
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    GetMemoryEntryRequest,
    SearchMemoryRequest,
    SubmitSourceObservation,
    open_builtin_contexts,
    open_builtin_runtime,
)
from powercontext.builtin.scope import ScopeDraft
from powercontext.sources import SourceDefinitionRegistry, manifest_for_definition, project_source_for_transport
from powercontext.sources.csv_rows import CSV_ROW_SOURCE_DEFINITION, CSVDocumentInput, parse_csv_document
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY


def test_exact_citations_and_complete_rows_survive_two_restarts(tmp_path):
    config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'csv.db'}"))
    rows = parse_csv_document(
        CSVDocumentInput(
            document_id="证据:/%",
            primary_key=" id ",
            content=' id ,name,number,empty,note\r\n key ,needlealpha,00123,,"一\r\n二"\r\nkey ,needlebeta,false,,\r\n'.encode(),
        )
    )
    registry = SourceDefinitionRegistry((CSV_ROW_SOURCE_DEFINITION,))

    class RowPipeline:
        async def extract(self, request, /):
            return tuple(
                MemoryEntryInput(
                    kind="csv-row", text=source.projection(TEXT_EVIDENCE_PROJECTION_KEY)["content"], sources=(source,)
                )
                for source in request.sources
            )

    async def scenario():
        observations = [project_source_for_transport(registry, await registry.resolve(row)) for row in rows]
        expected = {source.name: source.projection(TEXT_EVIDENCE_PROJECTION_KEY) for source in observations}
        async with open_builtin_runtime(config) as runtime:
            scope = await runtime.scopes.create(ScopeDraft(title="CSV", summary="Exact rows", idempotency_key="csv"))
            await runtime.ingestion.register(manifest_for_definition(CSV_ROW_SOURCE_DEFINITION))
            for source in observations:
                await runtime.ingestion.submit(SubmitSourceObservation(scope_id=scope.scope_id, observation=source))

        citations = []
        async with open_builtin_runtime(config, candidate_pipeline=RowPipeline()) as runtime:
            memory = runtime.memory.for_scope(scope.scope_id)
            assert (await memory.flush()).source_count == len(rows)
            for query, observation in zip(("needlealpha", "needlebeta"), observations, strict=True):
                results = await memory.search(SearchMemoryRequest(query=query, mode="fts", limit=10))
                assert len(results.hits) == 1
                hit = results.hits[0]
                citation = MemoryCitation(
                    memory_ref=hit.memory_ref, entry_id=hit.entry_id, entry_version_id=hit.entry_version_id
                )
                citations.append((citation, observation.name))

        async with open_builtin_runtime(config) as runtime:
            memory = runtime.memory.for_scope(scope.scope_id)
            for citation, identity in citations:
                result = await memory.get(GetMemoryEntryRequest(citation=citation))
                assert result.entry.text == expected[identity]["content"]
                assert [source.source_id for source in result.entry.sources] == [identity]

        async with open_builtin_contexts(config) as contexts:
            entries = await (await contexts.get(scope.scope_id)).sources.journal.entries()
            assert {
                entry.source_ref.source_id: entry.source.projection(TEXT_EVIDENCE_PROJECTION_KEY) for entry in entries
            } == expected

    asyncio.run(scenario())
