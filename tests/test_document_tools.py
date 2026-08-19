from pathlib import Path

import pytest

from hal.document_tools import (
    DocxReadTool, DocxWriteTool, PdfFormWriteTool, PdfReadTool, PdfWriteTool,
)
from hal.tools import Registry


def test_pdf_tools_round_trip_text_and_pages(tmp_path: Path) -> None:
    pytest.importorskip("pypdf")
    pytest.importorskip("reportlab")
    path = tmp_path / "report.pdf"

    result = PdfWriteTool().run({
        "path": str(path), "title": "Status report",
        "content": "# Summary\n\nFirst page content.\n\nSecond paragraph.",
        "page_size": "a4",
    })
    text = PdfReadTool().run({"path": str(path), "start_page": 1, "page_limit": 1})

    assert "wrote " in result
    assert path.read_bytes().startswith(b"%PDF-")
    assert "Page 1 of 1" in text
    assert "Summary" in text
    assert "First page content" in text


def test_docx_tools_round_trip_headings_lists_and_paragraphs(tmp_path: Path) -> None:
    pytest.importorskip("docx")
    path = tmp_path / "brief.docx"

    result = DocxWriteTool().run({
        "path": str(path), "title": "Project brief",
        "content": "# Overview\n\nA short brief.\n\n- First\n- Second",
    })
    text = DocxReadTool().run({"path": str(path)})

    assert "wrote " in result
    assert path.read_bytes().startswith(b"PK")
    assert "# Overview" in text
    assert "A short brief." in text
    assert "First" in text
    assert "Second" in text


def test_pdf_form_write_creates_fillable_fields_and_signature_widget(tmp_path: Path) -> None:
    pypdf = pytest.importorskip("pypdf")
    pytest.importorskip("reportlab")
    path = tmp_path / "application.pdf"

    result = PdfFormWriteTool().run({
        "path": str(path), "title": "Application", "page_size": "letter",
        "pages": [{
            "texts": [{"text": "Application", "x": 72, "y": 740, "font_size": 18, "bold": True}],
            "fields": [
                {"name": "full_name", "type": "text", "label": "Full name", "x": 72, "y": 670, "width": 240, "height": 24, "required": True},
                {"name": "notes", "type": "multiline", "label": "Notes", "x": 72, "y": 560, "width": 300, "height": 72},
                {"name": "approved", "type": "checkbox", "label": "Approved", "x": 72, "y": 500, "width": 18, "height": 18},
                {"name": "department", "type": "choice", "label": "Department", "x": 72, "y": 440, "width": 180, "height": 24, "options": ["Engineering", "Operations"]},
                {"name": "applicant_signature", "type": "signature", "label": "Applicant signature", "x": 72, "y": 340, "width": 240, "height": 48},
            ],
        }],
    })

    reader = pypdf.PdfReader(str(path))
    fields = reader.get_fields()
    assert set(fields) == {"full_name", "notes", "approved", "department", "applicant_signature"}
    assert fields["full_name"]["/FT"] == "/Tx"
    assert fields["approved"]["/FT"] == "/Btn"
    assert fields["department"]["/FT"] == "/Ch"
    assert fields["applicant_signature"]["/FT"] == "/Sig"
    assert "/V" not in fields["applicant_signature"]
    assert "1 signature" in result


def test_pdf_form_write_rejects_duplicate_and_out_of_bounds_fields(tmp_path: Path) -> None:
    pytest.importorskip("pypdf")
    pytest.importorskip("reportlab")
    base = {"name": "same", "type": "text", "x": 72, "y": 700, "width": 100, "height": 20}

    with pytest.raises(ValueError, match="duplicate form field"):
        PdfFormWriteTool().run({
            "path": str(tmp_path / "duplicate.pdf"),
            "pages": [{"fields": [base, dict(base)]}],
        })
    with pytest.raises(ValueError, match="extends outside"):
        PdfFormWriteTool().run({
            "path": str(tmp_path / "outside.pdf"),
            "pages": [{"fields": [{**base, "name": "outside", "x": 600}]}],
        })


@pytest.mark.parametrize(
    ("tool", "path"),
    [
        (PdfReadTool(), "file.txt"),
        (PdfWriteTool(), "file.txt"),
        (PdfFormWriteTool(), "file.txt"),
        (DocxReadTool(), "file.txt"),
        (DocxWriteTool(), "file.txt"),
    ],
)
def test_document_tools_require_matching_extensions(tool, path: str) -> None:
    arguments = {"path": path}
    if tool.spec.name.endswith("_write"):
        if tool.spec.name == "pdf_form_write":
            arguments["pages"] = [{}]
        else:
            arguments["content"] = "text"
    with pytest.raises(ValueError, match="path must end with"):
        tool.run(arguments)


def test_document_writes_follow_registry_workspace_policy(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside.pdf"
    registry = Registry([PdfWriteTool()], write_root=root, cwd=root)

    with pytest.raises(PermissionError, match="outside workspace was denied"):
        registry.run("pdf_write", {"path": str(outside), "content": "text"})
    assert not outside.exists()


def test_document_writes_cannot_replace_files_in_protected_workflow(tmp_path: Path) -> None:
    path = tmp_path / "existing.docx"
    path.write_bytes(b"original")
    registry = Registry([DocxWriteTool()], cwd=tmp_path)

    with pytest.raises(PermissionError, match="cannot replace existing file"):
        registry.run(
            "docx_write", {"path": str(path), "content": "replacement"},
            protect_existing_files=True,
        )
    assert path.read_bytes() == b"original"
