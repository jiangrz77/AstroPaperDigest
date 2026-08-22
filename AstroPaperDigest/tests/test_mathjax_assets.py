"""Regression checks for offline MathJax packaging and configuration."""

from pathlib import Path
import unittest


PROJECT_DIR = Path(__file__).resolve().parents[1]


class MathJaxAssetTests(unittest.TestCase):
    def test_digest_loads_local_mathjax_in_order(self):
        gui_source = (PROJECT_DIR / "src" / "gui.py").read_text(encoding="utf-8")
        config_tag = '<script defer src="/static/mathjax/config.js"></script>'
        engine_tag = '<script defer src="/static/mathjax/tex-svg-full.js" id="MathJax-script"></script>'

        self.assertIn(config_tag, gui_source)
        self.assertIn(engine_tag, gui_source)
        self.assertLess(gui_source.index(config_tag), gui_source.index(engine_tag))
        self.assertNotIn("cdn.jsdelivr.net/npm/mathjax", gui_source)

    def test_mathjax_config_preserves_tex_delimiters(self):
        config = (PROJECT_DIR / "static" / "mathjax" / "config.js").read_text(encoding="utf-8")

        self.assertIn("['\\\\(', '\\\\)']", config)
        self.assertIn("['\\\\[', '\\\\]']", config)
        self.assertIn("'\\\\mathrm{H_{II}}'", config)
        self.assertIn("processEscapes: true", config)

    def test_build_includes_static_assets(self):
        build_script = (PROJECT_DIR / "build_dmg.sh").read_text(encoding="utf-8")

        self.assertIn('--add-data "$APP_ROOT/static:static"', build_script)
        self.assertTrue((PROJECT_DIR / "static" / "mathjax" / "tex-svg-full.js").is_file())

    def test_regular_app_build_uses_native_bundle(self):
        build_script = (PROJECT_DIR / "build_app.sh").read_text(encoding="utf-8")

        self.assertIn("--windowed", build_script)
        self.assertIn("--osx-bundle-identifier", build_script)
        self.assertIn("--icon", build_script)
        self.assertIn("codesign --force --deep", build_script)
        self.assertNotIn("osascript", build_script)


if __name__ == "__main__":
    unittest.main()
