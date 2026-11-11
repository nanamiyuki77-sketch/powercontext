"""Late joiners, independent batches and per-caller usage."""

import asyncio

import pytest

from powercontext.builtin.artifacts.memory import EmbeddingProfile
from powercontext.builtin.inference import EmbeddingResult, InferenceUsage
from powercontext.builtin.inference.coalescing import CoalescingEmbeddingModel


class _Provider:
    profile = EmbeddingProfile(profile_id="test", model="deterministic", dimension=1)

    def __init__(self):
        self.calls = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def embed(self, texts):
        self.calls.append(texts)
        if "slow" in texts:
            self.started.set()
            await self.release.wait()
        if "fail" in texts and self.calls.count(texts) == 1:
            raise RuntimeError("offline")
        return EmbeddingResult(
            vectors=tuple((float(len(text)),) for text in texts),
            usage=InferenceUsage(requests=3, input_tokens=len(texts), output_tokens=0),
        )


def test_late_waiter_joins_inflight_after_first_waiter_leaves_with_cache_disabled():
    async def scenario():
        base = _Provider()
        model = CoalescingEmbeddingModel(base, cache_size=0, max_batch_size=2, coalesce_ms=0)
        first = asyncio.create_task(model.embed(("slow", "second")))
        await base.started.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        late = asyncio.create_task(model.embed(("second", "x", "slow", "second")))
        await asyncio.sleep(0)
        base.release.set()
        result = await late
        assert result.vectors == ((6.0,), (1.0,), (4.0,), (6.0,))
        assert result.usage == InferenceUsage(requests=6, input_tokens=3, output_tokens=0)
        assert base.calls == [("slow", "second"), ("x",)]
        await model.embed(("second",))
        assert base.calls[-1] == ("second",)

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_failed_batch_leaves_independent_batch_running_and_retries_only_misses():
    async def scenario():
        base = _Provider()
        model = CoalescingEmbeddingModel(base, max_batch_size=1, coalesce_ms=0)
        mixed = asyncio.create_task(model.embed(("fail", "slow")))
        await base.started.wait()
        with pytest.raises(RuntimeError, match="offline"):
            await mixed
        base.release.set()
        await model.embed(("slow",))
        retried = await model.embed(("slow", "fail", "fail"))
        assert retried.usage == InferenceUsage(requests=3, input_tokens=1, output_tokens=0)
        assert base.calls.count(("slow",)) == 1
        assert base.calls.count(("fail",)) == 2

    asyncio.run(asyncio.wait_for(scenario(), 5))
