# notability2pdf

Convert Notability notes to PDF — no Notability app required.
Handles both formats:

- **`.note`** — zipped export (e.g. from Google Drive auto-backup, or manual
  "Export As → Note" on iPad/Mac).
- **`.nbn`** — Notability's *native* on-disk bundle (a directory), as used by
  Notability's built-in iCloud Sync, so you can convert directly from
  Notability's iCloud container on macOS without first setting up a
  third-party auto-backup destination.

## Usage

```sh
# .note zips or .nbn bundles — batch a whole tree at once
./convert.py ~/Downloads/Notability ~/Downloads/Notability-PDF

# Or point straight at Notability's iCloud sync folder on macOS
./convert.py \
  "$HOME/Library/Mobile Documents/<TEAM_ID>~com~gingerlabs~Notability/Documents" \
  ~/Documents/notability-export
```

The `<TEAM_ID>` prefix (e.g. `ZP9ZJ4EF3S`) can be discovered with:

```sh
ls "$HOME/Library/Mobile Documents/" | grep gingerlabs
```

Requires [uv](https://docs.astral.sh/uv/); dependencies (PyMuPDF, Pillow) are
resolved automatically from the script header. If your default Python is 3.14
free-threaded (`Py_GIL_DISABLED=1`), PyMuPDF won't build — pin to 3.12:

```sh
uv run --python 3.12 --script convert.py …
```

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
