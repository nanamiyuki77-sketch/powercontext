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

"""Rank literal passages using query overlap and the surrounding document."""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from powercontext.builtin.artifacts.search import analyze_text

_BOUNDARY = re.compile(r"(?<=[.!?])\s+|(?<=[。！？])\s*|\n+")  # noqa: RUF001
_STOP = frozenset([
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "been",
    "by",
    "can",
    "for",
    "from",
    "had",
    "has",
    "have",
    "in",
    "into",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "their",
    "these",
    "this",
    "to",
    "was",
    "were",
    "with",
])


@dataclass(frozen=True)
class RankedPassage:
    document: int
    start: int
    end: int


def _terms(text: str) -> set[str]:
    # Short stems also match common inflections in technical vocabulary.
    return {
        (term.removesuffix("s")[:6] if term.isascii() and term.isalpha() and len(term) > 3 else term)
        for term in analyze_text(text).split()
        if term not in _STOP
    }


def _spans(text: str) -> Iterator[tuple[int, int]]:
    start = 0
    for match in _BOUNDARY.finditer(text):
        if start < match.start():
            yield start, match.start()
        start = match.end()
    if start < len(text):
        yield start, len(text)


def _passages(text: str) -> Iterator[tuple[int, int]]:
    for start, end in _spans(text):
        # An unpunctuated paragraph still needs independently selectable evidence near its end.
        while end - start > 480:
            split = text.rfind(" ", start + 160, start + 320)
            cut = start + 240 if split < 0 else split
            yield start, cut
            start = cut + (split >= 0)
        if start < end:
            yield start, end


def rank_passages(query: str, documents: Sequence[str]) -> list[RankedPassage]:
    wanted = _terms(query)
    passages = []
    frequency: Counter[str] = Counter()
    for index, text in enumerate(documents):
        seen = set()
        for start, end in _passages(text):
            sentence = text[start:end]
            if not sentence.strip() or sentence in seen:
                continue
            seen.add(sentence)
            terms = _terms(sentence)
            matches = wanted & terms
            frequency.update(matches)
            passages.append((RankedPassage(index, start, end), matches, len(terms)))

    weights = {term: 1 + math.log((1 + len(passages)) / (1 + count)) for term, count in frequency.items()}
    scores = []
    peaks = [0.0] * len(documents)
    for span, matches, length in passages:
        overlap = sum(weights[term] for term in sorted(matches))
        position = span.end / len(documents[span.document])
        # Results and conclusions later in a document often elaborate its opening summary.
        score = overlap * (1 + 0.35 * position) / (1 + 0.008 * length)
        scores.append(score)
        peaks[span.document] = max(peaks[span.document], score)

    def relevance(index: int) -> float:
        span = passages[index][0]
        position = span.end / len(documents[span.document])
        return scores[index] + peaks[span.document] * (0.35 + 0.15 * position)

    return [
        passages[i][0]
        for i in sorted(range(len(passages)), key=relevance, reverse=True)
        if peaks[passages[i][0].document]
    ]


def matched_excerpt(query: str, text: str, passage: RankedPassage, width: int) -> RankedPassage | None:
    """Center a short literal excerpt on a query term using original character offsets."""
    terms = sorted(set(re.findall(r"\w+", query)) - _STOP, key=lambda term: (-len(term), term))
    for term in terms:
        if len(term) > width:
            continue
        match = re.search(re.escape(term), text[passage.start : passage.end], flags=re.IGNORECASE)
        if match is not None:
            center = passage.start + (match.start() + match.end()) // 2
            start = max(passage.start, min(center - width // 2, passage.end - width))
            return RankedPassage(passage.document, start, min(start + width, passage.end))
    return None


def packing_options(
    selected: Sequence[RankedPassage], passage: RankedPassage, max_bytes: int
) -> Iterator[list[RankedPassage]]:
    """Offer a separate citation or a continuous quotation sharing an existing citation."""
    same_document = [span for span in selected if span.document == passage.document]
    if any(span.start <= passage.start and passage.end <= span.end for span in same_document):
        return
    yield [*selected, passage]
    for span in same_document:
        start, end = min(span.start, passage.start), max(span.end, passage.end)
        if end - start <= max_bytes:
            yield [
                *(
                    item
                    for item in selected
                    if item.document != passage.document or item.end <= start or item.start >= end
                ),
                RankedPassage(passage.document, start, end),
            ]
