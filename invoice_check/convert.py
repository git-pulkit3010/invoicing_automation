"""Step 1: invoice PDFs -> Markdown the agent reads, plus a manifest.

The manifest records, per invoice, whether the PDF has a real text layer. A
scanned invoice has none: its Markdown is empty, the agent must read a rendered
image instead, and the verifier will never auto-approve it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pymupdf

from . import pdf_to_markdown
from .common import sha256

MIN_TEXT_CHARS = 40     # less than this on every page = treat as scanned


def convert_folder(invoices_dir: Path, out_dir: Path) -> dict:
    md_dir = out_dir / "md"
    md_dir.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for pdf in sorted(p for p in invoices_dir.iterdir() if p.suffix.lower() == ".pdf"):
        doc = pymupdf.open(pdf)
        try:
            chars = sum(len(page.get_text().strip()) for page in doc)
            pages = doc.page_count
        finally:
            doc.close()
        has_text = chars >= MIN_TEXT_CHARS
        md_path = md_dir / (pdf.stem + ".md")
        if has_text:
            pdf_to_markdown.convert(pdf, md_path)
        else:
            md_path.write_text("<!-- scanned PDF: no text layer; read the rendered page images -->\n", encoding="utf-8")
        manifest[pdf.name] = {
            "pdf": str(pdf.resolve()),
            "markdown": str(md_path.resolve()),
            "has_text": has_text,
            "pages": pages,
            "sha256": sha256(pdf),
        }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def render_pages(pdf: Path, out_dir: Path, dpi: int = 110) -> list[Path]:
    """PNG per page, for scanned invoices the agent has to read visually."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    doc = pymupdf.open(pdf)
    try:
        for i, page in enumerate(doc, start=1):
            p = out_dir / f"{pdf.stem}_p{i}.png"
            page.get_pixmap(dpi=dpi).save(p)
            paths.append(p)
    finally:
        doc.close()
    return paths
