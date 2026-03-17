import importlib.util
import os
import pathlib
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import mock

from pypdf import PdfReader


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "md_to_pdf.py"


def load_converter_module():
    spec = importlib.util.spec_from_file_location("md_to_pdf_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def extract_pdf_text(pdf_path):
    reader = PdfReader(str(pdf_path))
    chunks = []
    for page in reader.pages:
        chunks.append(page.extract_text() or "")
    return "\n".join(chunks)


def flatten_outline_titles(outline):
    titles = []
    for item in outline:
        if isinstance(item, list):
            titles.extend(flatten_outline_titles(item))
            continue
        title = getattr(item, "title", None)
        if title:
            titles.append(title)
    return titles


def flatten_outline_with_depth(outline, depth=0):
    rows = []
    for item in outline:
        if isinstance(item, list):
            rows.extend(flatten_outline_with_depth(item, depth + 1))
            continue
        title = getattr(item, "title", None)
        if title:
            rows.append((depth, title))
    return rows


class MdToPdfEndToEndTests(unittest.TestCase):
    @contextmanager
    def converted_output(self, markdown, extra_files=None):
        extra_files = extra_files or {}
        with tempfile.TemporaryDirectory(prefix="md-to-pdf-test-") as tmpdir:
            tmpdir_path = pathlib.Path(tmpdir)
            input_path = tmpdir_path / "input.md"
            output_path = tmpdir_path / "output.pdf"
            input_path.write_text(textwrap.dedent(markdown).strip() + "\n", encoding="utf-8")

            for relative_path, content in extra_files.items():
                target = tmpdir_path / relative_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")

            result = subprocess.run(
                [sys.executable, str(SCRIPT), str(input_path), str(output_path)],
                cwd=tmpdir,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                result.returncode,
                0,
                msg=f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}",
            )
            self.assertTrue(output_path.exists(), msg=result.stderr or result.stdout)
            yield tmpdir_path, output_path, result

    def run_conversion(self, markdown, extra_files=None):
        with self.converted_output(markdown, extra_files=extra_files) as (_, output_path, result):
            return extract_pdf_text(output_path), result

    def test_renders_mermaid_inside_indented_list_item(self):
        pdf_text, _ = self.run_conversion(
            """
            # Nested Mermaid

            1. Flow
               ```mermaid
               graph TD
                   Start --> End
               ```
            """
        )
        self.assertIn("Start", pdf_text)
        self.assertIn("End", pdf_text)
        self.assertNotIn("graph TD", pdf_text)

    def test_preserves_relative_svg_assets(self):
        pdf_text, _ = self.run_conversion(
            """
            # Relative Asset

            ![ALT-FALLBACK](./assets/local.svg)
            """,
            extra_files={
                "assets/local.svg": """
                    <svg xmlns="http://www.w3.org/2000/svg" width="360" height="80" viewBox="0 0 360 80">
                      <rect width="360" height="80" fill="#f4efe6"/>
                      <text x="180" y="48" text-anchor="middle" font-size="24" fill="#202020">SVG-EMBEDDED</text>
                    </svg>
                """
            },
        )
        self.assertIn("SVG-EMBEDDED", pdf_text)
        self.assertNotIn("ALT-FALLBACK", pdf_text)

    def test_renders_tilde_mermaid_fence(self):
        pdf_text, _ = self.run_conversion(
            """
            ~~~mermaid
            graph TD
                Start --> End
            ~~~
            """
        )
        self.assertIn("Start", pdf_text)
        self.assertIn("End", pdf_text)
        self.assertNotIn("graph TD", pdf_text)

    def test_renders_mermaid_fence_with_info_string(self):
        pdf_text, _ = self.run_conversion(
            """
            ```mermaid title=test
            graph TD
                Start --> End
            ```
            """
        )
        self.assertIn("Start", pdf_text)
        self.assertIn("End", pdf_text)
        self.assertNotIn("graph TD", pdf_text)

    def test_setext_headings_generate_bookmarks(self):
        with self.converted_output(
            """
            Top Title
            =========

            Sub Title
            ---------
            """
        ) as (_, output_path, _):
            reader = PdfReader(str(output_path))
            titles = flatten_outline_titles(reader.outline)

        self.assertIn("Top Title", titles)
        self.assertIn("Sub Title", titles)

    def test_front_matter_img_dir_is_used_end_to_end(self):
        with self.converted_output(
            """
            ---
            md_to_pdf:
              img_dir: rendered/diagrams
            ---

            # Front Matter

            ```mermaid
            graph TD
                Start --> End
            ```
            """
        ) as (tmpdir_path, output_path, _):
            pdf_text = extract_pdf_text(output_path)
            self.assertTrue((tmpdir_path / "rendered" / "diagrams" / "d00.svg").exists())
        self.assertIn("Start", pdf_text)
        self.assertIn("End", pdf_text)
        self.assertNotIn("md_to_pdf", pdf_text)

    def test_top_level_front_matter_stylesheet_is_applied(self):
        with self.converted_output(
            """
            ---
            stylesheet: ./extra.css
            ---

            # Styled

            Body
            """,
            extra_files={
                "extra.css": 'body::before { content: "STYLE-MARKER"; display:block; }\n',
            },
        ) as (_, output_path, _):
            pdf_text = extract_pdf_text(output_path)

        self.assertIn("STYLE-MARKER", pdf_text)
        self.assertIn("Styled", pdf_text)

    def test_long_document_generates_precise_nested_bookmarks(self):
        filler = "\n\n".join(
            f"Paragraph {index} keeps the document long enough for pagination."
            for index in range(1, 50)
        )
        markdown = (
            "# Top Title\n\n"
            f"{filler}\n\n"
            "## Section One\n\n"
            f"{filler}\n\n"
            "### Deep Dive\n\n"
            f"{filler}\n"
        )
        with self.converted_output(
            markdown
        ) as (_, output_path, result):
            reader = PdfReader(str(output_path))
            outline = flatten_outline_with_depth(reader.outline)
            destination_names = {name.lstrip("/") for name in reader.named_destinations}

        self.assertGreater(len(reader.pages), 1)
        self.assertIn("Added 3 bookmarks (3 precise, 0 estimated)", result.stdout)
        self.assertIn("top-title", destination_names)
        self.assertIn("section-one", destination_names)
        self.assertIn("deep-dive", destination_names)
        self.assertIn((0, "Top Title"), outline)
        self.assertIn((1, "Section One"), outline)
        self.assertIn((2, "Deep Dive"), outline)


