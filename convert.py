#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pymupdf>=1.24", "pillow>=10"]
# ///
"""Convert Notability .note backups to PDF.

A .note file is a zip archive containing a Session.plist (NSKeyedArchiver)
with handwriting strokes, page layout, and image placements, plus any
imported PDFs under PDFs/ and images under Images/.

Verified against sessionFormatVersion 7, 8 and 9 (Notability 11.x-14.x).

Usage:
    ./convert.py INPUT_DIR OUTPUT_DIR [--force] [--audio]
    ./convert.py single.note OUTPUT_DIR
"""

import argparse
import io
import math
import plistlib
import struct
import sys
import zipfile
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image

DEFAULT_NOTE_WIDTH = 572.0
LETTER_ASPECT = 792.0 / 612.0  # US letter height/width


# ---------------------------------------------------------------- plist graph

class Archive:
    """Minimal NSKeyedArchiver graph reader."""

    def __init__(self, data: bytes):
        self.plist = plistlib.loads(data)
        self.objs = self.plist["$objects"]

    def deref(self, v):
        return self.objs[v.data] if isinstance(v, plistlib.UID) else v

    def classname(self, o):
        if isinstance(o, dict) and "$class" in o:
            return self.objs[o["$class"].data].get("$classname")
        return None

    def find(self, classname):
        for o in self.objs:
            if self.classname(o) == classname:
                return o
        return None

    def resolve(self, v, depth=0):
        """Recursively convert NS containers to plain python values."""
        v = self.deref(v)
        if isinstance(v, dict):
            c = self.classname(v)
            if c in ("NSDictionary", "NSMutableDictionary"):
                return dict(
                    zip(
                        (self.resolve(k, depth + 1) for k in v["NS.keys"]),
                        (self.resolve(x, depth + 1) for x in v["NS.objects"]),
                    )
                )
            if c in ("NSArray", "NSMutableArray"):
                return [self.resolve(x, depth + 1) for x in v["NS.objects"]]
            if c in ("NSString", "NSMutableString"):
                return v.get("NS.string", "")
            if c:
                if depth > 8:
                    return None
                out = {"__class": c}
                for k, x in v.items():
                    if k != "$class":
                        out[k] = self.resolve(x, depth + 1)
                return out
        if v == "$null":
            return None
        return v


def parse_point(s):
    """'{30.9, 2264.8}' -> (30.9, 2264.8)"""
    return tuple(float(t) for t in s.strip("{}").split(","))


def parse_rect(s):
    """'{{0, 0}, {1376, 562}}' -> (0, 0, 1376, 562)"""
    inner = s.replace("{", "").replace("}", "")
    x, y, w, h = (float(t) for t in inner.split(","))
    return x, y, w, h


# ------------------------------------------------------------------ note model

class Stroke:
    __slots__ = ("points", "width", "rgba", "ymin", "ymax")

    def __init__(self, points, width, rgba):
        self.points = points
        self.width = width
        self.rgba = rgba
        ys = [p[1] for p in points]
        self.ymin, self.ymax = min(ys), max(ys)


class Placement:
    """An image placed in the note's document coordinate space."""

    __slots__ = ("png", "x", "y", "w", "h")

    def __init__(self, png, x, y, w, h):
        self.png, self.x, self.y, self.w, self.h = png, x, y, w, h


class TextBlock:
    """A typed text box placed in the note's document coordinate space."""

    __slots__ = ("text", "x", "y", "w", "h", "rgb", "size")

    def __init__(self, text, x, y, w, h, rgb, size):
        self.text, self.x, self.y, self.w, self.h = text, x, y, w, h
        self.rgb, self.size = rgb, size


class Page:
    """One output page: either a PDF page or blank paper."""

    __slots__ = ("pdf_name", "pdf_page", "height")

    def __init__(self, pdf_name=None, pdf_page=None, height=None):
        self.pdf_name = pdf_name
        self.pdf_page = pdf_page  # 0-based
        self.height = height  # in note coordinates; set during layout


