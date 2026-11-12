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
import math
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from functools import cache
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field, field_validator

from powercontext.sources.definitions import SourceDefinition, SourceProjection
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.observations import SourceObservation
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

if TYPE_CHECKING:
    from powercontext.builtin.artifacts.memory.models import MemoryCitation
    from powercontext.builtin.runtime.application import ScopedMemoryApplication
    from powercontext.builtin.runtime.models import MemoryEntryRecord


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
    (r"\b(?:p\s*&\s*l|income statements?|earnings)\b", ("consolidated statement income", "statement operations")),
    (r"\b(?:cash flow|cash flows)\b", ("consolidated statement cash flows",)),
    (r"\b(?:balance sheets?|assets|liabilities)\b", ("consolidated balance sheets",)),
    (r"\b(?:revenue|revenues|sales)\b", ("net sales revenue",)),
    (
        r"\b(?:geograph\w*|countries|regions?|operates?)\b",
        (
            "geographic segments",
            "international operations",
            "reportable segments",
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
            "consolidated statement income",
        ),
    ),
    (r"\b(?:dividends?|repurchases?|buybacks?)\b", ("dividends share repurchases",)),
    (r"\b(?:debt|borrowings?|leverage)\b", ("debt borrowings",)),
)


# Use statement titles as evidence for questions that ask for accounting inputs
# without naming the statement. A title must occupy a line, not just occur in prose.
_STATEMENTS = (
    (
        r"cash flows?|capex|capital (?:spending|expenditures?)|depreciation|amortization|cash (?:used|provided)|ebitda",
        r"statements? of cash flows?",
    ),
    (
        r"ebitda|income statement|p\s*&\s*l|earnings|profit|net income|revenue|gross margin|per share",
        r"statements? of (?:income|earnings|operations|profit (?:and|or) loss)",
    ),
    (
        r"balance sheet|assets?|liabilit\w*|working capital|current ratio|quick ratio|inventory|receivables|payables",
        r"(?:balance sheets?|statements? of financial (?:position|condition))",
    ),
    (
        r"(?:shareholders?|stockholders?)['\u2019]? equity|changes in equity|movements in .*equity",
        r"statements? of (?:changes in )?(?:(?:shareholders?|stockholders?)['\u2019]? )?equity",
    ),
)


def _statement_pages(records: Sequence[MemoryEntryRecord], question: str) -> list[str]:
    selected = []
    for cue, title in _STATEMENTS:
        if not re.search(cue, question):
            continue
        pattern = re.compile(r"^(?:consolidated |combined |condensed )*" + title + r"(?: \(continued\))?[.:]?$", re.I)
        matches = []
        for record in records:
            body = record.entry.text.partition("\n")[2]
            if any(pattern.fullmatch(line.strip()) for line in body.splitlines()[:10]):
                # Numerical statements have consecutive comparative figures and independently readable line items.
                rows = sum(bool(re.search(r"\d[\d,.]*\s+[$(]?\d", line)) for line in body.splitlines())
                matches.append((rows, record.entry.entry_version_id))
        selected.extend(identifier for _, identifier in sorted(matches, reverse=True)[:2])
    return selected


def _retrieval_terms(text: str) -> tuple[str, ...]:
    from powercontext.builtin.artifacts.search import analyze_fts_query

    # Match common inflections locally without changing captured evidence text.
    terms = analyze_fts_query(text).split()
    return tuple(
        term[:-3] + "y"
        if term.endswith("ies")
        else term[:-1]
        if len(term) > 3 and term.endswith("s") and not term.endswith("ss")
        else term
        for term in terms
    )


def _pdf_queries(normalized: str) -> tuple[list[str], list[str]]:
    from powercontext.builtin.artifacts.search import analyze_fts_query

    keywords = analyze_fts_query(normalized)
    queries = [keywords]
    facets: list[str] = []
    for pattern, expansions in _FINANCIAL_CONCEPTS:
        if re.search(pattern, normalized):
            queries.extend(expansions)
            facets.extend(expansions)
    # Short query variants prevent long natural-language instructions from suppressing evidence.
    queries.extend(analyze_fts_query(part) for part in re.split(r"[?.;]", normalized) if part.strip())
    return list(dict.fromkeys(query for query in queries if query.strip())), facets


