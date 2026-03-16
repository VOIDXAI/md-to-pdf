#!/usr/bin/env python3
"""
md_to_pdf.py — Convert a Markdown file with Mermaid diagrams to a professional PDF.

Usage:
    python md_to_pdf.py <input.md> [output.pdf] [--max-size <MB>]

Steps:
  1. Extract every ```mermaid ... ``` block and render to SVG via mmdc
  2. Replace mermaid fences with image references in a processed .md
  3. Convert processed markdown → PDF via md-to-pdf (Chromium-based)
  4. Add PDF bookmarks from markdown headings
  5. Clear PDF /Title metadata

Dependencies:
  - @mermaid-js/mermaid-cli  (npm — provides mmdc)
  - md-to-pdf                (npm)
  - pypdf                    (pip3 install pypdf — optional, for bookmarks & metadata)
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse

try:
    from pypdf import PdfWriter, PdfReader
    _PYPDF_OK = True
except ImportError:
    _PYPDF_OK = False

# ── Tool path resolution ─────────────────────────────────────────────────────
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_SKILL_DIR = os.path.dirname(_SCRIPT_DIR)
_NODE_BIN = os.path.join(_SKILL_DIR, "node_modules", ".bin")
_PUPPETEER_CFG = os.path.join(_SKILL_DIR, "puppeteer-config.json")
_MERMAID_CFG = os.path.join(_SKILL_DIR, "templates", "mermaid-config.json")
_STYLESHEET = os.path.join(_SKILL_DIR, "templates", "technical.css")


def _find_tool(name):
    """Find a tool in local node_modules/.bin, fall back to PATH."""
    local = os.path.join(_NODE_BIN, name)
    return local if os.path.isfile(local) and os.access(local, os.X_OK) else name


MERMAID_RE = re.compile(
    r'^(?P<indent>[ \t]*)```mermaid[ \t]*\r?\n(?P<body>.*?)(?P=indent)```[ \t]*$',
    re.DOTALL | re.MULTILINE,
)
HEADING_RE = re.compile(r'^(#{1,6})\s+(.+)$', re.MULTILINE)
FENCE_RE = re.compile(r'(^[ \t]*```[^\n]*\n.*?^[ \t]*```[ \t]*$)', re.DOTALL | re.MULTILINE)
MARKDOWN_LINK_RE = re.compile(
    r'(?P<prefix>!?\[[^\]]*\]\()(?P<url><[^>]+>|[^)\s]+)'
    r'(?P<suffix>(?:\s+(?:"[^"]*"|\'[^\']*\'|\([^)]*\)))?\))'
)
LINK_DEF_RE = re.compile(
    r'^(?P<prefix>[ \t]{0,3}\[[^\]]+\]:[ \t]*)(?P<url><[^>]+>|[^\s]+)(?P<suffix>.*)$',
    re.MULTILINE,
)
HTML_URL_ATTR_RE = re.compile(
    r'(?P<attr>\b(?:src|href)\s*=\s*)(?P<quote>["\'])(?P<url>.*?)(?P=quote)',
    re.IGNORECASE,
)


def _sub_outside_fences(pattern, repl, src):
    """Apply re.sub only to text outside fenced code blocks."""
    parts = FENCE_RE.split(src)
    # parts[0::2] = non-code, parts[1::2] = fenced code blocks
    for i in range(0, len(parts), 2):
        parts[i] = pattern.sub(repl, parts[i])
    return ''.join(parts)


def _finditer_outside_fences(pattern, src):
    """Yield regex matches only from text outside fenced code blocks."""
    parts = FENCE_RE.split(src)
    offset = 0
    for i, part in enumerate(parts):
        if i % 2 == 0:  # non-code region
            for m in pattern.finditer(part):
                yield m, offset + m.start()
        offset += len(part)


def _resolve_executable(candidate):
    """Resolve an executable path or command name to an existing executable."""
    if not candidate:
        return None
    expanded = os.path.expanduser(candidate)
    if os.path.isabs(expanded):
        return expanded if os.path.isfile(expanded) and os.access(expanded, os.X_OK) else None
    resolved = shutil.which(expanded)
    return resolved if resolved else None


def load_launch_options():
    """Load launch options and normalize browser executable discovery."""
    launch_opts = {}
    if os.path.isfile(_PUPPETEER_CFG):
        with open(_PUPPETEER_CFG, encoding="utf-8") as f:
            launch_opts = json.load(f)

    candidates = [
        os.environ.get("PUPPETEER_EXECUTABLE_PATH"),
        os.environ.get("CHROME_PATH"),
        os.environ.get("GOOGLE_CHROME_BIN"),
        launch_opts.get("executablePath"),
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/snap/bin/chromium",
    ]
    for candidate in candidates:
        resolved = _resolve_executable(candidate)
        if resolved:
            launch_opts["executablePath"] = resolved
            break
    else:
        launch_opts.pop("executablePath", None)

    return launch_opts


def write_puppeteer_config(launch_opts):
    """Create a temporary Puppeteer config file for Mermaid CLI."""
    if not launch_opts:
        return None
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", suffix=".json", prefix="md-to-pdf-puppeteer-", delete=False
    ) as f:
        json.dump(launch_opts, f, ensure_ascii=False)
        f.write("\n")
        return f.name


def _normalize_fenced_body(body, indent):
    """Remove the fence indentation from fenced block content."""
    lines = body.splitlines(True)
    normalized = []
    for line in lines:
        normalized.append(line[len(indent):] if indent and line.startswith(indent) else line)
    return "".join(normalized)


def _indent_block(text, indent):
    """Apply a prefix to every line so replacements stay inside list items."""
    if not indent:
        return text
    return "".join(f"{indent}{line}" for line in text.splitlines(True))


def _rewrite_relative_url(url, src_path, proc_md):
    """Rewrite a relative URL so it resolves from md-to-pdf's virtual base URL."""
    wrapped = url.startswith("<") and url.endswith(">")
    raw_url = url[1:-1] if wrapped else url
    parsed = urllib.parse.urlsplit(raw_url)
    if parsed.scheme or parsed.netloc or raw_url.startswith("#") or parsed.path.startswith("/"):
        return url

    target_path = os.path.normpath(
        os.path.join(os.path.dirname(src_path), urllib.parse.unquote(parsed.path))
    )
    rewritten_path = os.path.relpath(target_path, start=proc_md).replace(os.path.sep, "/")
    rewritten = urllib.parse.urlunsplit(("", "", rewritten_path, parsed.query, parsed.fragment))
    return f"<{rewritten}>" if wrapped else rewritten


