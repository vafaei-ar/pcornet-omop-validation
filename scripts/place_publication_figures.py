from __future__ import annotations

"""Place code-generated Figures 2 and 3 into a manuscript DOCX without touching source data.

The manuscript is copied to a new output file. Existing OOXML revision markup is
left intact; only the referenced PNG/SVG artwork bytes and their drawing extents
are changed. The script also verifies that running it does not change Git worktree
status.
"""

import argparse
import os
import re
import struct
import subprocess
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
FIGURES = {
    "Figure 2 |": "Figure2_phenotype_mechanism",
    "Figure 3 |": "Figure3_outcome_estimands",
}


def png_dimensions(path: Path) -> tuple[int, int]:
    header = path.read_bytes()[:24]
    if len(header) < 24 or header[:8] != PNG_SIGNATURE or header[12:16] != b"IHDR":
        raise ValueError(f"Not a supported PNG: {path}")
    width, height = struct.unpack(">II", header[16:24])
    if width <= 0 or height <= 0:
        raise ValueError(f"Invalid PNG dimensions in {path}")
    return width, height


def _git_output(args: list[str], cwd: Path) -> str:
    result = subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=True)
    return result.stdout


def repo_root() -> Path | None:
    here = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=here,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip()).resolve()


def git_status(root: Path | None) -> str:
    if root is None:
        return ""
    return _git_output(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        root,
    )


def ensure_safe_output(output: Path, root: Path | None) -> None:
    if root is None:
        return
    try:
        rel = output.resolve().relative_to(root)
    except ValueError:
        return
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", "--", rel.as_posix()],
        cwd=root,
    )
    if ignored.returncode != 0:
        raise ValueError(
            "Refusing to write an unignored DOCX inside the repository because it "
            "would dirty the worktree. Choose an output path outside the repo or "
            "under an ignored directory such as results/."
        )


def relationship_targets(rels_xml: bytes) -> dict[str, str]:
    root = ET.fromstring(rels_xml)
    return {
        element.attrib["Id"]: element.attrib["Target"]
        for element in root.findall(f"{{{REL_NS}}}Relationship")
        if "Id" in element.attrib and "Target" in element.attrib
    }


def _replace_numeric_attr(tag: str, name: str, value: int) -> str:
    pattern = rf'({re.escape(name)}=")\d+(")'
    replaced, count = re.subn(pattern, rf"\g<1>{value}\g<2>", tag, count=1)
    if count != 1:
        raise ValueError(f"Could not update {name} in drawing extent: {tag}")
    return replaced


def update_drawing_extent(
    document_xml: str,
    title_prefix: str,
    width_px: int,
    height_px: int,
) -> tuple[str, list[str], int, int]:
    title_pos = document_xml.find(title_prefix)
    if title_pos < 0:
        raise ValueError(f"Could not find manuscript title paragraph beginning {title_prefix!r}")

    match = re.search(r'r:embed="([^"]+)"', document_xml[title_pos : title_pos + 50000])
    if not match:
        raise ValueError(f"Could not find an image relationship after {title_prefix!r}")
    embed_pos = title_pos + match.start()
    drawing_start = document_xml.rfind("<w:drawing", title_pos, embed_pos)
    drawing_end_start = document_xml.find("</w:drawing>", embed_pos)
    if drawing_start < 0 or drawing_end_start < 0:
        raise ValueError(f"Could not isolate drawing XML for {title_prefix!r}")
    drawing_end = drawing_end_start + len("</w:drawing>")
    drawing = document_xml[drawing_start:drawing_end]
    rel_ids = list(dict.fromkeys(re.findall(r'r:embed="([^"]+)"', drawing)))
    if not rel_ids:
        raise ValueError(f"Could not find image relationships for {title_prefix!r}")

    extent_match = re.search(r"<wp:extent\b[^>]*/?>", drawing)
    if not extent_match:
        raise ValueError(f"Could not find wp:extent for {title_prefix!r}")
    extent_tag = extent_match.group(0)
    cx_match = re.search(r'cx="(\d+)"', extent_tag)
    cy_match = re.search(r'cy="(\d+)"', extent_tag)
    if not cx_match or not cy_match:
        raise ValueError(f"Malformed wp:extent for {title_prefix!r}")
    cx = int(cx_match.group(1))
    old_cy = int(cy_match.group(1))
    new_cy = round(cx * height_px / width_px)
    new_extent = _replace_numeric_attr(_replace_numeric_attr(extent_tag, "cx", cx), "cy", new_cy)
    drawing = drawing[: extent_match.start()] + new_extent + drawing[extent_match.end() :]

    def update_a_ext(match_obj: re.Match[str]) -> str:
        tag = match_obj.group(0)
        if 'cx="' not in tag or 'cy="' not in tag:
            return tag
        return _replace_numeric_attr(_replace_numeric_attr(tag, "cx", cx), "cy", new_cy)

    drawing = re.sub(r"<a:ext\b[^>]*/?>", update_a_ext, drawing)
    updated_xml = document_xml[:drawing_start] + drawing + document_xml[drawing_end:]
    return updated_xml, rel_ids, old_cy, new_cy