def _page_relevance(body: tuple[str, ...], heading: tuple[str, ...], query: str) -> float:
    terms = _retrieval_terms(query)
    distinct = set(terms)
    if not distinct:
        return 0.0
    coverage = len(distinct.intersection(body)) / len(distinct)
    phrase = " " + " ".join(terms) + " "
    exact = len(terms) > 1 and phrase in " " + " ".join(body) + " "
    title = len(terms) > 1 and phrase in " " + " ".join(heading) + " "
    return coverage + 2 * exact + 5 * title


def _lexical_pages(
    records: dict[str, MemoryEntryRecord],
    terms: Callable[[str], tuple[tuple[str, ...], tuple[str, ...]]],
    question: str,
) -> list[str]:
    """Give rare question terms a route into the pool even when FTS admission drops a page."""
    wanted = set(_retrieval_terms(question))
    counts = {}
    frequency: Counter[str] = Counter()
    for key in records:
        body, heading = terms(key)
        matches = Counter(word for word in (*body, *heading) if word in wanted)
        frequency.update(matches.keys())
        counts[key] = matches
    idf = {word: math.log1p((len(records) - df + 0.5) / (df + 0.5)) for word, df in frequency.items()}
    scores = {
        key: sum(idf[word] * count / (count + 1.2) for word, count in matches.items())
        for key, matches in counts.items()
        if matches
    }
    return sorted(scores, key=lambda key: (-scores[key], key))


def _named_reports(document_ids: set[str], question: str) -> set[str]:
    # Report identifiers commonly join issuer words and append a fiscal year and filing type.
    ignored = {"inc", "corp", "corporation", "company", "form", "annual", "report", "fy", "10k", "10q", "k", "q"}
    tokens = re.findall(r"[a-z0-9]+", question.lower())
    phrases = {"".join(tokens[i:j]) for i in range(len(tokens)) for j in range(i + 1, min(i + 8, len(tokens)) + 1)}
    named = set()
    for doc in document_ids:
        words = re.findall(r"[a-z0-9]+", doc.lower())
        issuer = "".join(w for w in words if w not in ignored and not w.isdigit())
        if issuer and issuer in phrases:
            named.add(doc)
    return named


async def _page_pool(memory: ScopedMemoryApplication, normalized: str):
    entries = (await memory.list()).entries
    page_sources = {}
    documents = set()
    for record in entries:
        sources = {ref.source_id for ref in record.entry.sources if ref.source_type == "pdf-page"}
        if len(sources) == 1:
            page_sources[record.entry.entry_version_id] = sources.pop()
            documents.add(record.entry.text.partition("\n")[0])
    named = _named_reports(documents, normalized)
    restrict = bool(named and named != documents)
    records = [
        record
        for record in entries
        if record.entry.entry_version_id in page_sources
        and (not restrict or record.entry.text.partition("\n")[0] in named)
    ]
    return page_sources, named if restrict else set(), len(entries) if restrict else 0, records


