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

"""CSV contract boundaries beyond the public airport workload."""

import asyncio
import json
from urllib.parse import quote

import pytest

from powercontext.sources import SourceDefinitionRegistry, SourceMaterialization, TextEvidence
from powercontext.sources.csv_rows import (
    CSV_ROW_SOURCE_DEFINITION,
    CSVDocumentInput,
    CSVRowInput,
    CSVRowSource,
    csv_row_source_id,
    parse_csv_document,
)
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"\xef\xbb\xbf",
        b"\n",
        b'""\n',
        b"id,id\n",
        b"id, \t\n",
        b"id,\n",
        b"other\n",
        b"id\n\xff",
        b'id\n"unterminated',
        b'id\n"x"oops\n',
        b"id,n\nok,first\nbad\n",
        b"id,n\nok,first\nbad,extra,value\n",
        b"id,n\nok,first\n,blank\n",
        b"id,n\nok,first\n \t,blank\n",
        b"id,n\nok,first\nok,duplicate\n",
        b'id\nok\n""\n',
        b"id\nok\n" + b"x" * 253 + b"\n",
    ],
)
def test_document_errors_are_atomic(content):
    with pytest.raises(ValueError):
        parse_csv_document(CSVDocumentInput(document_id="doc", primary_key="id", content=content))


@pytest.mark.parametrize("document_id", ["", " ", " doc", "doc\t", "\u2003doc"])
def test_invalid_document_identity_even_without_rows(document_id):
    with pytest.raises(ValueError):
        parse_csv_document(CSVDocumentInput(document_id=document_id, primary_key="id", content=b"id\n"))
    with pytest.raises(ValueError):
        csv_row_source_id(document_id, "key")


@pytest.mark.parametrize("primary_key", ["", "id", " id", "id "])
def test_header_matching_never_trims(primary_key):
    with pytest.raises(ValueError):
        parse_csv_document(CSVDocumentInput(document_id="doc", primary_key=primary_key, content=b" id \n"))


def test_exact_strings_newlines_and_projection_round_trip():
    rows = parse_csv_document(
        CSVDocumentInput(
            document_id="文档:/%",
            primary_key=" id ",
            content='\ufeff id ,id,empty,note\r\n\r\n key ,00123,,"中,文\r\n第二行 ""引号"""\r\nkey ,false,,\r\n'.encode(),
        )
    )
    assert len(rows) == 2
    assert rows[0].values == (" key ", "00123", "", '中,文\r\n第二行 "引号"')
    registry = SourceDefinitionRegistry((CSV_ROW_SOURCE_DEFINITION,))
    for row in rows:
        source = asyncio.run(registry.resolve(row))
        assert source.materialization is SourceMaterialization.CAPTURED
        restored = CSVRowSource.model_validate_json(source.model_dump_json())
        assert asyncio.run(registry.read(restored)) == row
        evidence = TextEvidence.model_validate(registry.project(restored, TEXT_EVIDENCE_PROJECTION_KEY))
        mapping = dict(zip(row.columns, row.values, strict=True))
        assert evidence.content == json.dumps(mapping, ensure_ascii=False, separators=(",", ":"))
        assert evidence.metadata == {
            "document_id": row.document_id,
            "primary_key": row.primary_key,
            "primary_key_value": row.primary_key_value,
            "columns": list(row.columns),
            "row": mapping,
        }


def test_identity_encoding_collision_and_exact_length_boundary():
    assert csv_row_source_id("文档:/%", " key ") == f"csv:{quote('文档:/%', safe='')}:%20key%20"
    assert csv_row_source_id("a:b", "c") != csv_row_source_id("a", "b:c")
    assert csv_row_source_id("a%3Ab", "c") != csv_row_source_id("a:b", "c")
    assert len(csv_row_source_id("d", "k" * 250)) == 256
    with pytest.raises(ValueError):
        csv_row_source_id("d", "k" * 251)
    for key in ("", " ", "\t\r\n", "\u2003"):
        with pytest.raises(ValueError):
            csv_row_source_id("doc", key)


@pytest.mark.parametrize(
    "changes",
    [
        {"columns": ("id", "id")},
        {"values": ()},
        {"values": ("other",)},
        {"primary_key": "missing"},
        {"primary_key_value": " "},
    ],
)
def test_direct_row_inputs_cannot_capture_inconsistent_records(changes):
    fields = {
        "document_id": "doc",
        "primary_key": "id",
        "primary_key_value": "key",
        "columns": ("id",),
        "values": ("key",),
    }
    with pytest.raises(ValueError):
        CSVRowInput(**(fields | changes))


def test_header_only_and_blank_physical_records():
    assert parse_csv_document(CSVDocumentInput(document_id="doc", primary_key="id", content=b"id\r\n\r\n\n")) == ()
    rows = parse_csv_document(CSVDocumentInput(document_id="doc", primary_key="id", content=b'id,n\n\n1,""\n\n'))
    assert rows[0].values == ("1", "")


@pytest.mark.parametrize("changes", [{"name": "other"}, {"source_id": "other"}, {"materialization": "referenced"}])
def test_sources_reject_identity_or_materialization_mismatches(changes):
    row = parse_csv_document(CSVDocumentInput(document_id="doc", primary_key="id", content=b"id\nkey\n"))[0]
    source = asyncio.run(CSV_ROW_SOURCE_DEFINITION.resolve(row))
    with pytest.raises(ValueError):
        CSVRowSource.model_validate(source.model_dump() | changes)


def test_reordering_and_non_key_edits_preserve_identity():
    registry = SourceDefinitionRegistry((CSV_ROW_SOURCE_DEFINITION,))
    identities = []
    for content in (b"id,n\n1,old\n2,other\n", b"id,n\n2,changed\n1,new\n"):
        rows = parse_csv_document(CSVDocumentInput(document_id="doc", primary_key="id", content=content))
        identities.append({row.primary_key_value: asyncio.run(registry.resolve(row)).source_id for row in rows})
    assert identities[0] == identities[1]
