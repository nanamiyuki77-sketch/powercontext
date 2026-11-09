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

"""Complete physical PDF pages and offline retrieval through scoped Memory."""

from __future__ import annotations

import hashlib
import io
import json
import re
from collections import defaultdict
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field, field_validator

from powercontext.sources.definitions import SourceDefinition, SourceProjection
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

if TYPE_CHECKING:
    from powercontext.builtin.artifacts.memory.models import MemoryCitation
    from powercontext.builtin.runtime.application import ScopedMemoryApplication


class PDFDocumentInput(BaseModel):
    document_id: str
    content: bytes

    @field_validator("document_id")
    @classmethod
    def validate_document_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("PDF document ID must not be blank")  # noqa: TRY003
        return value


class PDFPageInput(BaseModel):
    document_id: str
    document_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_number: int = Field(ge=1)
    text: str

    @field_validator("document_id")
    @classmethod
    def validate_document_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("PDF document ID must not be blank")  # noqa: TRY003
        return value


class PDFPageSource(Source, PDFPageInput):
    """One complete page, bound to the original PDF byte revision."""


def parse_pdf_document(value: PDFDocumentInput) -> tuple[PDFPageInput, ...]:
    """Extract every physical page atomically, including pages without text."""
    from pypdf import PdfReader

    digest = hashlib.sha256(value.content).hexdigest()
    try:
        reader = PdfReader(io.BytesIO(value.content), strict=True)
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("PDF requires a password")  # noqa: TRY003, TRY301
        return tuple(
            PDFPageInput(
                document_id=value.document_id, document_sha256=digest, page_number=index, text=page.extract_text() or ""
            )
            for index, page in enumerate(reader.pages, start=1)
        )
    except Exception as error:
        # Decoder failures vary by stream/filter; the public boundary is always ValueError.
        raise ValueError("PDF could not be completely parsed") from error  # noqa: TRY003


class _PDFTextProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class: type[PDFPageSource] = PDFPageSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PDFPageSource, /) -> TextEvidence:
        return TextEvidence(
            source_type="pdf-page",
            source_id=source.name,
            content=f"{source.document_id}\n{source.text}",
            metadata={
                "document_id": source.document_id,
                "document_sha256": source.document_sha256,
                "page_number": source.page_number,
            },
        )


class _PDFPageDefinition:
    name = "pdf-page"
    version = "1"
    input_class: type[PDFPageInput] = PDFPageInput
    source_class: type[PDFPageSource] = PDFPageSource
    projections: tuple[SourceProjection[PDFPageSource], ...] = (_PDFTextProjection(),)

    async def resolve(self, value: PDFPageInput, /) -> PDFPageSource:
        identity = json.dumps(
            [value.document_id, value.document_sha256, value.page_number], ensure_ascii=False, separators=(",", ":")
        )
        return PDFPageSource(
            name=f"pdf:{hashlib.sha256(identity.encode()).hexdigest()}",
            materialization=SourceMaterialization.CAPTURED,
            **value.model_dump(),
        )

    async def read(self, source: PDFPageSource, /) -> PDFPageInput:
        return PDFPageInput.model_validate(source.model_dump())


PDF_PAGE_SOURCE_DEFINITION: SourceDefinition[PDFPageInput, PDFPageSource, PDFPageInput] = _PDFPageDefinition()

# General financial vocabulary, independent of any company, report, or benchmark answer.
_FINANCIAL_CONCEPTS = (
    (r"\bebitda\b", ("operating income", "depreciation amortization", "cash flows")),
    (r"\b(?:capex|capital expenditures?)\b", ("capital spending", "capital expenditures", "cash flows investing")),
    (r"\b(?:d\s*&\s*a|depreciation|amortization)\b", ("depreciation amortization",)),
    (r"\b(?:p\s*&\s*l|income statements?|earnings)\b", ("consolidated statements income", "statements operations")),
    (r"\b(?:cash flow|cash flows)\b", ("consolidated statements cash flows",)),
    (r"\b(?:balance sheets?|assets|liabilities)\b", ("consolidated balance sheets",)),
    (r"\b(?:revenue|revenues|sales)\b", ("net sales revenue",)),
    (
        r"\b(?:geograph\w*|countries|regions?|operates?)\b",
        (
            "geographic segments",
            "international operations",
        ),
    ),
    (
        r"\b(?:legal|litigation|lawsuits?|battles)\b",
        (
            "legal proceedings",
            "litigation contingencies",
        ),
    ),
    (
        r"\b(?:restructuring|reorganization)\b",
        (
            "restructuring charges",
            "consolidated statements income",
        ),
    ),
    (r"\b(?:dividends?|repurchases?|buybacks?)\b", ("dividends share repurchases",)),
    (r"\b(?:debt|borrowings?|leverage)\b", ("debt borrowings",)),
)


async def search_pdf_pages(
    memory: ScopedMemoryApplication,
    question: str,
    limit: int = 10,
) -> tuple[MemoryCitation, ...]:
    """Fuse bounded lexical searches and return distinct, real page citations."""
    from powercontext.builtin.artifacts.memory.models import MemoryCitation, MemoryHit
    from powercontext.builtin.artifacts.search import analyze_fts_query
    from powercontext.builtin.runtime.models import GetMemoryEntryRequest, SearchMemoryRequest

    if limit <= 0 or not question.strip():
        return ()
    normalized = re.sub(r"\bfy\s*(\d{4})\b", r"\1", question.lower())
    keywords = analyze_fts_query(normalized)
    queries = [keywords]
    for pattern, expansions in _FINANCIAL_CONCEPTS:
        if re.search(pattern, normalized):
            queries.extend(expansions)
    # Short query variants prevent long natural-language instructions from suppressing evidence.
    queries.extend(analyze_fts_query(part) for part in re.split(r"[?.;]", normalized) if part.strip())
    queries = list(dict.fromkeys(query for query in queries if query.strip()))
    scores: dict[str, float] = defaultdict(float)
    hits: dict[str, MemoryHit] = {}
    for query in queries:
        page = await memory.search(SearchMemoryRequest(query=query, mode="fts", limit=max(100, limit * 5)))
        for rank, hit in enumerate(page.hits, start=1):
            hits[hit.entry_version_id] = hit
            scores[hit.entry_version_id] += 1.0 / (60 + rank)
    question_terms = set(re.findall(r"[a-z]+|\d+", normalized))
    generic = {"annual", "report", "form", "k", "q", "the", "inc", "corporation", "company", "corp"}

    def score(identifier: str) -> float:
        document_id = hits[identifier].text.partition("\n")[0]
        document_terms = set(re.findall(r"[a-z]+|\d+", document_id.lower()))
        names = {term for term in document_terms - generic if term.isalpha()}
        # The document identifier is part of every page projection, not external metadata.
        company_match = bool(names & question_terms)
        year_match = bool({term for term in document_terms if len(term) == 4 and term.isdigit()} & question_terms)
        return scores[identifier] * (1 + 3 * company_match + year_match)

    citations = []
    seen: set[str] = set()
    for identifier in sorted(hits, key=lambda item: (-score(item), item)):
        hit = hits[identifier]
        citation = MemoryCitation(
            memory_ref=hit.memory_ref, entry_id=hit.entry_id, entry_version_id=hit.entry_version_id
        )
        entry = (await memory.get(GetMemoryEntryRequest(citation=citation))).entry
        pages = {source.source_id for source in entry.sources if source.source_type == "pdf-page"}
        if len(pages) != 1 or pages & seen:
            continue
        seen.update(pages)
        citations.append(citation)
        if len(citations) == limit:
            break
    return tuple(citations)