async def search_pdf_pages(
    memory: ScopedMemoryApplication,
    question: str,
    limit: int = 10,
) -> tuple[MemoryCitation, ...]:
    """Fuse bounded lexical searches and return distinct, real page citations."""
    from powercontext.builtin.artifacts.memory.models import MemoryHit
    from powercontext.builtin.runtime.models import SearchMemoryRequest

    if limit <= 0 or not question.strip():
        return ()
    normalized = re.sub(r"\bfy\s*(\d{4})\b", r"\1", question.lower())
    queries, facets = _pdf_queries(normalized)
    scores: dict[str, float] = defaultdict(float)
    hits: dict[str, MemoryHit] = {}
    rankings: dict[str, list[str]] = {}

    @cache
    def page_terms(identifier: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
        body = by_id[identifier].entry.text.partition("\n")[2]
        return _retrieval_terms(body), _retrieval_terms(" ".join(body.split()[:40]))

    def relevance(identifier: str, query: str) -> float:
        body, heading = page_terms(identifier)
        return _page_relevance(body, heading, query)

    page_sources, named, report_depth, records = await _page_pool(memory, normalized)
    by_id = {record.entry.entry_version_id: record for record in records}
    depth = max(100, limit * 5, report_depth)
    for query in queries:
        page = await memory.search(SearchMemoryRequest(query=query, mode="fts", limit=depth))
        eligible = [
            hit
            for hit in page.hits
            if hit.entry_version_id in page_sources and (not named or hit.text.partition("\n")[0] in named)
        ]
        hits.update((hit.entry_version_id, hit) for hit in eligible)
        # OR matches are candidates, not equally strong evidence for a phrase or statement title.
        ranked = sorted(enumerate(eligible), key=lambda pair: (-relevance(pair[1].entry_version_id, query), pair[0]))
        rankings[query] = [hit.entry_version_id for _, hit in ranked]
        for rank, identifier in enumerate(rankings[query], start=1):
            scores[identifier] += 1.0 / (60 + rank)
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
    # Reserve coverage for explicit evidence facets before generic mentions can crowd them out.
    preferred = list(
        dict.fromkeys(
            identifier
            for facet in dict.fromkeys(facets)
            for identifier in sorted(rankings[facet], key=lambda item: (-relevance(item, facet), -score(item), item))[
                :2
            ]
            if relevance(identifier, facet) >= 3
        )
    )
    preferred.sort(key=lambda item: (-score(item), item))
    ordered = list(dict.fromkeys([*preferred[: limit // 2], *sorted(hits, key=lambda item: (-score(item), item))]))
    lexical = _lexical_pages(by_id, page_terms, normalized)[: limit // 5]
    statements = _statement_pages(records, normalized)
    ordered = list(dict.fromkeys([*statements, *ordered[: max(1, limit // 4)], *lexical, *ordered]))
    neighbors = await _continuations(memory, page_sources, by_id, ordered[: min(6, limit // 2)], limit)
    ordered = list(dict.fromkeys([*ordered[: limit // 2], *neighbors[: limit // 4], *ordered]))
    for identifier in ordered:
        source_id = page_sources[identifier]
        if source_id in seen:
            continue
        seen.add(source_id)
        citations.append(by_id[identifier].citation)
        if len(citations) == limit:
            break
    return tuple(citations)


async def _continuations(
    memory: ScopedMemoryApplication,
    page_sources: dict[str, str],
    by_id: dict[str, MemoryEntryRecord],
    anchors: list[str],
    limit: int,
) -> list[str]:
    """Include the adjacent physical page only when its text continues the evidence."""
    if limit < 5 or not anchors:
        return []
    location = {}
    for entry in await memory.source_journal():
        if entry.source_ref.source_type == "pdf-page" and isinstance(entry.source, SourceObservation):
            page = PDFPageInput.model_validate(entry.source.payload)
            location[entry.source_ref.source_id] = (page.document_id, page.document_sha256, page.page_number)
    pages = {location[src]: key for key, src in page_sources.items() if src in location and key in by_id}
    results = []
    for key in anchors:
        pos = location.get(page_sources[key])
        if pos is None:
            continue
        doc, digest, number = pos
        for offset in (1, -1):
            neighbor = pages.get((doc, digest, number + offset))
            if neighbor is None:
                continue
            later = by_id[neighbor if offset == 1 else key].entry.text.partition("\n")[2]
            lead = re.sub(r"^\s*Table of Contents\s*", "", later, flags=re.I).lstrip()
            if re.match(r"(?:[a-z]|\d+[.)]\s)", lead) or "(continued)" in lead[:120].lower():
                results.append(neighbor)
    return results
