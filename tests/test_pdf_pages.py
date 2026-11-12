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

"""Complete physical pages, original revision identity, and parser failures."""

import asyncio
import hashlib
import io

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, NumberObject, RectangleObject

from powercontext.sources import SourceDefinitionRegistry, SourceMaterialization, TextEvidence
from powercontext.sources.pdf_pages import (
    PDF_PAGE_SOURCE_DEFINITION,
    PDFDocumentInput,
    PDFPageInput,
    parse_pdf_document,
)
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY


def make_pdf(texts, *, password=None, base_font="/Helvetica"):
    writer = PdfWriter()
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject(base_font),
    })
    for text in texts:
        page = writer.add_blank_page(width=600, height=800)
        if text:
            page[NameObject("/Resources")] = DictionaryObject({
                NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})
            })
            stream = DecodedStreamObject()
            escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            stream.set_data(f"BT /F1 12 Tf 30 700 Td ({escaped}) Tj ET".encode())
            page[NameObject("/Contents")] = stream
    if password is not None:
        writer.encrypt(user_password=password, owner_password="owner")  # noqa: S106
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


@pytest.mark.parametrize("password", [None, ""])
def test_all_physical_pages_and_numbers_are_preserved(password):
    raw = make_pdf(["Sales 00123 4,567.89", "", "Debt (123) 2025"], password=password)
    pages = parse_pdf_document(PDFDocumentInput(document_id="Report", content=raw))
    assert [page.page_number for page in pages] == [1, 2, 3]
    assert [page.text for page in pages] == ["Sales 00123 4,567.89", "", "Debt (123) 2025"]
    assert all(page.document_sha256 == hashlib.sha256(raw).hexdigest() for page in pages)
    registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
    for page in pages:
        source = asyncio.run(registry.resolve(page))
        assert source.materialization is SourceMaterialization.CAPTURED
        assert asyncio.run(registry.read(source)) == page
        evidence = TextEvidence.model_validate(registry.project(source, TEXT_EVIDENCE_PROJECTION_KEY))
        assert evidence.content == f"Report\n{page.text}"
        assert evidence.metadata == {
            "document_id": "Report",
            "document_sha256": page.document_sha256,
            "page_number": page.page_number,
        }


def test_xref_line_break_preserves_original_digest_and_other_invalid_offsets_fail():
    raw = make_pdf(["Sales 100"])
    body, marker, trailer = raw.rpartition(b"startxref")
    offset = int(trailer.split()[0])
    for adjustment in (1, 5):
        edited = body + marker + trailer.replace(str(offset).encode(), str(offset - adjustment).encode(), 1)
        value = PDFDocumentInput(document_id="report", content=edited)
        if adjustment == 1:
            (page,) = parse_pdf_document(value)
            assert page.text == "Sales 100"
            assert page.document_sha256 == hashlib.sha256(edited).hexdigest()
        else:
            with pytest.raises(ValueError):
                parse_pdf_document(value)


@pytest.mark.parametrize("crop_media", [False, True])
def test_positioned_text_respects_word_boundaries_table_values_and_page_crop(crop_media):
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(make_pdf(["placeholder"], base_font="/Courier"))))
    page = writer.pages[0]
    # Each short run is its own text object; the last one is on the adjacent printed panel.
    runs = [(10, "Ne"), (22, "t"), (34, "sa"), (46, "le"), (58, "s"), (80, "12"), (92, "34"), (120, "56")]
    stream = DecodedStreamObject()
    stream.set_data("\n".join(f"BT /F1 10 Tf {x} 100 Td ({word}) Tj ET" for x, word in runs).encode())
    page[NameObject("/Contents")] = stream
    page.cropbox = RectangleObject([0, 0, 115, 200])
    if crop_media:
        page.mediabox = page.cropbox
    output = io.BytesIO()
    writer.write(output)
    (result,) = parse_pdf_document(PDFDocumentInput(document_id="panels", content=output.getvalue()))
    assert result.text.split() == ["Net", "sales", "1234"]


def test_stylistic_glyph_is_decoded_only_when_named_by_the_font():
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=100)
    encoding = DictionaryObject({
        NameObject("/BaseEncoding"): NameObject("/WinAnsiEncoding"),
        NameObject("/Differences"): ArrayObject([NumberObject(ord("x")), NameObject("/t.alt")]),
    })
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Courier"),
        NameObject("/Encoding"): encoding,
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})
    })
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 10 Tf 10 50 Td (Reporx /t.alt) Tj ET BT /F1 10 Tf 10 -50 Td (printer slug) Tj ET")
    page[NameObject("/Contents")] = stream
    output = io.BytesIO()
    writer.write(output)
    (result,) = parse_pdf_document(PDFDocumentInput(document_id="glyphs", content=output.getvalue()))
    assert result.text.strip() == "Report /t.alt"


@pytest.mark.parametrize("raw", [b"", b"not pdf", b"%PDF-1.7\ntruncated"])
def test_invalid_pdf_raises_value_error(raw):
    with pytest.raises(ValueError):
        parse_pdf_document(PDFDocumentInput(document_id="doc", content=raw))


def test_password_and_damaged_tail_fail_atomically():
    for raw in (make_pdf(["secret"], password="secret"), make_pdf(["first", "second"])[:-40]):  # noqa: S106
        with pytest.raises(ValueError):
            parse_pdf_document(PDFDocumentInput(document_id="doc", content=raw))


@pytest.mark.parametrize("document_id", ["", " ", "\t\n", "\u2003"])
def test_blank_document_ids_are_invalid(document_id):
    with pytest.raises(ValueError):
        PDFDocumentInput(document_id=document_id, content=make_pdf([""]))


def test_revision_document_and_physical_page_have_distinct_bounded_identity():
    identities = []
    for document_id, digest, number in [
        ("a:b", "a" * 64, 1),
        ("a", "b" * 64, 1),
        ("a:b", "a" * 64, 2),
        ("a:b", "c" * 64, 1),
        ("文档" * 300, "a" * 64, 1),
    ]:
        page = PDFPageInput(document_id=document_id, document_sha256=digest, page_number=number, text="text")
        source = asyncio.run(PDF_PAGE_SOURCE_DEFINITION.resolve(page))
        assert len(source.name) <= 256
        assert asyncio.run(PDF_PAGE_SOURCE_DEFINITION.resolve(page)).name == source.name
        identities.append(source.name)
    assert len(set(identities)) == len(identities)


@pytest.mark.parametrize(
    "changes", [{"document_sha256": "bad"}, {"document_sha256": "A" * 64}, {"page_number": 0}, {"page_number": -1}]
)
def test_invalid_page_metadata(changes):
    with pytest.raises(ValueError):
        PDFPageInput.model_validate(
            {"document_id": "doc", "document_sha256": "a" * 64, "page_number": 1, "text": ""} | changes
        )