class Note:
    def __init__(self, path: Path):
        self.path = path
        # .nbn = Notability's native bundle (a directory), .note = zipped export
        if path.is_dir():
            self.zip = None
            self.folder = path.name
            self.arc = Archive((path / "Session.plist").read_bytes())
        else:
            self.zip = zipfile.ZipFile(path)
            names = self.zip.namelist()
            self.folder = names[0].split("/")[0]
            self.arc = Archive(self.zip.read(f"{self.folder}/Session.plist"))
        self.warnings = []

    def read_member(self, relpath):
        if self.zip is None:
            return (self.path / relpath).read_bytes()
        return self.zip.read(f"{self.folder}/{relpath}")

    # -- paper geometry
    def _paper_attrs(self):
        # Older notes used GLModel.PaperAttributes; newer notes use Notability.PaperAttributes.
        return (self.arc.find("Notability.PaperAttributes")
                or self.arc.find("GLModel.PaperAttributes"))

    def note_width(self):
        pa = self._paper_attrs()
        if pa:
            sizing = self.arc.deref(pa.get("paperSizingBehavior", ""))
            if isinstance(sizing, dict):
                sizing = sizing.get("NS.string", "")
            if isinstance(sizing, str) and sizing.startswith("lockedWidth:"):
                try:
                    return float(sizing.split(":")[1])
                except ValueError:
                    pass
        return DEFAULT_NOTE_WIDTH

    def paper_page_height(self, width):
        pa = self._paper_attrs()
        aspect = LETTER_ASPECT
        if pa:
            orient = self.arc.deref(pa.get("paperOrientation", "portrait"))
            if isinstance(orient, dict):
                orient = orient.get("NS.string", "portrait")
            if orient == "landscape":
                aspect = 1.0 / LETTER_ASPECT
        return width * aspect

    # -- paper style (lines / grid / dots + optional custom template PDF)
    def _paper_attr(self, key):
        pa = self._paper_attrs()
        if not pa or key not in pa:
            return None
        v = self.arc.deref(pa[key])
        if isinstance(v, dict):
            v = v.get("NS.string", "")
        return v if isinstance(v, str) else None

    def paper_line_style(self):
        """Return (kind, multiplier) where kind is 'lines'|'grid'|'dots'|'none'."""
        raw = self._paper_attr("lineStyle2") or ""
        if not raw or raw == "No Lines":
            return ("none", 1.0)
        parts = raw.split(":")
        head = parts[0].lower()
        if head not in ("lines", "grid", "dots"):
            return ("none", 1.0)
        # 'Lines:1.0' -> mult 1.0
        # 'Lines:false:false:0.5' -> last numeric field is the size in inches
        mult = 1.0
        for tail in reversed(parts[1:]):
            try:
                mult = float(tail)
                break
            except ValueError:
                continue
        return (head, mult)

    def paper_template_uuid(self):
        """UUID of a custom-template PDF stored inside the .nbn under PDFs/, or None."""
        raw = self._paper_attr("paperIdentifier") or ""
        if raw.startswith("TemplatePDF:"):
            return raw.split(":", 2)[1]
        return None

    # -- strokes
    def strokes(self):
        ish = self.arc.find("InkedSpatialHash")
        if not ish or not ish.get("curvespoints"):
            return []
        raw_pts = self.arc.deref(ish["curvespoints"])
        raw_num = self.arc.deref(ish["curvesnumpoints"])
        raw_wid = self.arc.deref(ish["curveswidth"])
        raw_col = self.arc.deref(ish["curvescolors"])
        pts = struct.unpack(f"<{len(raw_pts) // 4}f", raw_pts)
        nums = struct.unpack(f"<{len(raw_num) // 4}i", raw_num)
        widths = struct.unpack(f"<{len(raw_wid) // 4}f", raw_wid)
        colors = [tuple(raw_col[i : i + 4]) for i in range(0, len(raw_col), 4)]
        points = [(pts[i], pts[i + 1]) for i in range(0, len(pts), 2)]
        if len(points) != sum(nums):
            self.warnings.append(
                f"stroke point count mismatch ({len(points)} vs {sum(nums)}); rendering best-effort"
            )
        out, idx = [], 0
        for k, n in enumerate(nums):
            chunk = points[idx : idx + n]
            idx += n
            if not chunk:
                continue
            out.append(Stroke(chunk, widths[k] if k < len(widths) else 1.0,
                              colors[k] if k < len(colors) else (0, 0, 0, 255)))
        return out

    # -- images
    def images(self):
        fs = self.arc.find("FormattedString")
        if not fs or "mediaObjects" not in fs:
            return []
        media = self.arc.resolve(fs["mediaObjects"]) or []
        out = []
        for mo in media:
            if not isinstance(mo, dict) or mo.get("__class") != "ImageMediaObject":
                if isinstance(mo, dict) and mo.get("__class") != "TextBlockMediaObject":
                    self.warnings.append(f"skipping media object {mo.get('__class')}")
                continue
            try:
                fig = mo["figure"]
                rel = fig["FigureBackgroundObjectKey"]["kImageObjectSnapshotKey"]["relativePath"]
                data = self.read_member(rel)
                img = Image.open(io.BytesIO(data))
                crop = parse_rect(fig.get("FigureCropRectKey", "{{0, 0}, {0, 0}}"))
                cx, cy, cw, ch = crop
                if cw > 0 and ch > 0 and (cw < img.width or ch < img.height or cx or cy):
                    img = img.crop((int(cx), int(cy), int(cx + cw), int(cy + ch)))
                rot = float(mo.get("rotationDegrees") or 0.0)
                if rot:
                    img = img.rotate(-rot, expand=True, resample=Image.BICUBIC)
                x, y = parse_point(mo.get("documentContentOrigin") or mo["documentOrigin"])
                w, h = parse_point(mo["unscaledContentSize"])
                if rot:
                    # expand box around the same center after rotation
                    rad = math.radians(rot)
                    new_w = abs(w * math.cos(rad)) + abs(h * math.sin(rad))
                    new_h = abs(w * math.sin(rad)) + abs(h * math.cos(rad))
                    x -= (new_w - w) / 2
                    y -= (new_h - h) / 2
                    w, h = new_w, new_h
                buf = io.BytesIO()
                img.convert("RGBA").save(buf, "PNG")
                out.append(Placement(buf.getvalue(), x, y, w, h))
            except Exception as e:  # keep converting the rest of the note
                self.warnings.append(f"image skipped: {e}")
        return out

    # -- typed text boxes
    def text_blocks(self):
        fs = self.arc.find("FormattedString")
        if not fs or "mediaObjects" not in fs:
            return []
        media = self.arc.resolve(fs["mediaObjects"]) or []
        out = []
        for mo in media:
            if not isinstance(mo, dict):
                continue
            kind = mo.get("__class")
            try:
                rgb, size = (0.0, 0.0, 0.0), 12.0
                if kind == "TextBlockMediaObject":
                    att = (mo.get("textStore") or {}).get("attributedString") or {}
                    text = att.get("stringKey", "")
                    if not text.strip():
                        continue
                    runs = att.get("subRangesKey") or []
                    if runs and isinstance(runs[0], dict):
                        comps = runs[0].get("subRangeColorCrossPlatformKey")
                        if isinstance(comps, str):
                            rgb = tuple(float(t) for t in comps.split(","))[:3]
                        font = runs[0].get("subRangeFontKey") or {}
                        size = float(font.get("NSFontSizeAttribute", 12.0))
                elif kind == "Notability.MathMediaObject":
                    # LaTeX source has no pre-rendered image; preserve it verbatim
                    text = mo.get("latex", "")
                    if not text.strip():
                        continue
                    comps = mo.get("color")
                    if isinstance(comps, str):
                        rgb = tuple(float(t) for t in comps.split(","))[:3]
                    size = 11.0
                else:
                    continue
                x, y = parse_point(mo.get("documentContentOrigin") or mo["documentOrigin"])
                w, h = parse_point(mo["unscaledContentSize"])
                out.append(TextBlock(text, x, y, w, h, rgb, size))
            except Exception as e:
                self.warnings.append(f"text block skipped: {e}")
        return out

    # -- page layout
    def pages(self, width, content_ymax):
        fs = self.arc.find("FormattedString")
        layout = []
        if fs and "pageLayoutArray" in fs:
            layout = self.arc.resolve(fs["pageLayoutArray"]) or []
        if not layout:
            model = self.arc.find("Notability.NoteDocumentPaperLayoutModel")
            if model and "pageLayoutArray" in model:
                layout = self.arc.resolve(model["pageLayoutArray"]) or []

        pages = []
        if layout:
            layout = sorted(
                (e for e in layout if isinstance(e, dict)),
                key=lambda e: e.get("kPageLayoutDocumentPageNumberKey", 0),
            )
            for e in layout:
                pdf_name = e.get("kPageLayoutPDFFileNameKey")
                pdf_page = e.get("kPageLayoutPDFPageNumberKey")
                if pdf_name and pdf_page:
                    pages.append(Page(pdf_name, int(pdf_page) - 1))
                else:
                    pages.append(Page())
        if not pages:
            paper_h = self.paper_page_height(width)
            n = max(1, math.ceil((content_ymax + 8) / paper_h)) if content_ymax else 1
            pages = [Page() for _ in range(n)]
        return pages

    def recordings(self):
        exts = (".m4a", ".caf", ".aac", ".mp3", ".wav")
        if self.zip is None:
            rec_dir = self.path / "Recordings"
            if not rec_dir.exists():
                return []
            return [str(p.relative_to(self.path.parent)) for p in rec_dir.iterdir()
                    if p.is_file() and p.suffix.lower() in exts]
        prefix = f"{self.folder}/Recordings/"
        return [
            n for n in self.zip.namelist()
            if n.startswith(prefix) and n.lower().endswith(exts)
        ]