class LaunchOptionTests(unittest.TestCase):
    def test_prefers_environment_browser_path_over_invalid_config(self):
        module = load_converter_module()
        with tempfile.TemporaryDirectory(prefix="md-to-pdf-config-") as tmpdir:
            config_path = pathlib.Path(tmpdir) / "puppeteer.json"
            config_path.write_text('{"executablePath":"/definitely/missing/chrome"}\n', encoding="utf-8")

            with mock.patch.object(module, "_PUPPETEER_CFG", str(config_path)):
                with mock.patch.dict(os.environ, {"PUPPETEER_EXECUTABLE_PATH": sys.executable}, clear=False):
                    launch_opts = module.load_launch_options()

        self.assertEqual(launch_opts.get("executablePath"), sys.executable)

    def test_drops_invalid_browser_path_when_not_resolvable(self):
        module = load_converter_module()
        with tempfile.TemporaryDirectory(prefix="md-to-pdf-config-") as tmpdir:
            config_path = pathlib.Path(tmpdir) / "puppeteer.json"
            config_path.write_text('{"executablePath":"/definitely/missing/chrome"}\n', encoding="utf-8")

            with mock.patch.object(module, "_PUPPETEER_CFG", str(config_path)):
                with mock.patch.object(module, "_resolve_executable", return_value=None):
                    with mock.patch.dict(
                        os.environ,
                        {
                            "PUPPETEER_EXECUTABLE_PATH": "",
                            "CHROME_PATH": "",
                            "GOOGLE_CHROME_BIN": "",
                        },
                        clear=False,
                    ):
                        launch_opts = module.load_launch_options()

        self.assertNotIn("executablePath", launch_opts)


