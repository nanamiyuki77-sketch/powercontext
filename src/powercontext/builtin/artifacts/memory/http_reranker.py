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

"""Batched HTTP scoring with stable selection over the complete candidate pool."""

import httpx
from pydantic import BaseModel, ConfigDict, Field

from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.artifacts.memory.reranking import MemoryRerankDecision
from powercontext.builtin.inference import InferenceUsage


class _Score(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    rank: int
    score: float = Field(allow_inf_nan=False)


class _Response(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    scores: list[_Score]


class HTTPMemoryReranker:
    """Use a caller-owned client; preserve global ranks across all HTTP batches."""

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient,
        batch_size: int = 32,
        policy_id: str = "http-cross-encoder-v1",
    ) -> None:
        if type(batch_size) is not int or batch_size <= 0:
            raise ValueError("batch_size must be a positive integer")  # noqa: TRY003
        self._endpoint = endpoint
        self._client = client
        self._batch_size = batch_size
        self.policy_id = policy_id

    async def rerank(self, query: str, candidates: tuple[MemoryHit, ...], limit: int, /) -> MemoryRerankDecision:
        if not candidates or type(limit) is not int or not 1 <= limit <= len(candidates):
            raise ValueError("limit must be an integer between one and the candidate count")  # noqa: TRY003
        scores: list[_Score] = []
        requests = 0
        for start in range(0, len(candidates), self._batch_size):
            batch = candidates[start : start + self._batch_size]
            response = await self._client.post(
                self._endpoint,
                follow_redirects=False,
                json={
                    "query": query,
                    "candidates": [{"rank": rank, "text": hit.text} for rank, hit in enumerate(batch, start=start + 1)],
                },
            )
            requests += 1
            response.raise_for_status()
            result = _Response.model_validate(response.json())
            expected = set(range(start + 1, start + len(batch) + 1))
            if len(result.scores) != len(batch) or {item.rank for item in result.scores} != expected:
                raise ValueError("scores must cover each rank in the current batch exactly once")  # noqa: TRY003
            scores.extend(result.scores)
        ordered = sorted(scores, key=lambda item: (-item.score, item.rank))
        return MemoryRerankDecision(
            selected_ranks=tuple(item.rank for item in ordered[:limit]), usage=InferenceUsage(requests=requests)
        )
