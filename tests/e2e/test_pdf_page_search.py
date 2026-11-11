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

"""Offline scoped page retrieval returns durable, deduplicated citations."""

import asyncio

import pytest

from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    GetMemoryEntryRequest,
    SubmitSourceObservation,
    open_builtin_runtime,
)
from powercontext.builtin.scope import ScopeDraft
from powercontext.sources import (
    SourceDefinitionRegistry,
    TextEvidence,
    manifest_for_definition,
    project_source_for_transport,
)
from powercontext.sources.pdf_pages import PDF_PAGE_SOURCE_DEFINITION, PDFPageInput, search_pdf_pages
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY


class PagePipeline:
    async def extract(self, request, /):
        return tuple(
            MemoryEntryInput(
                kind=kind, text=source.projection(TEXT_EVIDENCE_PROJECTION_KEY)["content"], sources=(source,)
            )
            for source in request.sources
            for kind in ("page", "duplicate")
        )


def test_scope_deduplication_synonyms_limits_and_revision_citations(tmp_path):
    config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'pdf.db'}"))
    registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
    pages = [
        PDFPageInput(
            document_id="ATLAS_2025_10K",
            document_sha256="a" * 64,
            page_number=1,
            text="Consolidated statements of income. Net sales revenue 1500. Operating income 300."
            + " Financial statement disclosure." * 300,
        ),
        PDFPageInput(
            document_id="ATLAS_2025_10K",
            document_sha256="a" * 64,
            page_number=2,
            text="Consolidated statements of cash flows. Depreciation amortization 25. Capital expenditures 40.",
        ),
        PDFPageInput(
            document_id="ATLAS_2025_10K",
            document_sha256="a" * 64,
            page_number=3,
            text="Legal proceedings. Litigation contingencies and pending claims.",
        ),
        PDFPageInput(
            document_id="OTHER_2025_10K",
            document_sha256="b" * 64,
            page_number=1,
            text="Net sales revenue 9999. Sales revenue increased.",
        ),
    ]

    async def scenario():
        observations = [project_source_for_transport(registry, await registry.resolve(page)) for page in pages]
        async with open_builtin_runtime(config, candidate_pipeline=PagePipeline()) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(ScopeDraft(title="Reports", summary="PDF", idempotency_key="reports"))
            other = await runtime.scopes.create(ScopeDraft(title="Other", summary="PDF", idempotency_key="other"))
            await runtime.ingestion.register(manifest_for_definition(PDF_PAGE_SOURCE_DEFINITION))
            for observation in observations:
                await runtime.ingestion.submit(
                    SubmitSourceObservation(scope_id=scope.scope_id, observation=observation)
                )
            await runtime.ingestion.submit(
                SubmitSourceObservation(scope_id=scope.scope_id, observation=observations[0])
            )
            memory = runtime.memory.for_scope(scope.scope_id)
            assert (await memory.flush()).source_count == len(pages)

        async with open_builtin_runtime(config) as runtime:
            memory = runtime.memory.for_scope(scope.scope_id)
            assert await search_pdf_pages(memory, " ") == ()
            assert await search_pdf_pages(memory, "sales", limit=0) == ()
            assert await search_pdf_pages(runtime.memory.for_scope(other.scope_id), "sales") == ()
            by_id = {source.name: source for source in observations}
            for question, target in [
                ("What is Atlas revenue in FY2025?", observations[0].name),
                ("Atlas capex FY2025", observations[1].name),
                ("Atlas ongoing lawsuits FY2025", observations[2].name),
            ]:
                citations = await search_pdf_pages(memory, question, limit=3)
                assert citations and len(citations) <= 3
                found = []
                for citation in citations:
                    entry = (await memory.get(GetMemoryEntryRequest(citation=citation))).entry
                    assert len(entry.sources) == 1
                    identity = entry.sources[0].source_id
                    evidence = TextEvidence.model_validate(by_id[identity].projection(TEXT_EVIDENCE_PROJECTION_KEY))
                    assert entry.text == evidence.content
                    found.append(identity)
                assert observations[3].name not in found
                assert target in found
                assert len(found) == len(set(found))
            citations = await search_pdf_pages(memory, "Atlas revenue FY2025", limit=1)
            assert len(citations) == 1
            original = (await memory.get(GetMemoryEntryRequest(citation=citations[0]))).entry
            assert original.sources[0].source_id == observations[0].name

        async with open_builtin_runtime(config, candidate_pipeline=PagePipeline()) as runtime:
            revised = pages[0].model_copy(update={"document_sha256": "c" * 64, "text": "Net sales revenue 1600."})
            observation = project_source_for_transport(registry, await registry.resolve(revised))
            assert observation.name != observations[0].name
            await runtime.ingestion.submit(SubmitSourceObservation(scope_id=scope.scope_id, observation=observation))
            memory = runtime.memory.for_scope(scope.scope_id)
            assert (await memory.flush()).source_count == 1
            assert (await memory.get(GetMemoryEntryRequest(citation=citations[0]))).entry == original

    asyncio.run(scenario())


@pytest.mark.parametrize("title", ["Consolidated Statement of Income", "Consolidated Statements of Income"])
def test_statement_evidence_survives_many_partial_matching_notes(tmp_path, title):
    config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'ranking.db'}"))
    registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
    texts = [
        f"{title}\nNet revenue 1200. Operating income 300. Net income 180.",
        "Consolidated Statements of Cash Flows\nDepreciation amortization 50. Capital expenditures 80.",
        *[
            f"Note {number}. Income taxes and restructuring charges. "
            "Consolidated income tax expense. Capital spending expenditures. "
            "Operating income changes. Depreciation amortization policies. Cash flows investing."
            for number in range(30)
        ],
    ]

    async def scenario():
        observations = [
            project_source_for_transport(
                registry,
                await registry.resolve(
                    PDFPageInput(document_id="ATLAS_2025_10K", document_sha256="a" * 64, page_number=number, text=text)
                ),
            )
            for number, text in enumerate(texts, start=1)
        ]
        async with open_builtin_runtime(config, candidate_pipeline=PagePipeline()) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(ScopeDraft(title="Reports", summary="PDF", idempotency_key="reports"))
            await runtime.ingestion.register(manifest_for_definition(PDF_PAGE_SOURCE_DEFINITION))
            for observation in observations:
                await runtime.ingestion.submit(
                    SubmitSourceObservation(scope_id=scope.scope_id, observation=observation)
                )
            await runtime.memory.for_scope(scope.scope_id).flush()
        async with open_builtin_runtime(config) as runtime:
            citations = await search_pdf_pages(
                runtime.memory.for_scope(scope.scope_id),
                "Calculate Atlas EBITDA less capex in FY2025 using the income statement and cash flows.",
                limit=20,
            )
            entries = [
                (await runtime.memory.for_scope(scope.scope_id).get(GetMemoryEntryRequest(citation=citation))).entry
                for citation in citations
            ]
            identities = [entry.sources[0].source_id for entry in entries]
            assert {observations[0].name, observations[1].name} <= set(identities)
            assert len(identities) == len(set(identities)) <= 20

    asyncio.run(scenario())