# ------------------------------------------------------------------ rendering

# College-ruled blue, close to Notability's on-screen ruled line
PAPER_LINE_RGB = (0.62, 0.78, 0.92)
# 24pt = ~1/3 inch — matches Notability's default ruled spacing at multiplier 1.0
PAPER_LINE_BASE = 24.0


def draw_paper_pattern(page, kind, mult, width, height):
    """Overlay lines/grid/dots on a blank page."""
    if kind == "none":
        return
    # 'Lines:1.0' → 24pt; 'Lines:false:false:0.5' → 0.5 inch = 36pt
    if 0 < mult <= 2.0:
        spacing = PAPER_LINE_BASE * mult
    else:
        spacing = mult * 72.0  # value was in inches
    if spacing < 6:
        spacing = 6
    shape = page.new_shape()
    if kind == "lines":
        y = spacing
        while y < height:
            shape.draw_line(fitz.Point(0, y), fitz.Point(width, y))
            y += spacing
        shape.finish(color=PAPER_LINE_RGB, width=0.4, stroke_opacity=1.0)
    elif kind == "grid":
        y = spacing
        while y < height:
            shape.draw_line(fitz.Point(0, y), fitz.Point(width, y))
            y += spacing
        x = spacing
        while x < width:
            shape.draw_line(fitz.Point(x, 0), fitz.Point(x, height))
            x += spacing
        shape.finish(color=PAPER_LINE_RGB, width=0.4, stroke_opacity=1.0)
    elif kind == "dots":
        r = 0.6
        y = spacing
        while y < height:
            x = spacing
            while x < width:
                shape.draw_circle(fitz.Point(x, y), r)
                x += spacing
            y += spacing
        shape.finish(color=PAPER_LINE_RGB, fill=PAPER_LINE_RGB, width=0)
    shape.commit()


