"""Wire corruption, UTF-8 boundaries, and state-free incremental reconstruction."""

import asyncio
import gzip
import json

import pytest

from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.runtime import BuiltinConfig, CaptureSource, PrepareContextRequest, open_builtin_runtime
from powercontext.builtin.runtime.context_pack import (
    ContextPack,
    ContextPackDelta,
    ContextPackError,
    apply_context_pack_delta,
)
from powercontext.builtin.scope import ScopeDraft


@pytest.mark.parametrize("size", [1, 2, 3, 4, 7, 512])
def test_utf8_limits(size):
    for content in ["", "a\r\nb\x00", "海洋", "🙂a海e\u0301", "x" * 1001]:
        if any(len(char.encode()) > size for char in content):
            with pytest.raises(ContextPackError):
                ContextPack.from_content(content, chunk_bytes=size)
            continue
        pack = ContextPack.from_content(content, chunk_bytes=size)
        assert pack == ContextPack.from_content(content, chunk_bytes=size)
        assert b"".join(pack.items) == content.encode()
        assert all(len(item) <= size and item.decode() for item in pack.items)
        assert ContextPack.from_bytes(pack.to_bytes()) == pack
        assert json.loads(pack.to_bytes())["byte_count"] == len(pack.to_bytes())


@pytest.mark.parametrize("chunks", [None, [1], ["%%%"], ["YQ"], ["YR=="], ["/w=="], ["海"]])
def test_invalid_chunks_in_both_wire_types(chunks):
    pack = ContextPack.build(())
    delta = ContextPackDelta.between(pack, pack)
    for obj, field, decode in [
        (pack, "items", ContextPack.from_bytes),
        (delta, "appended", ContextPackDelta.from_bytes),
    ]:
        payload = json.loads(obj.to_bytes())
        payload[field] = chunks
        with pytest.raises(ContextPackError):
            decode(json.dumps(payload).encode())


def test_schema_integrity_and_byte_count():
    pack = ContextPack.from_content("hello")
    delta = ContextPackDelta.between(pack, pack)
    for obj, decode in [(pack, ContextPack.from_bytes), (delta, ContextPackDelta.from_bytes)]:
        payload = json.loads(obj.to_bytes())
        for key in payload:
            with pytest.raises(ContextPackError):
                decode(json.dumps({k: v for k, v in payload.items() if k != key}).encode())
        for bad in [{**payload, "extra": 0}, {**payload, "schema": "unknown"}]:
            with pytest.raises(ContextPackError):
                decode(json.dumps(bad).encode())
        for bad_bytes in [b"null", b"[]", b"{", b"\xff", b'{"schema":0,"schema":0}']:
            with pytest.raises(ContextPackError):
                decode(bad_bytes)
    for count in [True, 1.5, -1, pack.byte_count + 1, str(pack.byte_count)]:
        with pytest.raises(ContextPackError):
            ContextPack.from_bytes(json.dumps({**json.loads(pack.to_bytes()), "byte_count": count}).encode())


@pytest.mark.parametrize("size", [0, -1, True, 1.5])
def test_invalid_chunk_size(size):
    with pytest.raises(ContextPackError):
        ContextPack.from_content("", chunk_bytes=size)


class _Pipeline:
    async def extract(self, request, /):
        return tuple(
            MemoryEntryInput(kind="turn", text=source.content, sources=(source,)) for source in request.sources
        )


def test_native_synthetic_40_round_reconstruction_and_wire_cost():
    """Synthetic integration measurement, not the undisclosed QMSum scoring sequence."""

    async def scenario():
        raw_bytes = full_bytes = incremental_bytes = gzip_bytes = 0
        contents = set()
        base = None
        async with open_builtin_runtime(BuiltinConfig(), candidate_pipeline=_Pipeline()) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(ScopeDraft(title="Pack", summary="Synthetic", idempotency_key="pack"))
            sources = runtime.sources.for_scope(scope.scope_id)
            memory = runtime.memory.for_scope(scope.scope_id)
            context = runtime.context.for_scope(scope.scope_id)
            empty = await context.prepare_incremental(PrepareContextRequest(query="remote"))
            assert isinstance(empty, ContextPack) and empty.items == ()
            for index in range(8):
                await sources.capture(
                    CaptureSource(
                        source_id=f"turn-{index}",
                        content=(
                            f"Speaker {index}: remote control battery interface design. "
                            "The team discussed usability, durability, button layout and battery replacement. " * 8
                        ),
                        metadata={},
                    )
                )
            await memory.flush()
            for turn in range(40):
                if turn in (10, 20, 30):
                    await sources.capture(
                        CaptureSource(
                            source_id=f"addition-{turn}",
                            content=(
                                f"Speaker {turn}: remote control battery interface design. "
                                "New prototype observations and recommendations for the next design meeting. " * 8
                            ),
                            metadata={},
                        )
                    )
                    await memory.flush()
                request = PrepareContextRequest(
                    query=("remote", "battery", "interface", "design")[turn // 10], max_bytes=16000
                )
                prepared = await context.prepare(request)
                assert prepared.content
                target = prepared.content.encode()
                contents.add(target)
                transmitted = await context.prepare_incremental(request, base_pack=base)
                wire = transmitted.to_bytes()
                if base is None:
                    base = ContextPack.from_bytes(wire)
                else:
                    base = apply_context_pack_delta(base, ContextPackDelta.from_bytes(wire))
                assert b"".join(base.items) == target
                raw_bytes += len(target)
                full_bytes += len(base.to_bytes())
                incremental_bytes += len(wire)
                gzip_bytes += len(gzip.compress(target, compresslevel=9, mtime=0))
        assert len(contents) >= 4
        assert 1 - incremental_bytes / raw_bytes >= 0.45
        print(
            json.dumps({
                "sequence": "synthetic-40-round",
                "distinct_contexts": len(contents),
                "raw_full_bytes": raw_bytes,
                "full_pack_bytes": full_bytes,
                "incremental_bytes": incremental_bytes,
                "gzip_bytes": gzip_bytes,
                "savings_vs_raw": 1 - incremental_bytes / raw_bytes,
                "savings_vs_pack": 1 - incremental_bytes / full_bytes,
                "savings_vs_gzip": 1 - incremental_bytes / gzip_bytes,
            })
        )

    asyncio.run(scenario())