def rewrite_relative_urls(src: str, src_path: str, proc_md: str) -> str:
    """Rewrite markdown and HTML relative URLs outside fenced code blocks."""

    def _replace_markdown(m):
        url = m.group("url")
        return f"{m.group('prefix')}{_rewrite_relative_url(url, src_path, proc_md)}{m.group('suffix')}"

    def _replace_link_def(m):
        url = m.group("url")
        return f"{m.group('prefix')}{_rewrite_relative_url(url, src_path, proc_md)}{m.group('suffix')}"

    def _replace_html_attr(m):
        url = m.group("url")
        return (
            f"{m.group('attr')}{m.group('quote')}"
            f"{_rewrite_relative_url(url, src_path, proc_md)}"
            f"{m.group('quote')}"
        )

    rewritten = _sub_outside_fences(MARKDOWN_LINK_RE, _replace_markdown, src)
    rewritten = _sub_outside_fences(LINK_DEF_RE, _replace_link_def, rewritten)
    rewritten = _sub_outside_fences(HTML_URL_ATTR_RE, _replace_html_attr, rewritten)
    return rewritten


def make_processing_paths(src_path: str, src_stem: str):
    """Create a temporary working directory and the narrowest safe md-to-pdf base dir."""
    src_dir = os.path.dirname(src_path)
    try:
        proc_root = tempfile.mkdtemp(prefix=".md-to-pdf-", dir=src_dir)
        basedir = src_dir
    except OSError:
        proc_root = tempfile.mkdtemp(prefix="md-to-pdf-")
        basedir = os.path.commonpath([src_dir, proc_root])
    proc_md = os.path.join(proc_root, f"processed_{src_stem}.md")
    return proc_root, proc_md, basedir


