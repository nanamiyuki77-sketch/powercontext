"""Coalesce concurrent embedding requests on one event loop."""

from __future__ import annotations

import asyncio
import math
from collections import OrderedDict
from collections.abc import Iterable

from powercontext.builtin.inference.models import EmbeddingResult, EmbeddingVector, InferenceUsage
from powercontext.builtin.inference.protocols import EmbeddingModel


def _sum_reported(counts: Iterable[int | None]) -> int | None:
    reported = [count for count in counts if count is not None]
    return sum(reported) if reported else None


class CoalescingEmbeddingModel:
    def __init__(
        self, model: EmbeddingModel, *, max_batch_size: int = 64, coalesce_ms: float = 5.0, cache_size: int = 1024
    ) -> None:
        if type(max_batch_size) is not int or max_batch_size <= 0:
            raise ValueError("max_batch_size must be a positive integer")  # noqa: TRY003
        if type(cache_size) is not int or cache_size < 0:
            raise ValueError("cache_size must be a nonnegative integer")  # noqa: TRY003
        if not math.isfinite(coalesce_ms) or coalesce_ms < 0:
            raise ValueError("coalesce_ms must be finite and nonnegative")  # noqa: TRY003
        self.profile = model.profile
        self._model = model
        self._max_batch_size = max_batch_size
        self._delay = coalesce_ms / 1000
        self._cache_size = cache_size
        self._cache: OrderedDict[str, EmbeddingVector] = OrderedDict()
        self._open: tuple[list[str], asyncio.Task[EmbeddingResult]] | None = None
        self._inflight: dict[str, tuple[asyncio.Task[EmbeddingResult], int]] = {}

    def _reserve(self, text: str) -> tuple[asyncio.Task[EmbeddingResult], int]:
        if text in self._inflight:
            return self._inflight[text]
        if self._open is None:
            batch: list[str] = []
            task = asyncio.create_task(self._run(batch))
            self._open = batch, task
        batch, task = self._open
        slot = task, len(batch)
        self._inflight[text] = slot
        batch.append(text)
        if len(batch) == self._max_batch_size:
            self._open = None
        return slot

    async def _run(self, batch: list[str]) -> EmbeddingResult:
        try:
            await asyncio.sleep(self._delay)
            if self._open is not None and self._open[0] is batch:
                self._open = None
            result = await self._model.embed(tuple(batch))
            if len(result.vectors) != len(batch):
                raise ValueError("embedding vector count differs from batch size")  # noqa: TRY003
            if self._cache_size:
                for text, vector in zip(batch, result.vectors, strict=True):
                    self._cache[text] = vector
                    if len(self._cache) > self._cache_size:
                        self._cache.popitem(last=False)
            return result
        finally:
            for text in batch:
                del self._inflight[text]

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        vectors: dict[str, EmbeddingVector] = {}
        slots = {}
        for text in texts:
            if text in self._cache:
                self._cache.move_to_end(text)
                vectors[text] = self._cache[text]
            else:
                slots[text] = self._reserve(text)

        batches = tuple(dict.fromkeys(task for task, _ in slots.values()))
        # Gather retains and observes shared tasks even if the last caller leaves.
        combined = asyncio.gather(*batches)
        try:
            results = await asyncio.shield(combined)
        except asyncio.CancelledError:
            combined.add_done_callback(lambda future: None if future.cancelled() else future.exception())
            raise
        for text, (task, index) in slots.items():
            vectors[text] = task.result().vectors[index]
        return EmbeddingResult(
            vectors=tuple(vectors[text] for text in texts),
            usage=InferenceUsage(
                requests=sum(result.usage.requests for result in results),
                input_tokens=_sum_reported(result.usage.input_tokens for result in results),
                output_tokens=_sum_reported(result.usage.output_tokens for result in results),
            ),
        )
