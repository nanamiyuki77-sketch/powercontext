"""Canonical, self-validating context packs and independently replayable deltas."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, cast

_PACK = "powercontext.context-pack.v1"
_DELTA = "powercontext.context-pack-delta.v1"


class ContextPackError(ValueError):
    """Invalid context wire data, chunks, or reconstruction."""


def _json(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _validate_items(items: tuple[bytes, ...]) -> None:
    if not isinstance(items, tuple) or any(not isinstance(item, bytes) for item in items):
        raise ContextPackError("chunks must be a tuple of bytes")  # noqa: TRY003
    try:
        for item in items:
            item.decode("utf-8")
    except UnicodeError as error:
        raise ContextPackError("chunks must contain valid UTF-8") from error  # noqa: TRY003


def _encode_items(items: tuple[bytes, ...]) -> list[str]:
    return [base64.b64encode(item).decode("ascii") for item in items]


def _decode_items(value: object) -> tuple[bytes, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ContextPackError("wire chunks must be a Base64 string array")  # noqa: TRY003
    try:
        items = tuple(base64.b64decode(item, validate=True) for item in cast(list[str], value))
    except (ValueError, binascii.Error) as error:
        raise ContextPackError("invalid Base64") from error  # noqa: TRY003
    if _encode_items(items) != value:
        raise ContextPackError("noncanonical Base64")  # noqa: TRY003
    _validate_items(items)
    return items


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ContextPackError("duplicate JSON fields")  # noqa: TRY003
    return result


def _decode(data: bytes, schema: str, fields: set[str]) -> dict[str, Any]:
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_object)
    except (ValueError, UnicodeError, AttributeError) as error:
        raise ContextPackError("invalid UTF-8 JSON") from error  # noqa: TRY003
    if not isinstance(value, dict) or set(value) != fields or value["schema"] != schema:
        raise ContextPackError("invalid wire fields or schema")  # noqa: TRY003
    return value


def _validate_digest(value: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
        raise ContextPackError("invalid SHA-256 digest")  # noqa: TRY003


@dataclass(frozen=True)
class ContextPack:
    items: tuple[bytes, ...]

    def __post_init__(self) -> None:
        _validate_items(self.items)

    @classmethod
    def build(cls, items: tuple[bytes, ...]) -> ContextPack:
        return cls(items)

    @classmethod
    def from_content(cls, content: str | None, *, chunk_bytes: int = 512) -> ContextPack:
        if type(chunk_bytes) is not int or chunk_bytes <= 0:
            raise ContextPackError("chunk_bytes must be a positive integer")  # noqa: TRY003
        if content is not None and not isinstance(content, str):
            raise ContextPackError("content must be text or None")  # noqa: TRY003
        try:
            data = (content or "").encode("utf-8")
        except (UnicodeError, AttributeError) as error:
            raise ContextPackError("invalid text content") from error  # noqa: TRY003
        items = []
        start = 0
        while start < len(data):
            end = min(start + chunk_bytes, len(data))
            while end < len(data) and end > start and data[end] & 0xC0 == 0x80:
                end -= 1
            if end == start:
                raise ContextPackError("chunk limit cannot fit a UTF-8 character")  # noqa: TRY003
            items.append(data[start:end])
            start = end
        return cls.build(tuple(items))

    @property
    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(_json({"schema": _PACK, "items": _encode_items(self.items)})).hexdigest()

    @property
    def byte_count(self) -> int:
        return len(self.to_bytes())

    def to_bytes(self) -> bytes:
        payload = {"schema": _PACK, "items": _encode_items(self.items), "digest": self.digest, "byte_count": 0}
        encoded = _json(payload)
        # The decimal width of byte_count contributes to its own value.
        while payload["byte_count"] != len(encoded):
            payload["byte_count"] = len(encoded)
            encoded = _json(payload)
        return encoded

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPack:
        value = _decode(data, _PACK, {"schema", "items", "digest", "byte_count"})
        pack = cls.build(_decode_items(value["items"]))
        if (
            value["digest"] != pack.digest
            or type(value["byte_count"]) is not int
            or value["byte_count"] != pack.byte_count
        ):
            raise ContextPackError("pack digest or byte count mismatch")  # noqa: TRY003
        return pack


@dataclass(frozen=True)
class ContextPackDelta:
    base_digest: str
    target_digest: str
    prefix_items: int
    appended: tuple[bytes, ...]

    def __post_init__(self) -> None:
        _validate_digest(self.base_digest)
        _validate_digest(self.target_digest)
        if type(self.prefix_items) is not int or self.prefix_items < 0:
            raise ContextPackError("prefix_items must be a nonnegative integer")  # noqa: TRY003
        _validate_items(self.appended)

    @classmethod
    def between(cls, base: ContextPack, target: ContextPack) -> ContextPackDelta:
        prefix = 0
        for left, right in zip(base.items, target.items, strict=False):
            if left != right:
                break
            prefix += 1
        return cls(base.digest, target.digest, prefix, target.items[prefix:])

    @property
    def byte_count(self) -> int:
        return len(self.to_bytes())

    def to_bytes(self) -> bytes:
        return _json({
            "schema": _DELTA,
            "base_digest": self.base_digest,
            "target_digest": self.target_digest,
            "prefix_items": self.prefix_items,
            "appended": _encode_items(self.appended),
        })

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPackDelta:
        value = _decode(data, _DELTA, {"schema", "base_digest", "target_digest", "prefix_items", "appended"})
        return cls(
            value["base_digest"], value["target_digest"], value["prefix_items"], _decode_items(value["appended"])
        )


def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta) -> ContextPack:
    if base.digest != delta.base_digest or delta.prefix_items > len(base.items):
        raise ContextPackError("base digest mismatch or prefix out of bounds")  # noqa: TRY003
    target = ContextPack.build(base.items[: delta.prefix_items] + delta.appended)
    if target.digest != delta.target_digest:
        raise ContextPackError("target digest mismatch")  # noqa: TRY003
    return target