def render_diagrams(src: str, img_dir: str, puppeteer_cfg_path=None) -> str:
    """Replace mermaid fences with rendered SVG image references."""
    os.makedirs(img_dir, exist_ok=True)
    counter = [0]

    def replace(m):
        idx = counter[0]
        counter[0] += 1
        indent = m.group("indent")
        mermaid_body = _normalize_fenced_body(m.group("body"), indent)
        mmd_path = os.path.join(img_dir, f"d{idx:02d}.mmd")
        svg_path = os.path.join(img_dir, f"d{idx:02d}.svg")

        with open(mmd_path, "w", encoding="utf-8") as f:
            f.write(mermaid_body)

        mmdc_cmd = [_find_tool("mmdc"), "-i", mmd_path, "-o", svg_path,
                    "-b", "white"]
        if os.path.isfile(_MERMAID_CFG):
            mmdc_cmd.extend(["--configFile", _MERMAID_CFG])
        if puppeteer_cfg_path and os.path.isfile(puppeteer_cfg_path):
            mmdc_cmd.extend(["-p", puppeteer_cfg_path])

        result = subprocess.run(mmdc_cmd, capture_output=True, text=True)
        if result.returncode != 0 or not os.path.exists(svg_path):
            print(f"  [WARN] d{idx:02d} failed: {result.stderr[:120]}", flush=True)
            return m.group(0)

        print(f"  [OK] d{idx:02d}.svg", flush=True)
        # Read SVG and inline it for vector-quality rendering in PDF
        with open(svg_path, "r", encoding="utf-8") as sf:
            svg_content = sf.read()
        # Remove XML declaration if present, keep <svg> tag
        svg_content = re.sub(r'<\?xml[^?]*\?>\s*', '', svg_content)
        # Replace default "my-svg" id with unique per-diagram id to avoid
        # CSS selector conflicts when multiple SVGs are inlined in one page
        uid = f"mmd-{idx:02d}"
        svg_content = svg_content.replace("my-svg", uid)
        block = f"\n\n<div class='mermaid-diagram'>\n{svg_content}\n</div>\n\n"
        return _indent_block(block, indent)

    return MERMAID_RE.sub(replace, src)


def generate_pdf(proc_md, out_path, launch_opts, basedir=None):
    """Run md-to-pdf and move the result to out_path. Returns True on success."""
    pdf_options = {
        "format": "A4",
        "margin": {"top": "20mm", "bottom": "25mm", "left": "15mm", "right": "15mm"},
        "printBackground": True,
        "displayHeaderFooter": True,
        "headerTemplate": "<div></div>",
        "footerTemplate": (
            '<div style="font-size:9px;width:100%;text-align:center;color:#999">'
            '<span class="pageNumber"></span> / <span class="totalPages"></span>'
            '</div>'
        ),
    }

    cmd = [
        _find_tool("md-to-pdf"),
        "--stylesheet", _STYLESHEET,
        "--page-media-type", "print",
        "--pdf-options", json.dumps(pdf_options),
        "--launch-options", json.dumps(launch_opts),
    ]
    if basedir:
        cmd.extend(["--basedir", basedir])
    cmd.append(proc_md)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[ERROR] md-to-pdf failed:\n{result.stderr}", file=sys.stderr)
        return False

    generated_pdf = os.path.splitext(proc_md)[0] + ".pdf"
    if generated_pdf != out_path and os.path.exists(generated_pdf):
        os.replace(generated_pdf, out_path)
    if not os.path.exists(out_path):
        print("[ERROR] md-to-pdf succeeded but output file not found", file=sys.stderr)
        return False
    return True


