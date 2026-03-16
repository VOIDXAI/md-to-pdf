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
                check=True,
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
                },
            }
            cli_args = SimpleNamespace(
                img_dir=str(tmpdir_path / "cli-mermaid"),
                max_size=9.0,
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


if __name__ == "__main__":
    unittest.main()
