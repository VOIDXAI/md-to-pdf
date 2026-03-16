# `md-to-pdf`

Convert Markdown documents with Mermaid diagrams into professional PDF output.

This project is optimized for engineering documents rather than slide decks:

- Mermaid diagrams render to inline SVG for crisp PDF output.
- Markdown headings become PDF bookmarks.
- Relative Markdown assets keep working after preprocessing.
- Chinese and mixed CJK documents render with sensible defaults.
- Chrome/Chromium is auto-detected at runtime.

## Why this exists

Generic Markdown-to-PDF tools are often good enough until a document contains a
mix of Mermaid, local assets, CJK text, and heading-heavy structure.

This tool wraps `md-to-pdf` and Mermaid CLI with a few reliability features that
are especially useful for internal docs, RFCs, design notes, and printable
technical specifications.

## Features

- Converts `.md` to PDF with Chromium-based rendering
- Renders Mermaid code fences, including indented fences inside list items
- Inlines Mermaid SVGs to preserve vector quality
- Preserves relative Markdown and HTML asset references
- Adds page numbers to every page
- Generates PDF bookmarks from Markdown headings
- Clears PDF `/Title` metadata so PDF viewers show the filename
- Falls back gracefully when `pypdf` is unavailable

## Project layout

- `scripts/md_to_pdf.py`: main conversion entry point
- `templates/technical.css`: PDF stylesheet
- `templates/mermaid-config.json`: Mermaid theme and font settings
- `puppeteer-config.json`: Chromium launch defaults
- `tests/test_md_to_pdf.py`: regression tests

## Requirements

- Python `3.10+`
- Node.js `18+`
- `npm`
- A working Chrome or Chromium installation, or a CI step that installs one

The script also respects these environment variables:

- `PUPPETEER_EXECUTABLE_PATH`
- `CHROME_PATH`
- `GOOGLE_CHROME_BIN`

## Install

```bash
cd <repo-root>
npm ci
pip install -r requirements-test.txt
```

`requirements-test.txt` contains `pypdf`, which is optional for conversion but
required for the test suite and bookmark assertions.

## Usage

```bash
python3 scripts/md_to_pdf.py input.md
python3 scripts/md_to_pdf.py input.md output.pdf
python3 scripts/md_to_pdf.py input.md output.pdf --img-dir build/mermaid --max-size 12
```

Default output goes to:

```text
md-to-pdf/<stem>/<stem>.pdf
```

Rendered Mermaid SVG files are written to:

```text
md-to-pdf/<stem>/mermaid/
```

## Example

````markdown
# Example

![Architecture](./assets/architecture.svg)

1. Request flow
   ```mermaid
   graph TD
       Client --> Gateway
       Gateway --> Service
   ```
````

The generated PDF keeps the local image, renders the indented Mermaid block, and
creates a bookmark for `Example`.

## Testing

Run the regression suite:

```bash
python3 -m unittest discover -s tests -v
```

The suite covers:

- nested Mermaid fences
- relative asset preservation
- browser executable detection precedence

## CI

GitHub Actions is configured in `.github/workflows/ci.yml`.

The workflow:

- installs Node.js and Python
- installs Chrome for Testing
- runs `npm ci`
- installs Python test dependencies
- runs the unit and end-to-end regression suite

## Positioning

Compared with general-purpose tools such as `md-to-pdf` or `marp-cli`, this
project is narrower but stronger for printable engineering documents that mix
Mermaid, local assets, and heading-driven navigation.
