# notability2pdf

Convert Notability `.note` backup files (e.g. from Google Drive auto-backup)
to PDF — no Notability app required.

## Usage

```sh
./convert.py ~/Downloads/Notability ~/Downloads/Notability-PDF
```

Requires [uv](https://docs.astral.sh/uv/); dependencies (PyMuPDF, Pillow) are
resolved automatically from the script header. Pass a single `.note` file or a
directory tree — the folder structure is mirrored into the output directory.

Flags:

- `--force` — overwrite PDFs that already exist (default: skip them, so reruns are incremental)
- `--audio` — also extract audio recordings (`.m4a` etc.) next to each PDF

## How it works

A `.note` file is a zip archive:

```
NoteName/
  Session.plist        # NSKeyedArchiver: strokes, page layout, image placement
  metadata.plist
  PDFs/<uuid>.pdf      # imported PDF, if the note annotates one
  Images/*.png         # pasted images
  Recordings/*.m4a     # audio
  thumb*.png
```

Inside `Session.plist`:

- **Strokes** live in an `InkedSpatialHash` object as flat little-endian
  arrays: `curvespoints` (float32 x,y pairs), `curvesnumpoints` (int32 points
  per stroke), `curveswidth` (float32), `curvescolors` (RGBA bytes).
- **Page layout** is `FormattedString.pageLayoutArray`: each entry maps a
  document page to a page of an embedded PDF (or blank paper). An empty array
  means plain paper, paginated implicitly.
- **Coordinates**: the note is one continuous vertical strip. Its width is
  locked (see `GLModel.PaperAttributes.paperSizingBehavior`, e.g.
  `lockedWidth:572.0:Mac`); each PDF page is scaled to that width, so page *k*
  spans `[sum(heights of pages < k), +height)` and stroke/image positions are
  mapped into pages by their y coordinate.
- **Images** are `ImageMediaObject`s with `documentContentOrigin`,
  `unscaledContentSize`, a crop rect in source pixels, and a `relativePath`
  into `Images/`.

Verified against `sessionFormatVersion` 7, 8 and 9 (Notability 11.x–14.x)
across a corpus of 941 notes.

Handled media objects: imported PDFs, pasted images (with crop + rotation),
and typed text boxes (`TextBlockMediaObject`). Math boxes
(`Notability.MathMediaObject`) store LaTeX source with no pre-rendered image,
so the LaTeX string is placed verbatim rather than typeset. Any other media
type prints a warning and is skipped; the rest of the note still converts.
