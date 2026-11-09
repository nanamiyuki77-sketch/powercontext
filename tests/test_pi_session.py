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

"""Pi parser fidelity, graph validation, and bounded event identities."""

import asyncio
import json

import pytest

from powercontext.sources import SourceDefinitionRegistry, SourceMaterialization, TextEvidence
from powercontext.sources.pi_session import PI_EVENT_SOURCE_DEFINITION, PiEventSource, PiSessionInput, parse_pi_session
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY


def archive(records, *, session_id="session", leaf=None):
    header = {"type": "session", "version": 3, "id": session_id}
    content = "\n".join(json.dumps(record, ensure_ascii=False) for record in [header, *records]).encode()
    return PiSessionInput(content=content, leaf_id=leaf)


def message(identifier="a", parent=None, content="text", role="user"):
    return {"id": identifier, "parentId": parent, "type": "message", "message": {"role": role, "content": content}}


@pytest.mark.parametrize("text", [" 原样 \t", "one\u2028two\u2029three\u0085four", "\r\n", "汉字😀", ""])
def test_text_is_preserved_without_trimming_or_unicode_line_splitting(text):
    events = parse_pi_session(archive([message(content=text)]))
    assert [event.content for event in events] == ([f"user: {text}"] if text else [])


def test_blocks_tools_and_compaction_preserve_original_evidence():
    records = [
        message(
            content=[
                {"type": "text", "text": "one"},
                {"type": "image", "data": "private"},
                {"type": "thinking", "thinking": "private"},
                {"type": "text", "text": "two"},
            ]
        ),
        {"type": "model_change", "id": "control", "parentId": "a"},
        message(
            "call",
            "control",
            [{"type": "toolCall", "name": "read", "id": "tc", "arguments": {"z": "中", "a": [1, False]}}],
            "assistant",
        ),
        {
            "type": "compaction",
            "id": "summary",
            "parentId": "call",
            "summary": "summary",
            "firstKeptEntryId": "summary",
        },
        {
            "type": "custom_message",
            "id": "custom",
            "parentId": "summary",
            "content": [{"type": "text", "text": "custom"}],
        },
        message("hidden", "custom", [{"type": "thinking", "thinking": "secret"}], "assistant"),
    ]
    events = parse_pi_session(archive(records))
    assert [event.event_id for event in events] == ["a", "call", "summary", "custom"]
    assert [event.content for event in events] == [
        "user: one\ntwo",
        'assistant: [tool_call read tc] {"a":[1,false],"z":"中"}',
        "compaction: summary",
        "custom: custom",
    ]
    assert events[1].parent_id == "control"
    assert events[0].record == records[0]
    assert parse_pi_session(archive(records, leaf="control")) == events[:1]


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        {},
        {"id": "bad", "type": "model_change"},
        {"id": "bad", "type": "model_change", "parentId": "missing"},
        {"id": "bad", "type": "model_change", "parentId": "bad"},
        {"id": "bad", "type": "model_change", "parentId": []},
        {"id": "a", "type": "model_change", "parentId": None},
        {"id": "", "type": "model_change", "parentId": None},
        {"id": "bad", "type": [], "parentId": None},
    ],
)
def test_entire_graph_is_validated_even_outside_selected_branch(bad):
    with pytest.raises(ValueError):
        parse_pi_session(archive([message(), bad], leaf="a"))


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"\xff",
        b"null",
        b"[]",
        b"{}",
        b"{broken}",
        b'{"type":"session","version":3,"id":""}',
        b'{"type":"session","version":2,"id":"s"}',
        b'{"type":"session","version":3,"id":42}',
        b'{"type":"session","version":3,"id":"s","x":NaN}',
    ],
)
def test_invalid_input_raises_value_error(raw):
    with pytest.raises(ValueError):
        parse_pi_session(PiSessionInput(content=raw))


def test_empty_archive_missing_leaf_and_bom():
    value = archive([])
    assert parse_pi_session(value) == ()
    with pytest.raises(ValueError):
        parse_pi_session(PiSessionInput(content=value.content, leaf_id="missing"))
    value = archive([message()])
    assert parse_pi_session(PiSessionInput(content=b"\xef\xbb\xbf\r\n" + value.content + b"\n\n")) == parse_pi_session(
        value
    )


def test_full_large_tool_result_and_record_survive_source_roundtrip():
    output = "中😀 evidence\r\n" * 2000
    record = message(content=[{"type": "text", "text": output}], role="toolResult")
    record["message"].update(toolName="read", toolCallId="call", details={"extra": [1, 2]})
    event = parse_pi_session(archive([record]))[0]
    registry = SourceDefinitionRegistry((PI_EVENT_SOURCE_DEFINITION,))
    source = asyncio.run(registry.resolve(event))
    assert isinstance(source, PiEventSource)
    assert source.materialization is SourceMaterialization.CAPTURED
    assert asyncio.run(registry.read(source)) == event
    evidence = TextEvidence.model_validate(registry.project(source, TEXT_EVIDENCE_PROJECTION_KEY))
    assert evidence.content == "toolResult: [tool_result read call] " + output
    assert source.record == record
    assert evidence.metadata == {
        "session_id": "session",
        "event_id": "a",
        "parent_id": None,
        "event_type": "message",
        "role": "toolResult",
    }


def test_identity_is_stable_bounded_and_component_safe():
    identities = []
    for session_id, event_id in [("a:b", "c"), ("a", "b:c"), ("文" * 400, "项" * 400), ("a%3Ab", "c")]:
        first = parse_pi_session(archive([message(event_id)], session_id=session_id))[0]
        changed = parse_pi_session(archive([message(event_id, content="changed")], session_id=session_id))[0]
        source = asyncio.run(PI_EVENT_SOURCE_DEFINITION.resolve(first))
        assert 0 < len(source.name) <= 256
        assert source.name == asyncio.run(PI_EVENT_SOURCE_DEFINITION.resolve(changed)).name
        identities.append(source.name)
    assert len(set(identities)) == len(identities)