def _slugify(text):
    """Generate a slug matching markdown-it's anchor ID generation."""
    text = text.lower().strip()
    # Remove markdown formatting: bold, italic, code, links
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'`(.+?)`', r'\1', text)
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    # Keep CJK, alphanumeric, spaces, hyphens
    text = re.sub(r'[^\w\u4e00-\u9fff\s-]', '', text)
    text = re.sub(r'[\s]+', '-', text)
    return text


def _make_unique_slug(slug, seen):
    """Append -1, -2, ... suffix to deduplicate slugs (markdown-it behavior)."""
    if slug not in seen:
        seen[slug] = 0
        return slug
    seen[slug] += 1
    unique = f"{slug}-{seen[slug]}"
    while unique in seen:
        seen[slug] += 1
        unique = f"{slug}-{seen[slug]}"
    seen[unique] = 0
    return unique


def inject_heading_anchors(src):
    """Inject <a id="slug"></a> before each heading AND append a hidden link
    block so Chromium generates named destinations for ALL headings.
    Only processes headings outside fenced code blocks."""
    seen = {}
    slugs = []

    def _add_anchor(m):
        hashes = m.group(1)
        title = m.group(2).strip()
        slug = _make_unique_slug(_slugify(title), seen)
        slugs.append(slug)
        return f'<a id="{slug}"></a>\n{hashes} {title}'

    result = _sub_outside_fences(HEADING_RE, _add_anchor, src)

    # Chromium only creates named destinations for anchors targeted by links.
    # Append a hidden div with links to every heading anchor to force generation.
    links = ''.join(f'<a href="#{s}"> </a>' for s in slugs)
    result += f'\n\n<div style="height:0;overflow:hidden;font-size:0">{links}</div>\n'
    return result


def add_bookmarks(out_path, src):
    """Add PDF bookmarks using precise page numbers from named destinations."""
    if not _PYPDF_OK:
        return
    try:
        headings = []
        seen = {}
        src_len = len(src)
        for m, abs_pos in _finditer_outside_fences(HEADING_RE, src):
            level = len(m.group(1))
            title = m.group(2).strip()
            slug = _make_unique_slug(_slugify(title), seen)
            headings.append((level, title, slug, abs_pos))
        if not headings:
            return

        reader = PdfReader(out_path)
        total_pages = len(reader.pages)
        if total_pages == 0:
            return

        # Build slug → page mapping from PDF named destinations
        # Use indirect_reference for reliable identity comparison across pypdf versions
        page_iref_to_idx = {}
        for i, page in enumerate(reader.pages):
            iref = page.indirect_reference
            if iref is not None:
                page_iref_to_idx[(iref.idnum, iref.generation)] = i

        dest_to_page = {}
        dests = reader.named_destinations
        for name, dest in dests.items():
            slug = urllib.parse.unquote(name.lstrip('/'))
            page_ref = dest.get('/Page')
            if page_ref is not None:
                iref = page_ref if hasattr(page_ref, 'idnum') else getattr(page_ref, 'indirect_reference', None)
                if iref is not None:
                    key = (iref.idnum, iref.generation)
                    if key in page_iref_to_idx:
                        dest_to_page[slug] = page_iref_to_idx[key]

        writer = PdfWriter()
        writer.append(reader)

        parent_stack = []  # [(level, bookmark_ref)]
        precise = 0

        for level, title, slug, char_pos in headings:
            page_num = dest_to_page.get(slug)
            if page_num is not None:
                precise += 1
            else:
                # Fallback: estimate page from character position
                page_num = min(int(char_pos / src_len * total_pages), total_pages - 1)

            while parent_stack and parent_stack[-1][0] >= level:
                parent_stack.pop()

            parent = parent_stack[-1][1] if parent_stack else None
            bookmark = writer.add_outline_item(title, page_num, parent=parent)
            parent_stack.append((level, bookmark))

        tmp_path = out_path + ".tmp"
        with open(tmp_path, "wb") as f:
            writer.write(f)
        os.replace(tmp_path, out_path)
        print(f"  Added {len(headings)} bookmarks ({precise} precise, {len(headings)-precise} estimated)", flush=True)
    except Exception as e:
        print(f"  [WARN] Bookmark generation failed: {e}", flush=True)