def convert_note(note_path: Path, out_pdf: Path, extract_audio=False):
    note = Note(note_path)
    width = note.note_width()
    strokes = note.strokes()
    images = note.images()
    texts = note.text_blocks()

    content_ymax = 0.0
    for s in strokes:
        content_ymax = max(content_ymax, s.ymax)
    for im in images:
        content_ymax = max(content_ymax, im.y + im.h)
    for tb in texts:
        content_ymax = max(content_ymax, tb.y + tb.h)

    pages = note.pages(width, content_ymax)

    # open embedded PDFs once
    pdf_docs = {}
    for pg in pages:
        if pg.pdf_name and pg.pdf_name not in pdf_docs:
            try:
                pdf_docs[pg.pdf_name] = fitz.open(
                    stream=note.read_member(f"PDFs/{pg.pdf_name}"), filetype="pdf"
                )
            except Exception as e:
                note.warnings.append(f"embedded pdf {pg.pdf_name} unreadable: {e}")
                pdf_docs[pg.pdf_name] = None

    # custom-template PDF background (paperIdentifier = TemplatePDF:UUID:...)
    template_doc = None
    template_uuid = note.paper_template_uuid()
    if template_uuid:
        try:
            template_doc = fitz.open(
                stream=note.read_member(f"PDFs/{template_uuid}.pdf"), filetype="pdf"
            )
        except Exception as e:
            note.warnings.append(f"paper template pdf {template_uuid} unreadable: {e}")

    line_kind, line_mult = note.paper_line_style()

    # page heights in note coordinates
    paper_h = note.paper_page_height(width)
    for pg in pages:
        src = pdf_docs.get(pg.pdf_name)
        if src is not None and pg.pdf_name and 0 <= pg.pdf_page < src.page_count:
            r = src[pg.pdf_page].rect
            pg.height = width * (r.height / r.width) if r.width else paper_h
        else:
            pg.height = paper_h

    doc = fitz.open()
    y0 = 0.0
    for pg in pages:
        src = pdf_docs.get(pg.pdf_name)
        has_pdf = src is not None and pg.pdf_name and 0 <= pg.pdf_page < src.page_count
        if has_pdf:
            src_rect = src[pg.pdf_page].rect
            scale = src_rect.width / width
            page = doc.new_page(width=src_rect.width, height=src_rect.height)
            page.show_pdf_page(page.rect, src, pg.pdf_page)
        else:
            scale = 1.0
            page = doc.new_page(width=width, height=pg.height)
            # Blank page: apply custom-template background (if any) and/or line pattern
            if template_doc is not None and template_doc.page_count > 0:
                page.show_pdf_page(page.rect, template_doc, 0)
            draw_paper_pattern(page, line_kind, line_mult, width, pg.height)

        y1 = y0 + pg.height

        for im in images:
            if im.y + im.h <= y0 or im.y >= y1:
                continue
            rect = fitz.Rect(
                im.x * scale, (im.y - y0) * scale,
                (im.x + im.w) * scale, (im.y + im.h - y0) * scale,
            )
            try:
                page.insert_image(rect, stream=im.png, keep_proportion=False)
            except Exception as e:
                note.warnings.append(f"image placement failed: {e}")

        for tb in texts:
            if tb.y + tb.h <= y0 or tb.y >= y1:
                continue
            # generous bottom padding so insert_textbox never drops trailing lines
            rect = fitz.Rect(
                tb.x * scale, (tb.y - y0) * scale,
                (tb.x + tb.w) * scale + 2, (tb.y + tb.h - y0) * scale + tb.size * scale * 2,
            )
            try:
                page.insert_textbox(rect, tb.text, fontname="helv",
                                    fontsize=tb.size * scale, color=tb.rgb)
            except Exception as e:
                note.warnings.append(f"text placement failed: {e}")

        shape = page.new_shape()
        for s in strokes:
            if s.ymax <= y0 or s.ymin >= y1:
                continue
            pts = [fitz.Point(x * scale, (y - y0) * scale) for x, y in s.points]
            r, g, b, a = s.rgba
            color = (r / 255.0, g / 255.0, b / 255.0)
            w = max(s.width * scale, 0.2)
            if len(pts) == 1:
                shape.draw_circle(pts[0], w / 2)
                shape.finish(color=color, fill=color, width=0,
                             fill_opacity=a / 255.0)
            else:
                shape.draw_polyline(pts)
                shape.finish(color=color, width=w, lineCap=1, lineJoin=1,
                             stroke_opacity=a / 255.0)
        shape.commit()
        y0 = y1

    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    doc.save(out_pdf, garbage=3, deflate=True)
    doc.close()
    for d in pdf_docs.values():
        if d is not None:
            d.close()
    if template_doc is not None:
        template_doc.close()

    audio_files = []
    if extract_audio:
        for member in note.recordings():
            target = out_pdf.parent / f"{out_pdf.stem}_{Path(member).name}"
            if note.zip is None:
                target.write_bytes((note.path.parent / member).read_bytes())
            else:
                target.write_bytes(note.zip.read(member))
            audio_files.append(target)

    if note.zip is not None:
        note.zip.close()
    return note.warnings, audio_files


