"""
2.5 T52 (test pass 8): the ``text-faint`` token reads at WCAG AA (4.5:1) on
the console's backgrounds (ink, panel, panel-hi) in both themes, and the
built stylesheet carries the same values as the source.

Test pass 9: the colour tokens hold bare RGB channels ("16 19 27"), so a
template's own CSS must wrap them in ``rgb()``; a bare ``var(--panel)`` is an
invalid colour and the browser drops it (the Restarting page lost its card,
spinner and progress-bar colours that way).
"""

import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

SOURCE = Path(settings.BASE_DIR) / "theme" / "static_src" / "src" / "styles.css"
TEMPLATES = Path(settings.BASE_DIR) / "templates"
BUILT = Path(settings.BASE_DIR) / "theme" / "static" / "css" / "dist" / "styles.css"
BACKGROUNDS = ("ink", "panel", "panel-hi")


def _tokens(css, selector):
    block = re.search(re.escape(selector) + r"\s*\{(.*?)\}", css, re.S).group(1)
    return {k: tuple(int(x) for x in v.split()) for k, v in re.findall(r"--([\w-]+):\s*([\d ]+);", block)}


def _luminance(rgb):
    def channel(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _ratio(a, b):
    high, low = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


class FaintContrastTests(SimpleTestCase):
    def test_faint_text_is_at_least_4_5_to_1_in_both_themes(self):
        css = SOURCE.read_text(encoding="utf-8")
        for theme, selector in (("light", ":root"), ("dark", ".dark")):
            tokens = _tokens(css, selector)
            for background in BACKGROUNDS:
                with self.subTest(theme=theme, background=background):
                    self.assertGreaterEqual(_ratio(tokens["faint"], tokens[background]), 4.5)

    def test_the_built_stylesheet_has_the_same_faint_values(self):
        source = SOURCE.read_text(encoding="utf-8")
        built = BUILT.read_text(encoding="utf-8")
        for selector in (":root", ".dark"):
            faint = " ".join(str(c) for c in _tokens(source, selector)["faint"])
            with self.subTest(selector=selector):
                self.assertIn(f"--faint:{faint}", built.replace(": ", ":"))


class TemplateColourTokenTests(SimpleTestCase):
    def test_templates_wrap_the_colour_tokens_in_rgb(self):
        names = "|".join(re.escape(name) for name in sorted(_tokens(SOURCE.read_text(encoding="utf-8"), ":root")))
        bare = re.compile(r"(?<!rgb\()(?<!rgba\()var\(--(?:" + names + r")\)")
        found = []
        for path in sorted(TEMPLATES.rglob("*.html")):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if bare.search(line):
                    found.append(f"{path.relative_to(TEMPLATES)}:{number}: {line.strip()}")
        self.assertEqual(found, [])

    def test_the_restarting_page_colours_are_valid(self):
        html = (TEMPLATES / "cpanel" / "plugins" / "restarting.html").read_text(encoding="utf-8")
        for rule in ("background:rgb(var(--panel))", "border-top-color:rgb(var(--warn))",
                     "background:rgb(var(--warn))", "color:rgb(var(--faint))"):
            with self.subTest(rule=rule):
                self.assertIn(rule, html)