def place_figures(manuscript: Path, figure_dir: Path, output: Path) -> list[dict[str, object]]:
    manuscript = manuscript.resolve()
    figure_dir = figure_dir.resolve()
    output = output.resolve()
    if manuscript == output:
        raise ValueError("Output must differ from the source manuscript; the source is never overwritten")
    if not manuscript.is_file():
        raise FileNotFoundError(manuscript)

    root = repo_root()
    before = git_status(root)
    ensure_safe_output(output, root)

    with zipfile.ZipFile(manuscript, "r") as zin:
        document_xml = zin.read("word/document.xml").decode("utf-8")
        targets = relationship_targets(zin.read("word/_rels/document.xml.rels"))
        replacements: dict[str, bytes] = {}
        report: list[dict[str, object]] = []

        for title_prefix, stem in FIGURES.items():
            png_path = figure_dir / f"{stem}.png"
            if not png_path.is_file():
                raise FileNotFoundError(png_path)
            width_px, height_px = png_dimensions(png_path)
            document_xml, rel_ids, old_cy, new_cy = update_drawing_extent(
                document_xml,
                title_prefix,
                width_px,
                height_px,
            )
            members: list[str] = []
            for rel_id in rel_ids:
                target = targets.get(rel_id)
                if not target:
                    raise ValueError(f"Relationship {rel_id!r} has no target")
                if target.startswith("/") or ".." in Path(target).parts:
                    raise ValueError(f"Unexpected image relationship target: {target!r}")
                suffix = Path(target).suffix.lower()
                if suffix not in {".png", ".svg"}:
                    continue
                source_path = figure_dir / f"{stem}{suffix}"
                if not source_path.is_file():
                    raise FileNotFoundError(
                        f"DOCX drawing references {suffix} artwork but generated file is missing: {source_path}"
                    )
                member = f"word/{target}"
                if member not in zin.namelist():
                    raise ValueError(f"DOCX image member not found: {member}")
                replacements[member] = source_path.read_bytes()
                members.append(member)
            if not members:
                raise ValueError(f"No replaceable PNG/SVG relationships found for {title_prefix!r}")
            report.append(
                {
                    "figure": title_prefix.rstrip(" |"),
                    "relationships": rel_ids,
                    "members": members,
                    "png": str(png_path),
                    "pixel_size": [width_px, height_px],
                    "old_cy": old_cy,
                    "new_cy": new_cy,
                }
            )

        output.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=output.stem + ".", suffix=".tmp", dir=output.parent)
        os.close(fd)
        temp_path = Path(temp_name)
        try:
            with zipfile.ZipFile(temp_path, "w") as zout:
                for info in zin.infolist():
                    if info.filename == "word/document.xml":
                        data = document_xml.encode("utf-8")
                    else:
                        data = replacements.get(info.filename, zin.read(info.filename))
                    zout.writestr(info, data)
            os.replace(temp_path, output)
        finally:
            temp_path.unlink(missing_ok=True)

    after = git_status(root)
    if after != before:
        output.unlink(missing_ok=True)
        raise RuntimeError(
            "Figure placement changed Git worktree status; output was removed to keep the repo clean.\n"
            f"Before:\n{before}\nAfter:\n{after}"
        )
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replace manuscript Figures 2 and 3 with canonical code-generated artwork"
    )
    parser.add_argument("--manuscript", type=Path, required=True)
    parser.add_argument(
        "--figure-dir",
        type=Path,
        default=Path("results/publication_assets/figures"),
    )
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output = args.output
    if output is None:
        output = args.manuscript.with_name(args.manuscript.stem + "_figures_updated.docx")
    report = place_figures(args.manuscript, args.figure_dir, output)
    print("status: publication_figures_placed")
    for item in report:
        print(
            f"{item['figure']}: {', '.join(item['members'])} <- {item['png']} (+ SVG when referenced) "
            f"({item['pixel_size'][0]}x{item['pixel_size'][1]})"
        )
    print(f"output: {output.resolve()}")
    print("git_worktree_status_unchanged: true")


if __name__ == "__main__":
    main()