# ------------------------------------------------------------------------ cli

def main():
    ap = argparse.ArgumentParser(description="Convert Notability .note files to PDF")
    ap.add_argument("input", type=Path, help=".note file or directory tree of them")
    ap.add_argument("output", type=Path, help="output directory (tree is mirrored)")
    ap.add_argument("--force", action="store_true", help="overwrite existing PDFs")
    ap.add_argument("--audio", action="store_true", help="also extract audio recordings")
    args = ap.parse_args()

    # A .nbn is a directory bundle; a .note is a zip file.
    if args.input.suffix == ".nbn" and args.input.is_dir():
        note_files = [args.input]
        base = args.input.parent
    elif args.input.is_file():
        note_files = [args.input]
        base = args.input.parent
    else:
        note_files = sorted(args.input.rglob("*.note"))
        # Also include .nbn bundles (which are directories, not files, so use glob differently)
        note_files += sorted(p for p in args.input.rglob("*.nbn") if p.is_dir())
        base = args.input
    if not note_files:
        sys.exit(f"no .note or .nbn files found under {args.input}")

    ok = skipped = failed = 0
    for i, nf in enumerate(note_files, 1):
        rel = nf.relative_to(base).with_suffix(".pdf")
        out = args.output / rel
        if out.exists() and not args.force:
            skipped += 1
            continue
        try:
            warnings, _ = convert_note(nf, out, extract_audio=args.audio)
            ok += 1
            status = f"[{i}/{len(note_files)}] ok  {rel}"
            if warnings:
                status += f"  ({'; '.join(warnings[:3])})"
            print(status)
        except Exception as e:
            failed += 1
            print(f"[{i}/{len(note_files)}] FAIL {rel}: {e}", file=sys.stderr)

    print(f"\ndone: {ok} converted, {skipped} skipped (already exist), {failed} failed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
