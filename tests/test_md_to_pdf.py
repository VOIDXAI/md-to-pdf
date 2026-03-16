import importlib.util
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
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


class MdToPdfEndToEndTests(unittest.TestCase):
    def run_conversion(self, markdown, extra_files=None):
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


if __name__ == "__main__":
    unittest.main()