def clear_pdf_title(out_path):
    """Clear /Title metadata so Adobe Reader displays the OS filename."""
    if not _PYPDF_OK:
        return
    try:
        reader = PdfReader(out_path)
        writer = PdfWriter()
        writer.append(reader)
        writer.add_metadata({"/Title": ""})
        tmp_path = out_path + ".tmp"
        with open(tmp_path, "wb") as f:
            writer.write(f)
        os.replace(tmp_path, out_path)
    except Exception as e:
        print(f"  [WARN] Metadata clear failed: {e}", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Convert Markdown+Mermaid to PDF")
    parser.add_argument("input", help="Input .md file")
    parser.add_argument("output", nargs="?", default=None,
                        help="Output .pdf file (default: md-to-pdf/<stem>/<stem>.pdf)")
    parser.add_argument("--img-dir", default=None,
                        help="Directory for rendered SVGs (default: <output_dir>/mermaid)")
    parser.add_argument("--max-size", type=float, default=10.0,
                        help="Max PDF size in MB (default: 10)")
    args = parser.parse_args()

    src_path = os.path.abspath(args.input)
    src_stem = os.path.splitext(os.path.basename(src_path))[0]

    if args.output:
        out_path = os.path.abspath(args.output)
        out_dir = os.path.dirname(out_path)
    else:
        out_dir = os.path.join(os.getcwd(), "md-to-pdf", src_stem)
        out_path = os.path.join(out_dir, f"{src_stem}.pdf")

    img_dir = os.path.abspath(args.img_dir) if args.img_dir else os.path.join(out_dir, "mermaid")
    proc_root, proc_md, basedir = make_processing_paths(src_path, src_stem)

    # Clean previous output
    if not args.output and os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    elif os.path.isdir(img_dir):
        shutil.rmtree(img_dir)
    os.makedirs(out_dir, exist_ok=True)

    # Load puppeteer launch options
    launch_opts = load_launch_options()
    runtime_puppeteer_cfg = write_puppeteer_config(launch_opts)

    print(f"[1/3] Reading {src_path}", flush=True)
    with open(src_path, encoding="utf-8") as f:
        src = f.read()

    print("[2/3] Rendering Mermaid diagrams...", flush=True)
    try:
        processed = rewrite_relative_urls(src, src_path, proc_md)
        processed = inject_heading_anchors(processed)
        processed = render_diagrams(processed, img_dir, runtime_puppeteer_cfg)

        with open(proc_md, "w", encoding="utf-8") as f:
            f.write(processed)

        print(f"[3/3] Converting to PDF -> {out_path}", flush=True)
        if not generate_pdf(proc_md, out_path, launch_opts, basedir=basedir):
            sys.exit(1)

        size_mb = os.path.getsize(out_path) / 1024 / 1024
        if size_mb > args.max_size:
            print(f"  [WARN] PDF {size_mb:.1f} MB exceeds {args.max_size} MB limit", flush=True)

        add_bookmarks(out_path, src)
        clear_pdf_title(out_path)
        size_mb = os.path.getsize(out_path) / 1024 / 1024
        print(f"Done! {out_path}  ({size_mb:.1f} MB)", flush=True)
    finally:
        if runtime_puppeteer_cfg and os.path.exists(runtime_puppeteer_cfg):
            os.remove(runtime_puppeteer_cfg)
        if os.path.isdir(proc_root):
            shutil.rmtree(proc_root)


if __name__ == "__main__":
    main()
