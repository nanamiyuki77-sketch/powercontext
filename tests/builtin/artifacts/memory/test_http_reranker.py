"""HTTP reranking boundaries, complete coverage, and cross-batch selection."""

import asyncio
import json
import math

import httpx
import pytest

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory.http_reranker import HTTPMemoryReranker
from powercontext.builtin.artifacts.memory.models import MemoryHit


def _hits(count: int) -> tuple[MemoryHit, ...]:
    return tuple(
        MemoryHit(
            memory_ref=ArtifactRef(family="memory", artifact_id="m", revision=1),
            entry_id=f"e{rank}",
            entry_version_id=f"v{rank}",
            text=f"候选 {rank}\nOriginal text.",
            score=1 / rank,
            matched_by=("fts",),
        )
        for rank in range(1, count + 1)
    )


@pytest.mark.parametrize("batch_size", [1, 7, 32, 200])
def test_full_pool_stable_sort_and_client_ownership(batch_size):
    candidates = _hits(173)
    before = tuple(hit.model_dump() for hit in candidates)
    seen = []
    requests = []
    values = {rank: (rank % 11) - 5 for rank in range(1, 174)}

    def handler(request):
        body = json.loads(request.content)
        assert request.method == "POST"
        assert set(body) == {"query", "candidates"}
        assert body["query"] == "Which original record?"
        assert 1 <= len(body["candidates"]) <= batch_size
        requests.append(request)
        for item in body["candidates"]:
            assert item == {"rank": item["rank"], "text": candidates[item["rank"] - 1].text}
            seen.append(item["rank"])
        return httpx.Response(
            200,
            json={
                "scores": [
                    {"rank": item["rank"], "score": values[item["rank"]]} for item in reversed(body["candidates"])
                ]
            },
        )

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            reranker = HTTPMemoryReranker(
                "https://reranker.invalid", client=client, batch_size=batch_size, policy_id="test-policy"
            )
            decision = await reranker.rerank("Which original record?", candidates, 10)
            assert not client.is_closed
            assert reranker.policy_id == "test-policy"
            assert decision.selected_ranks == tuple(sorted(values, key=lambda rank: (-values[rank], rank))[:10])
            assert decision.usage.requests == len(requests) == math.ceil(173 / batch_size)
            assert sorted(seen) == list(range(1, 174))
            assert tuple(hit.model_dump() for hit in candidates) == before

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "payload",
    [
        "not JSON",
        "null",
        "[]",
        "{}",
        '{"scores":null}',
        '{"scores":[]}',
        '{"scores":[{"rank":true,"score":0}]}',
        '{"scores":[{"rank":1.0,"score":0}]}',
        '{"scores":[{"rank":"1","score":0}]}',
        '{"scores":[{"rank":1,"score":true}]}',
        '{"scores":[{"rank":1,"score":"0.5"}]}',
        '{"scores":[{"rank":1,"score":null}]}',
        '{"scores":[{"rank":1,"score":-Infinity}]}',
    ],
)
def test_invalid_response_fails_without_fallback(payload):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=payload))
        ) as client:
            with pytest.raises(ValueError):
                await HTTPMemoryReranker("https://reranker.invalid", client=client).rerank("q", _hits(1), 1)

    asyncio.run(scenario())


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_invalid_bounds_do_not_send_requests(value):
    def handler(_):
        pytest.fail("invalid input must fail before HTTP")

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ValueError):
                HTTPMemoryReranker("https://reranker.invalid", client=client, batch_size=value)
            reranker = HTTPMemoryReranker("https://reranker.invalid", client=client)
            with pytest.raises(ValueError):
                await reranker.rerank("q", _hits(2), value)

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [302, 400, 429, 500])
def test_non_success_status_is_preserved(status):
    async def scenario():
        async with httpx.AsyncClient(
            follow_redirects=True,
            transport=httpx.MockTransport(lambda _: httpx.Response(status, headers={"Location": "/redirect"})),
        ) as client:
            with pytest.raises(httpx.HTTPStatusError) as error:
                await HTTPMemoryReranker("https://reranker.invalid", client=client).rerank("q", _hits(1), 1)
            assert error.value.response.status_code == status

    asyncio.run(scenario())


def test_second_batch_cannot_return_first_batch_rank():
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"scores": [{"rank": 1, "score": 1}]}))
        ) as client:
            with pytest.raises(ValueError):
                await HTTPMemoryReranker("https://reranker.invalid", client=client, batch_size=1).rerank(
                    "q", _hits(2), 1
                )

    asyncio.run(scenario())