class HeadingInjectionTests(unittest.TestCase):
    def test_ignores_tilde_code_fences_when_injecting_anchors(self):
        module = load_converter_module()
        result = module.inject_heading_anchors(
            "~~~\n# inside code\n~~~\n\n# real heading\n"
        )

        self.assertNotIn('<a id="inside-code"></a>', result)
        self.assertIn('<a id="real-heading"></a>', result)

    def test_uses_fallback_slug_for_punctuation_only_headings(self):
        module = load_converter_module()
        headings = module.collect_headings("# !!!\n\n# ???\n")
        result = module.inject_heading_anchors("# !!!\n\n# ???\n")

        self.assertEqual(
            [(heading.title, heading.slug) for heading in headings],
            [("!!!", "section"), ("???", "section-1")],
        )
        self.assertIn('<a id="section"></a>', result)
        self.assertIn('<a id="section-1"></a>', result)
        self.assertNotIn('<a id=""></a>', result)


class FrontMatterTests(unittest.TestCase):
    def test_build_render_settings_merges_front_matter_and_cli(self):
        module = load_converter_module()
        with tempfile.TemporaryDirectory(prefix="md-to-pdf-frontmatter-") as tmpdir:
            tmpdir_path = pathlib.Path(tmpdir)
            input_path = tmpdir_path / "input.md"
            input_path.write_text("# Title\n", encoding="utf-8")
            (tmpdir_path / "extra.css").write_text("body { color: #222; }\n", encoding="utf-8")
            (tmpdir_path / "custom-mermaid.json").write_text('{"theme":"base"}\n', encoding="utf-8")
            (tmpdir_path / "custom-puppeteer.json").write_text('{"headless":false}\n', encoding="utf-8")

            metadata = {
                "stylesheet": "./extra.css",
                "page_media_type": "screen",
                "pdf_options": {"format": "Letter", "margin": {"top": "12mm"}},
                "launch_options": {"args": ["--lang=en-US"]},
                "md_to_pdf": {
                    "img_dir": "rendered/diagrams",
                    "max_size": 7.5,
                    "mermaid_config": "./custom-mermaid.json",
                    "puppeteer_config": "./custom-puppeteer.json",
                    "cache_dir": "./cache/mermaid",
                },
            }
            cli_args = SimpleNamespace(
                img_dir=str(tmpdir_path / "cli-mermaid"),
                max_size=9.0,
                performance_mode=False,
            )

            with mock.patch.object(module, "_resolve_executable", return_value=sys.executable):
                settings = module.build_render_settings(str(input_path), metadata, cli_args)

        self.assertEqual(settings["page_media_type"], "screen")
        self.assertEqual(settings["img_dir"], str(tmpdir_path / "cli-mermaid"))
        self.assertEqual(settings["max_size"], 9.0)
        self.assertIn(str(tmpdir_path / "extra.css"), settings["stylesheets"])
        self.assertEqual(settings["pdf_options"]["format"], "Letter")
        self.assertEqual(settings["pdf_options"]["margin"]["top"], "12mm")
        self.assertEqual(settings["pdf_options"]["margin"]["left"], "15mm")
        self.assertEqual(settings["launch_options"]["headless"], False)
        self.assertEqual(settings["launch_options"]["args"], ["--lang=en-US"])
        self.assertEqual(settings["mermaid_config_path"], str(tmpdir_path / "custom-mermaid.json"))
        self.assertEqual(settings["cache_dir"], str(tmpdir_path / "cache" / "mermaid"))
        self.assertFalse(settings["performance_mode"])
        self.assertEqual(settings["mermaid_batch_size"], 4)

    def test_build_render_settings_allows_disabling_cache(self):
        module = load_converter_module()
        with tempfile.TemporaryDirectory(prefix="md-to-pdf-frontmatter-") as tmpdir:
            tmpdir_path = pathlib.Path(tmpdir)
            input_path = tmpdir_path / "input.md"
            input_path.write_text("# Title\n", encoding="utf-8")

            metadata = {"md_to_pdf": {"cache_dir": False}}
            cli_args = SimpleNamespace(img_dir=None, max_size=None, performance_mode=False)
            settings = module.build_render_settings(str(input_path), metadata, cli_args)

        self.assertIsNone(settings["cache_dir"])

    def test_build_render_settings_enables_performance_mode(self):
        module = load_converter_module()
        with tempfile.TemporaryDirectory(prefix="md-to-pdf-frontmatter-") as tmpdir:
            tmpdir_path = pathlib.Path(tmpdir)
            input_path = tmpdir_path / "input.md"
            input_path.write_text("# Title\n", encoding="utf-8")

            metadata = {"md_to_pdf": {"performance_mode": True}}
            cli_args = SimpleNamespace(img_dir=None, max_size=None, performance_mode=False)
            settings = module.build_render_settings(str(input_path), metadata, cli_args)

        self.assertTrue(settings["performance_mode"])
        self.assertIsNone(settings["mermaid_batch_size"])

    def test_cli_performance_mode_overrides_default_batching(self):
        module = load_converter_module()
        with tempfile.TemporaryDirectory(prefix="md-to-pdf-frontmatter-") as tmpdir:
            tmpdir_path = pathlib.Path(tmpdir)
            input_path = tmpdir_path / "input.md"
            input_path.write_text("# Title\n", encoding="utf-8")

            metadata = {}
            cli_args = SimpleNamespace(img_dir=None, max_size=None, performance_mode=True)
            settings = module.build_render_settings(str(input_path), metadata, cli_args)

        self.assertTrue(settings["performance_mode"])
        self.assertIsNone(settings["mermaid_batch_size"])

    def test_rewrite_relative_urls_preserves_code_fences_after_prior_rewrites(self):
        module = load_converter_module()
        src = textwrap.dedent(
            """
            ![Outside](./assets/outside.svg)

            ```markdown
            ![Inside](./assets/inside.svg)
            ```

            [ref]: ./assets/ref.svg
            """
        ).lstrip()

        rewritten = module.rewrite_relative_urls(
            src,
            "/tmp/project/input.md",
            "/tmp/project/.md-to-pdf-123/processed_input.md",
            structure=module.parse_markdown_structure(src),
        )

        self.assertIn("![Outside](../assets/outside.svg)", rewritten)
        self.assertIn("[ref]: ../assets/ref.svg", rewritten)
        self.assertIn("![Inside](./assets/inside.svg)", rewritten)

    def test_rewrite_relative_urls_preserves_inline_code_examples(self):
        module = load_converter_module()
        src = '`![code](./assets/code.svg)` and `<img src="./assets/example.svg">` and ![real](./assets/real.svg)\n'

        rewritten = module.rewrite_relative_urls(
            src,
            "/tmp/project/input.md",
            "/tmp/project/.md-to-pdf-123/processed_input.md",
            structure=module.parse_markdown_structure(src),
        )

        self.assertIn("`![code](./assets/code.svg)`", rewritten)
        self.assertIn("`<img src=\"./assets/example.svg\">`", rewritten)
        self.assertIn("![real](../assets/real.svg)", rewritten)

    def test_rewrite_relative_urls_rewrites_html_snippets(self):
        module = load_converter_module()
        src = '<img src="./assets/diagram.svg" alt="Diagram">\n<a href="./docs/spec.html">Spec</a>\n'

        rewritten = module.rewrite_relative_urls(
            src,
            "/tmp/project/input.md",
            "/tmp/project/.md-to-pdf-123/processed_input.md",
            structure=module.parse_markdown_structure(src),
        )

        self.assertIn('<img src="../assets/diagram.svg" alt="Diagram">', rewritten)
        self.assertIn('<a href="../docs/spec.html">Spec</a>', rewritten)


