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
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from dataclasses import dataclass

try:
    from pypdf import PdfWriter, PdfReader
    _PYPDF_OK = True
except ImportError:
    _PYPDF_OK = False

try:
    from markdown_it import MarkdownIt
    _MARKDOWN_IT_OK = True
except ImportError:
    _MARKDOWN_IT_OK = False

try:
    import yaml
    _YAML_OK = True
except ImportError:
    _YAML_OK = False

# ── Tool path resolution ─────────────────────────────────────────────────────
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_SKILL_DIR = os.path.dirname(_SCRIPT_DIR)
_NODE_BIN = os.path.join(_SKILL_DIR, "node_modules", ".bin")
_PUPPETEER_CFG = os.path.join(_SKILL_DIR, "puppeteer-config.json")
_MERMAID_CFG = os.path.join(_SKILL_DIR, "templates", "mermaid-config.json")
_STYLESHEET = os.path.join(_SKILL_DIR, "templates", "technical.css")
_DEFAULT_CACHE_DIR = os.path.join(
    os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")),
    "md-to-pdf",
    "mermaid",
)
_MERMAID_CACHE_SCHEMA = "v2"
_MERMAID_CLI_VERSION = None
DEFAULT_MERMAID_BATCH_SIZE = 4

DEFAULT_PAGE_MEDIA_TYPE = "print"
DEFAULT_PDF_OPTIONS = {
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


def _find_tool(name):
    """Find a tool in local node_modules/.bin, fall back to PATH."""
    local = os.path.join(_NODE_BIN, name)
    return local if os.path.isfile(local) and os.access(local, os.X_OK) else name


def _read_file_bytes(path):
    """Read a file as bytes when it exists, otherwise return empty bytes."""
    if path and os.path.isfile(path):
        with open(path, "rb") as f:
            return f.read()
    return b""


def _mermaid_cli_version():
    """Return the installed mermaid-cli version for cache invalidation."""
    global _MERMAID_CLI_VERSION
    if _MERMAID_CLI_VERSION is None:
        package_json = os.path.join(
            _SKILL_DIR, "node_modules", "@mermaid-js", "mermaid-cli", "package.json"
        )
        try:
            with open(package_json, encoding="utf-8") as f:
                _MERMAID_CLI_VERSION = json.load(f).get("version", "unknown")
        except Exception:
            _MERMAID_CLI_VERSION = "unknown"
    return _MERMAID_CLI_VERSION


def _normalize_cache_dir(value, base_dir):
    """Resolve cache_dir settings, allowing False/None to disable caching."""
    if value in (None, True):
        return _DEFAULT_CACHE_DIR
    if value in (False, ""):
        return None
    return _resolve_path(value, base_dir)


def _normalize_bool(value, default=False):
    """Normalize truthy configuration values from YAML/CLI."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return bool(value)


def _mermaid_cache_key(block_content, mermaid_config_bytes):
    """Compute a stable cache key for rendered Mermaid SVG output."""
    digest = hashlib.sha256()
    digest.update(_MERMAID_CACHE_SCHEMA.encode("utf-8"))
    digest.update(b"\0")
    digest.update(_mermaid_cli_version().encode("utf-8"))
    digest.update(b"\0")
    digest.update(mermaid_config_bytes)
    digest.update(b"\0")
    digest.update(block_content.encode("utf-8"))
    return digest.hexdigest()


def _mermaid_cache_path(cache_dir, cache_key):
    """Place cache entries into sharded subdirectories."""
    return os.path.join(cache_dir, cache_key[:2], f"{cache_key}.svg")


def _store_mermaid_cache(cache_dir, cache_key, svg_path):
    """Copy a freshly rendered SVG into the persistent cache."""
    if not cache_dir or not os.path.exists(svg_path):
        return
    cache_path = _mermaid_cache_path(cache_dir, cache_key)
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    shutil.copyfile(svg_path, cache_path)


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
FRONT_MATTER_DELIM_RE = re.compile(r'^\s*---\s*$')


@dataclass(frozen=True)
class Heading:
    level: int
    title: str
    slug: str
    char_pos: int
    start_line: int
    end_line: int


@dataclass(frozen=True)
class MermaidBlock:
    start_line: int
    end_line: int
    indent: str
    content: str


@dataclass(frozen=True)
class MarkdownStructure:
    headings: list
    mermaid_blocks: list
    code_ranges: list


def _require_runtime(name, available, install_hint):
    """Fail with a clear message when a required Python dependency is missing."""
    if available:
        return
    print(
        f"[ERROR] Missing Python dependency: {name}. Install it with: {install_hint}",
        file=sys.stderr,
    )
    sys.exit(1)


def ensure_runtime_dependencies():
    """Ensure required Python packages are available."""
    _require_runtime("markdown-it-py", _MARKDOWN_IT_OK, "pip install -r requirements.txt")


def _get_markdown_parser():
    """Create the CommonMark parser used for structural preprocessing."""
    ensure_runtime_dependencies()
    return MarkdownIt("commonmark", {"html": True})


def _line_offsets(src):
    """Return source lines and their start offsets."""
    lines = src.splitlines(True)
    offsets = []
    total = 0
    for line in lines:
        offsets.append(total)
        total += len(line)
    return lines, offsets


def _line_to_offset(offsets, src_len, line_idx):
    """Translate a line index to a character offset."""
    return offsets[line_idx] if line_idx < len(offsets) else src_len


def _merge_ranges(ranges):
    """Normalize and merge overlapping protected ranges."""
    if not ranges:
        return []
    ordered = sorted((start, end) for start, end in ranges if start < end)
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _find_inline_code_ranges(text):
    """Locate backtick-delimited inline code spans within a text chunk."""
    ranges = []
    cursor = 0
    length = len(text)

    while cursor < length:
        if text[cursor] != "`":
            cursor += 1
            continue

        tick_count = 1
        while cursor + tick_count < length and text[cursor + tick_count] == "`":
            tick_count += 1
        fence = "`" * tick_count

        search_pos = cursor + tick_count
        while True:
            closing = text.find(fence, search_pos)
            if closing == -1:
                cursor += tick_count
                break

            before = closing - 1
            after = closing + tick_count
            if (before < 0 or text[before] != "`") and (after >= length or text[after] != "`"):
                ranges.append((cursor, after))
                cursor = after
                break

            search_pos = closing + 1

    return ranges


def _sub_outside_ranges(pattern, repl, src, protected_ranges):
    """Apply re.sub only to text outside protected character ranges."""
    protected_ranges = _merge_ranges(protected_ranges)
    chunks = []
    cursor = 0
    for start, end in protected_ranges:
        if cursor < start:
            chunks.append(pattern.sub(repl, src[cursor:start]))
        chunks.append(src[start:end])
        cursor = end
    if cursor < len(src):
        chunks.append(pattern.sub(repl, src[cursor:]))
    return ''.join(chunks)


def _resolve_executable(candidate):
    """Resolve an executable path or command name to an existing executable."""
    if not candidate:
        return None
    expanded = os.path.expanduser(candidate)
    if os.path.isabs(expanded):
        return expanded if os.path.isfile(expanded) and os.access(expanded, os.X_OK) else None
    resolved = shutil.which(expanded)
    return resolved if resolved else None


def _deep_merge(base, override):
    """Recursively merge dictionaries without mutating the inputs."""
    if not isinstance(base, dict):
        base = {}
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _looks_like_url(value):
    """Return True when a string looks like an absolute URL."""
    parsed = urllib.parse.urlsplit(value)
    return bool(parsed.scheme and parsed.netloc)


def _normalize_list(value):
    """Normalize a scalar-or-list config value into a list."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _resolve_path(value, base_dir):
    """Resolve a possibly relative filesystem path against the markdown directory."""
    if not value or not isinstance(value, str):
        return value
    expanded = os.path.expanduser(value)
    if _looks_like_url(expanded) or os.path.isabs(expanded):
        return expanded
    return os.path.abspath(os.path.join(base_dir, expanded))


def _normalize_max_size(value):
    """Convert a max-size setting to float, ignoring invalid values."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        print(f"  [WARN] Ignoring invalid max_size value: {value!r}", flush=True)
        return None


def load_launch_options(config_path=None, inline_options=None):
    """Load launch options and normalize browser executable discovery."""
    launch_opts = {}
    config_path = config_path or _PUPPETEER_CFG
    if config_path and os.path.isfile(config_path):
        with open(config_path, encoding="utf-8") as f:
            launch_opts = json.load(f)
    launch_opts = _deep_merge(launch_opts, inline_options or {})

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


def parse_front_matter(src):
    """Split YAML front matter from the markdown body."""
    if not src:
        return "", {}, src
    lines = src.splitlines(True)
    if not lines or not FRONT_MATTER_DELIM_RE.match(lines[0]):
        return "", {}, src

    for index in range(1, len(lines)):
        if FRONT_MATTER_DELIM_RE.match(lines[index]) or lines[index].strip() == "...":
            raw = ''.join(lines[:index + 1])
            body = ''.join(lines[index + 1:])
            if not _YAML_OK:
                print("  [WARN] PyYAML not installed; front matter config ignored", flush=True)
                return raw, {}, body
            try:
                metadata = yaml.safe_load(''.join(lines[1:index])) or {}
                if not isinstance(metadata, dict):
                    print("  [WARN] Front matter must be a mapping; ignoring config", flush=True)
                    metadata = {}
                return raw, metadata, body
            except Exception as exc:
                print(f"  [WARN] Front matter parse failed: {exc}", flush=True)
                return raw, {}, body
    return "", {}, src


def parse_markdown_structure(src):
    """Collect headings, Mermaid fences, and protected code ranges via Markdown AST."""
    parser = _get_markdown_parser()
    tokens = parser.parse(src)
    lines, offsets = _line_offsets(src)
    src_len = len(src)
    seen = {}
    headings = []
    mermaid_blocks = []
    code_ranges = []

    for index, token in enumerate(tokens):
        token_map = token.map or []
        if token.type in ("fence", "code_block") and len(token_map) == 2:
            code_ranges.append(
                (
                    _line_to_offset(offsets, src_len, token_map[0]),
                    _line_to_offset(offsets, src_len, token_map[1]),
                )
            )

        if token.type == "fence" and len(token_map) == 2:
            info = token.info.strip().split()
            if info and info[0] == "mermaid":
                indent = ""
                if token_map[0] < len(lines):
                    indent = re.match(r"[ \t]*", lines[token_map[0]]).group(0)
                mermaid_blocks.append(
                    MermaidBlock(
                        start_line=token_map[0],
                        end_line=token_map[1],
                        indent=indent,
                        content=token.content,
                    )
                )

        if token.type == "heading_open" and len(token_map) == 2:
            inline = tokens[index + 1] if index + 1 < len(tokens) else None
            title = (inline.content if inline and inline.type == "inline" else "").strip()
            slug = _make_unique_slug(_slugify(title), seen)
            headings.append(
                Heading(
                    level=int(token.tag[1]),
                    title=title,
                    slug=slug,
                    char_pos=_line_to_offset(offsets, src_len, token_map[0]),
                    start_line=token_map[0],
                    end_line=token_map[1],
                )
            )

    return MarkdownStructure(
        headings=headings,
        mermaid_blocks=mermaid_blocks,
        code_ranges=_merge_ranges(code_ranges),
    )


def collect_headings(src):
    """Collect heading metadata via Markdown AST."""
    return parse_markdown_structure(src).headings


def build_render_settings(src_path, metadata, cli_args):
    """Merge defaults, front matter, and CLI arguments into render settings."""
    src_dir = os.path.dirname(src_path)
    wrapper_config = metadata.get("md_to_pdf", {}) if isinstance(metadata.get("md_to_pdf"), dict) else {}

    stylesheets = [_STYLESHEET]
    stylesheet_values = []
    stylesheet_values.extend(_normalize_list(metadata.get("stylesheet")))
    stylesheet_values.extend(_normalize_list(wrapper_config.get("stylesheet")))
    stylesheet_values.extend(_normalize_list(wrapper_config.get("stylesheets")))
    for stylesheet in stylesheet_values:
        resolved = _resolve_path(stylesheet, src_dir)
        if resolved and resolved not in stylesheets:
            stylesheets.append(resolved)

    launch_inline = _deep_merge(metadata.get("launch_options") or {}, wrapper_config.get("launch_options") or {})
    max_size = _normalize_max_size(wrapper_config.get("max_size"))
    performance_mode = _normalize_bool(wrapper_config.get("performance_mode"), default=False)

    settings = {
        "stylesheets": stylesheets,
        "page_media_type": (
            wrapper_config.get("page_media_type")
            or metadata.get("page_media_type")
            or DEFAULT_PAGE_MEDIA_TYPE
        ),
        "pdf_options": _deep_merge(
            DEFAULT_PDF_OPTIONS,
            _deep_merge(metadata.get("pdf_options") or {}, wrapper_config.get("pdf_options") or {}),
        ),
        "launch_options": load_launch_options(
            config_path=_resolve_path(wrapper_config.get("puppeteer_config"), src_dir) or _PUPPETEER_CFG,
            inline_options=launch_inline,
        ),
        "mermaid_config_path": _resolve_path(wrapper_config.get("mermaid_config"), src_dir) or _MERMAID_CFG,
        "cache_dir": _normalize_cache_dir(wrapper_config.get("cache_dir"), src_dir),
        "performance_mode": performance_mode,
        "mermaid_batch_size": None if performance_mode else DEFAULT_MERMAID_BATCH_SIZE,
        "img_dir": _resolve_path(wrapper_config.get("img_dir"), src_dir),
        "max_size": max_size,
    }

    if cli_args.img_dir:
        settings["img_dir"] = os.path.abspath(cli_args.img_dir)
    if cli_args.max_size is not None:
        settings["max_size"] = float(cli_args.max_size)
    if getattr(cli_args, "performance_mode", False):
        settings["performance_mode"] = True
        settings["mermaid_batch_size"] = None
    if settings["max_size"] is None:
        settings["max_size"] = 10.0
    return settings


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
    rewritten_path = os.path.relpath(target_path, start=os.path.dirname(proc_md)).replace(
        os.path.sep, "/"
    )
    rewritten = urllib.parse.urlunsplit(("", "", rewritten_path, parsed.query, parsed.fragment))
    return f"<{rewritten}>" if wrapped else rewritten


def rewrite_relative_urls(src: str, src_path: str, proc_md: str, structure=None) -> str:
    """Rewrite markdown and HTML relative URLs outside fenced code blocks."""
    structure = structure or parse_markdown_structure(src)

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

    def _rewrite_chunk(chunk):
        inline_ranges = _merge_ranges(_find_inline_code_ranges(chunk))
        pieces = []
        piece_cursor = 0
        for start, end in inline_ranges:
            if piece_cursor < start:
                segment = chunk[piece_cursor:start]
                segment = MARKDOWN_LINK_RE.sub(_replace_markdown, segment)
                segment = LINK_DEF_RE.sub(_replace_link_def, segment)
                segment = HTML_URL_ATTR_RE.sub(_replace_html_attr, segment)
                pieces.append(segment)
            pieces.append(chunk[start:end])
            piece_cursor = end
        if piece_cursor < len(chunk):
            segment = chunk[piece_cursor:]
            segment = MARKDOWN_LINK_RE.sub(_replace_markdown, segment)
            segment = LINK_DEF_RE.sub(_replace_link_def, segment)
            segment = HTML_URL_ATTR_RE.sub(_replace_html_attr, segment)
            pieces.append(segment)
        return ''.join(pieces)

    protected_ranges = _merge_ranges(structure.code_ranges)
    chunks = []
    cursor = 0
    for start, end in protected_ranges:
        if cursor < start:
            chunks.append(_rewrite_chunk(src[cursor:start]))
        chunks.append(src[start:end])
        cursor = end
    if cursor < len(src):
        chunks.append(_rewrite_chunk(src[cursor:]))
    return ''.join(chunks)


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


def _inline_svg_markup(svg_path, idx):
    """Read an SVG and rewrite its default IDs for safe inline embedding."""
    with open(svg_path, "r", encoding="utf-8") as sf:
        svg_content = sf.read()
    svg_content = re.sub(r'<\?xml[^?]*\?>\s*', '', svg_content)
    uid = f"mmd-{idx:02d}"
    svg_content = svg_content.replace('id="my-svg"', f'id="{uid}"')
    svg_content = svg_content.replace('#my-svg', f'#{uid}')
    return svg_content


def _render_single_mermaid_block(
    idx,
    block,
    img_dir,
    mermaid_config_path=None,
    puppeteer_cfg_path=None,
    cache_dir=None,
    cache_key=None,
):
    """Render one Mermaid block as a compatibility fallback."""
    svg_path = os.path.join(img_dir, f"d{idx:02d}.svg")
    mmdc_cmd = [_find_tool("mmdc"), "-i", os.path.join(img_dir, f"d{idx:02d}.mmd"), "-o", svg_path, "-b", "white"]
    cfg_path = mermaid_config_path or _MERMAID_CFG
    if cfg_path and os.path.isfile(cfg_path):
        mmdc_cmd.extend(["--configFile", cfg_path])
    if puppeteer_cfg_path and os.path.isfile(puppeteer_cfg_path):
        mmdc_cmd.extend(["-p", puppeteer_cfg_path])

    try:
        result = subprocess.run(mmdc_cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        print(f"  [WARN] d{idx:02d} timed out after 120s", flush=True)
        return False
    if result.returncode != 0 or not os.path.exists(svg_path):
        print(f"  [WARN] d{idx:02d} failed: {result.stderr[:120]}", flush=True)
        return False

    if cache_dir and cache_key:
        _store_mermaid_cache(cache_dir, cache_key, svg_path)
    print(f"  [OK] d{idx:02d}.svg", flush=True)
    return True


def _render_mermaid_batch(
    indices,
    blocks,
    img_dir,
    mermaid_config_path=None,
    puppeteer_cfg_path=None,
    cache_dir=None,
    cache_keys=None,
):
    """Render multiple Mermaid blocks in one mmdc invocation."""
    if not indices:
        return []

    with tempfile.TemporaryDirectory(prefix="md-to-pdf-mermaid-") as batch_root:
        batch_input = os.path.join(batch_root, "batch.md")
        batch_output = os.path.join(batch_root, "rendered.md")
        artefacts = os.path.join(batch_root, "artefacts")

        parts = []
        for idx in indices:
            content = blocks[idx].content
            if content and not content.endswith("\n"):
                content += "\n"
            parts.append(f"```mermaid\n{content}```\n")
        with open(batch_input, "w", encoding="utf-8") as f:
            f.write("\n".join(parts))

        mmdc_cmd = [
            _find_tool("mmdc"),
            "-i",
            batch_input,
            "-o",
            batch_output,
            "-a",
            artefacts,
            "-e",
            "svg",
            "-b",
            "white",
        ]
        cfg_path = mermaid_config_path or _MERMAID_CFG
        if cfg_path and os.path.isfile(cfg_path):
            mmdc_cmd.extend(["--configFile", cfg_path])
        if puppeteer_cfg_path and os.path.isfile(puppeteer_cfg_path):
            mmdc_cmd.extend(["-p", puppeteer_cfg_path])

        try:
            result = subprocess.run(
                mmdc_cmd,
                capture_output=True,
                text=True,
                timeout=max(120, 15 * len(indices)),
            )
        except subprocess.TimeoutExpired:
            print(
                f"  [WARN] Batch Mermaid render timed out for {len(indices)} diagrams; falling back",
                flush=True,
            )
            return list(indices)

        if result.returncode != 0:
            stderr = result.stderr.strip() or result.stdout.strip()
            print(f"  [WARN] Batch Mermaid render failed: {stderr[:160]}", flush=True)
            return list(indices)

        output_stem = os.path.splitext(os.path.basename(batch_output))[0]
        failures = []
        for offset, idx in enumerate(indices, start=1):
            rendered_svg = os.path.join(artefacts, f"{output_stem}-{offset}.svg")
            svg_path = os.path.join(img_dir, f"d{idx:02d}.svg")
            if not os.path.exists(rendered_svg):
                print(f"  [WARN] d{idx:02d} missing from batch output", flush=True)
                failures.append(idx)
                continue
            shutil.copyfile(rendered_svg, svg_path)
            if cache_dir and cache_keys and cache_keys.get(idx):
                _store_mermaid_cache(cache_dir, cache_keys[idx], svg_path)
            print(f"  [OK] d{idx:02d}.svg", flush=True)

        return failures


def _batched_indices(indices, batch_size):
    """Yield work in bounded chunks unless performance mode disables chunking."""
    if not batch_size or batch_size <= 0:
        if indices:
            yield list(indices)
        return
    for offset in range(0, len(indices), batch_size):
        yield indices[offset:offset + batch_size]


def render_diagrams(
    src: str,
    img_dir: str,
    mermaid_config_path=None,
    puppeteer_cfg_path=None,
    structure=None,
    cache_dir=None,
    batch_size=DEFAULT_MERMAID_BATCH_SIZE,
) -> str:
    """Replace Mermaid fences with inline SVG while minimizing renderer startups."""
    structure = structure or parse_markdown_structure(src)
    if not structure.mermaid_blocks:
        return src

    os.makedirs(img_dir, exist_ok=True)
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)

    mermaid_config_bytes = _read_file_bytes(mermaid_config_path or _MERMAID_CFG)
    cache_keys = {}
    missing = []
    for idx, block in enumerate(structure.mermaid_blocks):
        mmd_path = os.path.join(img_dir, f"d{idx:02d}.mmd")
        svg_path = os.path.join(img_dir, f"d{idx:02d}.svg")
        with open(mmd_path, "w", encoding="utf-8") as f:
            f.write(block.content)

        if cache_dir:
            cache_key = _mermaid_cache_key(block.content, mermaid_config_bytes)
            cache_keys[idx] = cache_key
            cache_path = _mermaid_cache_path(cache_dir, cache_key)
            if os.path.exists(cache_path):
                shutil.copyfile(cache_path, svg_path)
                print(f"  [CACHE] d{idx:02d}.svg", flush=True)
                continue

        missing.append(idx)

    failed = []
    for batch_indices in _batched_indices(missing, batch_size):
        failed.extend(
            _render_mermaid_batch(
                batch_indices,
                structure.mermaid_blocks,
                img_dir,
                mermaid_config_path=mermaid_config_path,
                puppeteer_cfg_path=puppeteer_cfg_path,
                cache_dir=cache_dir,
                cache_keys=cache_keys,
            )
        )
    for idx in failed:
        _render_single_mermaid_block(
            idx,
            structure.mermaid_blocks[idx],
            img_dir,
            mermaid_config_path=mermaid_config_path,
            puppeteer_cfg_path=puppeteer_cfg_path,
            cache_dir=cache_dir,
            cache_key=cache_keys.get(idx),
        )

    lines = src.splitlines(True)
    for idx, block in reversed(list(enumerate(structure.mermaid_blocks))):
        svg_path = os.path.join(img_dir, f"d{idx:02d}.svg")
        if not os.path.exists(svg_path):
            continue
        svg_content = _inline_svg_markup(svg_path, idx)
        replacement = f"\n\n<div class='mermaid-diagram'>\n{svg_content}\n</div>\n\n"
        lines[block.start_line:block.end_line] = _indent_block(
            replacement, block.indent
        ).splitlines(True)

    return ''.join(lines)


def generate_pdf(
    proc_md,
    out_path,
    launch_opts,
    basedir=None,
    stylesheets=None,
    page_media_type=DEFAULT_PAGE_MEDIA_TYPE,
    pdf_options=None,
):
    """Run md-to-pdf and move the result to out_path. Returns True on success."""
    stylesheets = stylesheets or [_STYLESHEET]
    pdf_options = pdf_options or copy.deepcopy(DEFAULT_PDF_OPTIONS)

    cmd = [_find_tool("md-to-pdf")]
    for stylesheet in stylesheets:
        cmd.extend(["--stylesheet", stylesheet])
    cmd.extend(
        [
            "--page-media-type",
            page_media_type,
            "--pdf-options",
            json.dumps(pdf_options),
            "--launch-options",
            json.dumps(launch_opts or {}),
        ]
    )
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
    text = text.strip("-")
    if not text:
        return "section"
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


def inject_heading_anchors(src, headings=None):
    """Inject stable HTML anchors into heading lines so Chromium emits destinations."""
    headings = headings or collect_headings(src)
    if not headings:
        return src

    lines = src.splitlines(True)
    for heading in reversed(headings):
        if heading.start_line >= len(lines):
            continue
        anchor = f'<a id="{heading.slug}"></a>'
        line = lines[heading.start_line]
        if anchor in line:
            continue

        newline = "\n" if line.endswith("\n") else ""
        body = line[:-1] if newline else line

        if heading.end_line - heading.start_line > 1:
            suffix = f" {anchor}" if body.strip() else anchor
            lines[heading.start_line] = f"{body}{suffix}{newline}"
            continue

        match = re.match(r"^([ \t>]*#{1,6}[ \t]+)(.*?)([ \t]+#+[ \t]*)?$", body)
        if match:
            prefix, title, closing = match.groups()
            title = title.rstrip()
            parts = [prefix]
            if title:
                parts.append(title)
                parts.append(" ")
            parts.append(anchor)
            if closing:
                parts.append(closing)
            lines[heading.start_line] = "".join(parts) + newline
        else:
            suffix = f" {anchor}" if body.strip() else anchor
            lines[heading.start_line] = f"{body}{suffix}{newline}"

    links = ''.join(f'<a href="#{heading.slug}"> </a>' for heading in headings)
    hidden_links = (
        '\n\n<div style="height:0;overflow:hidden;font-size:0;line-height:0">'
        f"{links}</div>\n"
    )
    return ''.join(lines) + hidden_links


def add_bookmarks(out_path, src, headings=None):
    """Add PDF bookmarks using precise page numbers from named destinations."""
    if not _PYPDF_OK:
        return
    try:
        headings = headings or collect_headings(src)
        src_len = max(len(src), 1)
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

        for heading in headings:
            page_num = dest_to_page.get(heading.slug)
            if page_num is not None:
                precise += 1
            else:
                # Fallback: estimate page from character position
                page_num = min(int(heading.char_pos / src_len * total_pages), total_pages - 1)

            while parent_stack and parent_stack[-1][0] >= heading.level:
                parent_stack.pop()

            parent = parent_stack[-1][1] if parent_stack else None
            bookmark = writer.add_outline_item(heading.title, page_num, parent=parent)
            parent_stack.append((heading.level, bookmark))

        # Clear /Title metadata in the same write pass to avoid double read/write
        writer.add_metadata({"/Title": ""})

        tmp_path = out_path + ".tmp"
        with open(tmp_path, "wb") as f:
            writer.write(f)
        os.replace(tmp_path, out_path)
        estimated = len(headings) - precise
        if precise == 0 and len(headings) > 0:
            print(f"  [WARN] All {len(headings)} bookmarks used estimated page numbers (no named destinations found)", flush=True)
        else:
            print(f"  Added {len(headings)} bookmarks ({precise} precise, {estimated} estimated)", flush=True)
    except Exception as e:
        print(f"  [WARN] Bookmark generation failed: {e}", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Convert Markdown+Mermaid to PDF")
    parser.add_argument("input", help="Input .md file")
    parser.add_argument("output", nargs="?", default=None,
                        help="Output .pdf file (default: md-to-pdf/<stem>/<stem>.pdf)")
    parser.add_argument("--img-dir", default=None,
                        help="Directory for rendered SVGs (default: <output_dir>/mermaid)")
    parser.add_argument("--max-size", type=float, default=None,
                        help="Max PDF size in MB (default: 10)")
    parser.add_argument(
        "--performance-mode",
        action="store_true",
        help="Use aggressive full-batch Mermaid rendering for maximum speed",
    )
    args = parser.parse_args()

    src_path = os.path.abspath(args.input)
    src_stem = os.path.splitext(os.path.basename(src_path))[0]

    if args.output:
        out_path = os.path.abspath(args.output)
        out_dir = os.path.dirname(out_path)
    else:
        out_dir = os.path.join(os.getcwd(), "md-to-pdf", src_stem)
        out_path = os.path.join(out_dir, f"{src_stem}.pdf")

    # Clean previous output
    if not args.output and os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    print(f"[1/3] Reading {src_path}", flush=True)
    with open(src_path, encoding="utf-8") as f:
        src = f.read()

    front_matter_raw, metadata, body = parse_front_matter(src)
    settings = build_render_settings(src_path, metadata, args)
    img_dir = settings["img_dir"] or os.path.join(out_dir, "mermaid")
    if os.path.isdir(img_dir):
        shutil.rmtree(img_dir)

    proc_root, proc_md, basedir = make_processing_paths(src_path, src_stem)
    runtime_puppeteer_cfg = write_puppeteer_config(settings["launch_options"])

    print("[2/3] Rendering Mermaid diagrams...", flush=True)
    try:
        structure = parse_markdown_structure(body)
        processed = rewrite_relative_urls(body, src_path, proc_md, structure=structure)
        processed = inject_heading_anchors(processed, headings=structure.headings)
        processed = render_diagrams(
            processed,
            img_dir,
            mermaid_config_path=settings["mermaid_config_path"],
            puppeteer_cfg_path=runtime_puppeteer_cfg,
            structure=structure,
            cache_dir=settings["cache_dir"],
            batch_size=settings["mermaid_batch_size"],
        )

        with open(proc_md, "w", encoding="utf-8") as f:
            f.write(front_matter_raw + processed)

        print(f"[3/3] Converting to PDF -> {out_path}", flush=True)
        if not generate_pdf(
            proc_md,
            out_path,
            settings["launch_options"],
            basedir=basedir,
            stylesheets=settings["stylesheets"],
            page_media_type=settings["page_media_type"],
            pdf_options=settings["pdf_options"],
        ):
            sys.exit(1)

        size_mb = os.path.getsize(out_path) / 1024 / 1024
        if size_mb > settings["max_size"]:
            print(
                f"  [WARN] PDF {size_mb:.1f} MB exceeds {settings['max_size']} MB limit",
                flush=True,
            )

        add_bookmarks(out_path, body, headings=structure.headings)
        size_mb = os.path.getsize(out_path) / 1024 / 1024
        print(f"Done! {out_path}  ({size_mb:.1f} MB)", flush=True)
    finally:
        if runtime_puppeteer_cfg and os.path.exists(runtime_puppeteer_cfg):
            os.remove(runtime_puppeteer_cfg)
        if os.path.isdir(proc_root):
            shutil.rmtree(proc_root)


if __name__ == "__main__":
    main()
