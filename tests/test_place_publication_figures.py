from __future__ import annotations

import binascii
import importlib.util
import struct
import zlib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "place_publication_figures.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("place_publication_figures", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", binascii.crc32(kind + payload) & 0xFFFFFFFF)


def _png(width: int, height: int) -> bytes:
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\xff\xff\xff" * width for _ in range(height))
    return signature + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(raw)) + _chunk(b"IEND", b"")


def _document_xml() -> str:
    return '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:asvg="http://schemas.microsoft.com/office/drawing/2016/SVG/main">
<w:body>
<w:p><w:r><w:t>Figure 2 | Test phenotype figure</w:t></w:r></w:p>
<w:p><w:r><w:drawing><wp:inline><wp:extent cx="1000" cy="700"/><a:graphic><a:graphicData><a:blip r:embed="rId2"/><asvg:svgBlip r:embed="rId3"/><a:xfrm><a:ext cx="1000" cy="700"/></a:xfrm></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>
<w:p><w:r><w:t>Figure 3 | Test outcome figure</w:t></w:r></w:p>
<w:p><w:r><w:drawing><wp:inline><wp:extent cx="1200" cy="900"/><a:graphic><a:graphicData><a:blip r:embed="rId4"/><asvg:svgBlip r:embed="rId5"/><a:xfrm><a:ext cx="1200" cy="900"/></a:xfrm></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>
</w:body></w:document>'''


def _rels_xml() -> str:
    return '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId2" Type="image" Target="media/f2.png"/>
<Relationship Id="rId3" Type="image" Target="media/f2.svg"/>
<Relationship Id="rId4" Type="image" Target="media/f3.png"/>
<Relationship Id="rId5" Type="image" Target="media/f3.svg"/>
</Relationships>'''


def test_places_png_and_svg_and_updates_aspect_ratio(tmp_path: Path) -> None:
    module = _load_module()
    manuscript = tmp_path / "input.docx"
    figures = tmp_path / "figures"
    figures.mkdir()
    f2_png = _png(200, 100)
    f3_png = _png(300, 150)
    (figures / "Figure2_phenotype_mechanism.png").write_bytes(f2_png)
    (figures / "Figure2_phenotype_mechanism.svg").write_text("<svg>f2-new</svg>", encoding="utf-8")
    (figures / "Figure3_outcome_estimands.png").write_bytes(f3_png)
    (figures / "Figure3_outcome_estimands.svg").write_text("<svg>f3-new</svg>", encoding="utf-8")

    with zipfile.ZipFile(manuscript, "w") as z:
        z.writestr("word/document.xml", _document_xml())
        z.writestr("word/_rels/document.xml.rels", _rels_xml())
        z.writestr("word/media/f2.png", b"old2png")
        z.writestr("word/media/f2.svg", b"old2svg")
        z.writestr("word/media/f3.png", b"old3png")
        z.writestr("word/media/f3.svg", b"old3svg")

    output = tmp_path / "output.docx"
    report = module.place_figures(manuscript, figures, output)
    assert len(report) == 2

    with zipfile.ZipFile(output) as z:
        assert z.read("word/media/f2.png") == f2_png
        assert z.read("word/media/f2.svg") == b"<svg>f2-new</svg>"
        assert z.read("word/media/f3.png") == f3_png
        assert z.read("word/media/f3.svg") == b"<svg>f3-new</svg>"
        xml = z.read("word/document.xml").decode("utf-8")

    # Figure 2 keeps cx=1000 and becomes 2:1 -> cy=500.
    assert '<wp:extent cx="1000" cy="500"' in xml
    assert '<a:ext cx="1000" cy="500"' in xml
    # Figure 3 keeps cx=1200 and becomes 2:1 -> cy=600.
    assert '<wp:extent cx="1200" cy="600"' in xml
    assert '<a:ext cx="1200" cy="600"' in xml


def test_png_dimension_reader(tmp_path: Path) -> None:
    module = _load_module()
    path = tmp_path / "x.png"
    path.write_bytes(_png(37, 19))
    assert module.png_dimensions(path) == (37, 19)