class MermaidRenderTests(unittest.TestCase):
    def test_render_diagrams_batches_and_reuses_cache(self):
        module = load_converter_module()
        src = textwrap.dedent(
            """
            # Demo

            ```mermaid
            graph TD
                Start --> End
            ```

            ```mermaid
            graph TD
                Left --> Right
            ```
            """
        ).lstrip()
        structure = module.parse_markdown_structure(src)

        with tempfile.TemporaryDirectory(prefix="md-to-pdf-mermaid-") as tmpdir:
            tmpdir_path = pathlib.Path(tmpdir)
            img_dir = tmpdir_path / "img"
            cache_dir = tmpdir_path / "cache"
            calls = []

            def fake_run(cmd, capture_output, text, timeout):
                calls.append(cmd)
                batch_output = pathlib.Path(cmd[cmd.index("-o") + 1])
                artefacts_dir = pathlib.Path(cmd[cmd.index("-a") + 1])
                artefacts_dir.mkdir(parents=True, exist_ok=True)
                for offset, label in enumerate(("Start", "Left"), start=1):
                    (artefacts_dir / f"{batch_output.stem}-{offset}.svg").write_text(
                        (
                            '<svg xmlns="http://www.w3.org/2000/svg" id="my-svg">'
                            f"<text>{label}</text></svg>"
                        ),
                        encoding="utf-8",
                    )
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with mock.patch.object(module.subprocess, "run", side_effect=fake_run):
                rendered = module.render_diagrams(
                    src,
                    str(img_dir),
                    structure=structure,
                    cache_dir=str(cache_dir),
                )

            self.assertEqual(len(calls), 1)
            self.assertIn('id="mmd-00"', rendered)
            self.assertIn('id="mmd-01"', rendered)
            self.assertIn("Start", rendered)
            self.assertIn("Left", rendered)
            self.assertEqual(len(list(cache_dir.rglob("*.svg"))), 2)

            img_dir_second = tmpdir_path / "img-second"
            with mock.patch.object(module.subprocess, "run") as run_mock:
                rendered_second = module.render_diagrams(
                    src,
                    str(img_dir_second),
                    structure=structure,
                    cache_dir=str(cache_dir),
                )

            run_mock.assert_not_called()
            self.assertIn('id="mmd-00"', rendered_second)
            self.assertIn('id="mmd-01"', rendered_second)
            self.assertTrue((img_dir_second / "d00.svg").exists())
            self.assertTrue((img_dir_second / "d01.svg").exists())

    def test_render_diagrams_uses_conservative_default_batch_size(self):
        module = load_converter_module()
        src = textwrap.dedent(
            """
            ```mermaid
            graph TD
                A --> B
            ```

            ```mermaid
            graph TD
                C --> D
            ```

            ```mermaid
            graph TD
                E --> F
            ```

            ```mermaid
            graph TD
                G --> H
            ```

            ```mermaid
            graph TD
                I --> J
            ```
            """
        ).lstrip()
        structure = module.parse_markdown_structure(src)

        with tempfile.TemporaryDirectory(prefix="md-to-pdf-mermaid-") as tmpdir:
            img_dir = pathlib.Path(tmpdir) / "img"
            calls = []

            def fake_run(cmd, capture_output, text, timeout):
                calls.append(cmd)
                batch_output = pathlib.Path(cmd[cmd.index("-o") + 1])
                artefacts_dir = pathlib.Path(cmd[cmd.index("-a") + 1])
                artefacts_dir.mkdir(parents=True, exist_ok=True)
                batch_size = 4 if len(calls) == 1 else 1
                for offset in range(1, batch_size + 1):
                    label = f"batch-{len(calls)}-{offset}"
                    (artefacts_dir / f"{batch_output.stem}-{offset}.svg").write_text(
                        (
                            '<svg xmlns="http://www.w3.org/2000/svg" id="my-svg">'
                            f"<text>{label}</text></svg>"
                        ),
                        encoding="utf-8",
                    )
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with mock.patch.object(module.subprocess, "run", side_effect=fake_run):
                rendered = module.render_diagrams(src, str(img_dir), structure=structure)

            self.assertEqual(len(calls), 2)
            self.assertIn("batch-1-1", rendered)
            self.assertIn("batch-2-1", rendered)
            self.assertTrue((img_dir / "d04.svg").exists())

    def test_render_diagrams_performance_mode_uses_single_batch(self):
        module = load_converter_module()
        src = textwrap.dedent(
            """
            ```mermaid
            graph TD
                A --> B
            ```

            ```mermaid
            graph TD
                C --> D
            ```

            ```mermaid
            graph TD
                E --> F
            ```

            ```mermaid
            graph TD
                G --> H
            ```

            ```mermaid
            graph TD
                I --> J
            ```
            """
        ).lstrip()
        structure = module.parse_markdown_structure(src)

        with tempfile.TemporaryDirectory(prefix="md-to-pdf-mermaid-") as tmpdir:
            img_dir = pathlib.Path(tmpdir) / "img"
            calls = []

            def fake_run(cmd, capture_output, text, timeout):
                calls.append(cmd)
                batch_output = pathlib.Path(cmd[cmd.index("-o") + 1])
                artefacts_dir = pathlib.Path(cmd[cmd.index("-a") + 1])
                artefacts_dir.mkdir(parents=True, exist_ok=True)
                for offset in range(1, 6):
                    (artefacts_dir / f"{batch_output.stem}-{offset}.svg").write_text(
                        (
                            '<svg xmlns="http://www.w3.org/2000/svg" id="my-svg">'
                            f"<text>perf-{offset}</text></svg>"
                        ),
                        encoding="utf-8",
                    )
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with mock.patch.object(module.subprocess, "run", side_effect=fake_run):
                rendered = module.render_diagrams(
                    src,
                    str(img_dir),
                    structure=structure,
                    batch_size=None,
                )

            self.assertEqual(len(calls), 1)
            self.assertIn("perf-5", rendered)

    def test_render_diagrams_falls_back_to_single_render_on_batch_failure(self):
        module = load_converter_module()
        src = textwrap.dedent(
            """
            ```mermaid
            graph TD
                One --> Two
            ```

            ```mermaid
            graph TD
                Three --> Four
            ```
            """
        ).lstrip()
        structure = module.parse_markdown_structure(src)

        with tempfile.TemporaryDirectory(prefix="md-to-pdf-mermaid-") as tmpdir:
            img_dir = pathlib.Path(tmpdir) / "img"
            calls = []

            def fake_run(cmd, capture_output, text, timeout):
                calls.append(cmd)
                output_path = pathlib.Path(cmd[cmd.index("-o") + 1])
                if "-a" in cmd:
                    return SimpleNamespace(returncode=1, stdout="", stderr="batch failed")

                label = "One" if output_path.name == "d00.svg" else "Three"
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(
                    (
                        '<svg xmlns="http://www.w3.org/2000/svg" id="my-svg">'
                        f"<text>{label}</text></svg>"
                    ),
                    encoding="utf-8",
                )
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with mock.patch.object(module.subprocess, "run", side_effect=fake_run):
                rendered = module.render_diagrams(src, str(img_dir), structure=structure)

            self.assertEqual(len(calls), 3)
            self.assertIn("One", rendered)
            self.assertIn("Three", rendered)
            self.assertTrue((img_dir / "d00.svg").exists())
            self.assertTrue((img_dir / "d01.svg").exists())


