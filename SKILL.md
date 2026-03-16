---
name: md-to-pdf
description: >
  Convert Markdown documents (with Mermaid diagrams) to professional PDF with
  vector-quality inline SVG diagrams and precise PDF bookmarks. Use whenever the
  user asks to convert, export, or render a .md file to PDF — especially if
  the markdown contains Mermaid code blocks (flowchart, sequence, mindmap,
  block-beta, state, class diagrams). Also trigger when the user says: "转PDF",
  "导出PDF", "markdown to pdf", "生成PDF", "md转pdf", "打印文档", "export document",
  "print to PDF", or asks for a "printable version" of a markdown doc.
---

# Markdown → Professional PDF (with Mermaid)

## When to Use

- User provides a `.md` file and asks for a PDF
- The markdown contains ` ```mermaid ` blocks (any diagram type)
- User wants diagrams rendered correctly (not shown as raw code)
- User says: "转PDF", "导出PDF", "markdown to pdf", "生成PDF", "md转pdf", "打印文档"

## Workflow

### Step 1 — Run the conversion script

```bash
python3 scripts/md_to_pdf.py <input.md>
```

Default output: `md-to-pdf/<stem>/<stem>.pdf` (under current working directory), with Mermaid SVGs in `md-to-pdf/<stem>/mermaid/`.

Optional flags:
- Second positional arg — explicit output .pdf path (overrides default)
- `--img-dir <dir>` — where to store rendered SVGs (default: `<output_dir>/mermaid`)
- `--max-size <MB>` — max PDF size warning threshold (default: 10)

The script handles everything automatically:
1. Extracts all ` ```mermaid ` blocks and renders each to SVG via `mmdc`
2. **Inlines SVG** directly into the HTML (vector quality — no rasterization, sharp at any zoom)
3. Each inline SVG gets a unique ID (`mmd-00`, `mmd-01`, ...) to avoid CSS conflicts
4. Injects `<a id="slug">` anchors before every heading (h1–h6) with hidden links to force Chromium to generate named destinations for all headings
5. Converts processed markdown → PDF via `md-to-pdf` npm package (Chromium-based)
6. Adds page numbers (footer: page / total)
7. **Generates precise PDF bookmarks** by reading Chromium's named destinations — all headings get exact page numbers (with character-position estimation as fallback)
8. Clears the PDF `/Title` metadata so Adobe Reader displays the OS filename

### Step 2 — Verify output

Check the PDF file size is under 10 MB:

```bash
ls -lh <output.pdf>
```

If any diagrams failed (logged as `[WARN]`), inspect the `.mmd` files in `img-dir` and fix the diagram syntax manually, then re-run.

### Step 3 — Deliver

Report the output path and file size to the user.

## Known Limitations

| Issue | Cause | Fix |
|-------|-------|-----|
| Very tall diagrams display as full-height images | Chromium renders full image, no cropping | Expected — CSS constrains to 250mm max-height per page |
| PDF > 10 MB | Many large SVG diagrams | Rare with inline SVG; consider splitting the document |

## Dependencies

Managed via `package.json`:
- `@mermaid-js/mermaid-cli` — renders `.mmd` files to SVG via `mmdc`
- `md-to-pdf` — converts markdown to PDF via Chromium
- `pypdf` — PDF bookmarks and metadata (Python, `pip3 install pypdf`; gracefully skipped if unavailable)

The script auto-detects a local Chrome/Chromium binary and also respects
`PUPPETEER_EXECUTABLE_PATH`, `CHROME_PATH`, and `GOOGLE_CHROME_BIN`.

Install with:
```bash
cd <repo-root> && npm install
```