class GeneratePdfTests(unittest.TestCase):
    def test_generate_pdf_retries_browser_launch_failures(self):
        module = load_converter_module()
        with tempfile.TemporaryDirectory(prefix="md-to-pdf-generate-") as tmpdir:
            tmpdir_path = pathlib.Path(tmpdir)
            proc_md = tmpdir_path / "processed_input.md"
            proc_md.write_text("# Title\n", encoding="utf-8")
            out_path = tmpdir_path / "output.pdf"
            generated_pdf = proc_md.with_suffix(".pdf")
            calls = []

            def fake_run(cmd, capture_output, text):
                calls.append(cmd)
                if len(calls) == 1:
                    return SimpleNamespace(
                        returncode=1,
                        stdout="",
                        stderr="Error: Failed to launch the browser process!\n",
                    )
                generated_pdf.write_bytes(b"%PDF-1.4\n")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with mock.patch.object(module.subprocess, "run", side_effect=fake_run):
                with mock.patch.object(module.time, "sleep") as sleep_mock:
                    ok = module.generate_pdf(str(proc_md), str(out_path), launch_opts={})

            self.assertTrue(ok)
            self.assertEqual(len(calls), 2)
            sleep_mock.assert_called_once_with(1)
            self.assertTrue(out_path.exists())

    def test_generate_pdf_does_not_retry_non_launch_errors(self):
        module = load_converter_module()
        with tempfile.TemporaryDirectory(prefix="md-to-pdf-generate-") as tmpdir:
            tmpdir_path = pathlib.Path(tmpdir)
            proc_md = tmpdir_path / "processed_input.md"
            proc_md.write_text("# Title\n", encoding="utf-8")
            out_path = tmpdir_path / "output.pdf"

            with mock.patch.object(
                module.subprocess,
                "run",
                return_value=SimpleNamespace(returncode=1, stdout="", stderr="syntax exploded"),
            ) as run_mock:
                with mock.patch.object(module.time, "sleep") as sleep_mock:
                    ok = module.generate_pdf(str(proc_md), str(out_path), launch_opts={})

            self.assertFalse(ok)
            run_mock.assert_called_once()
            sleep_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
