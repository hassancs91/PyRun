"""
2.5 T52 (test pass 8): the ``text-faint`` token reads at WCAG AA (4.5:1) on
the console's backgrounds (ink, panel, panel-hi) in both themes, and the
built stylesheet carries the same values as the source.

Test pass 9: the colour tokens hold bare RGB channels ("16 19 27"), so a
template's own CSS must wrap them in ``rgb()``; a bare ``var(--panel)`` is an
invalid colour and the browser drops it (the Restarting page lost its card,
spinner and progress-bar colours that way).

Run 7 (O3): in the light theme the status colours read at 4.5:1 too. Text
(``text-ok`` / ``text-warn`` / ``text-fail`` / ``text-accent``) has its own
darker ``--*-text`` tokens, checked on the backgrounds and on its own /5-/20
tints over them (pills, banners, hover); the fills are dark enough that a
button's ``text-ink`` label reads at 4.5:1. The dark theme keeps its values.
"""

import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

SOURCE = Path(settings.BASE_DIR) / "theme" / "static_src" / "src" / "styles.css"
TEMPLATES = Path(settings.BASE_DIR) / "templates"
BUILT = Path(settings.BASE_DIR) / "theme" / "static" / "css" / "dist" / "styles.css"
TAILWIND = Path(settings.BASE_DIR) / "theme" / "static_src" / "tailwind.config.js"
BACKGROUNDS = ("ink", "panel", "panel-hi")
STATUSES = ("ok", "warn", "fail", "accent")
TINTS = (0.05, 0.06, 0.10, 0.15, 0.20)  # bg-*/5 to hover:bg-*/20; .pyai-error uses .06


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


def _over(colour, background, alpha):
    return tuple(round(alpha * c + (1 - alpha) * b) for c, b in zip(colour, background))


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


class StatusContrastTests(SimpleTestCase):
    def setUp(self):
        css = SOURCE.read_text(encoding="utf-8")
        self.light = _tokens(css, ":root")
        self.dark = _tokens(css, ".dark")

    def test_status_text_is_at_least_4_5_to_1_on_backgrounds_and_tints(self):
        for theme, tokens in (("light", self.light), ("dark", self.dark)):
            for status in STATUSES:
                text, fill = tokens[f"{status}-text"], tokens[status]
                for name in BACKGROUNDS:
                    background = tokens[name]
                    with self.subTest(theme=theme, status=status, background=name):
                        self.assertGreaterEqual(_ratio(text, background), 4.5)
                    for alpha in TINTS:
                        with self.subTest(theme=theme, status=status, background=name, tint=alpha):
                            self.assertGreaterEqual(_ratio(text, _over(fill, background, alpha)), 4.5)

    def test_a_label_on_a_status_fill_is_at_least_4_5_to_1(self):
        # Primary buttons and solid badges: bg-ok / bg-warn / bg-fail with text-ink.
        for theme, tokens in (("light", self.light), ("dark", self.dark)):
            for status in STATUSES:
                with self.subTest(theme=theme, status=status):
                    self.assertGreaterEqual(_ratio(tokens["ink"], tokens[status]), 4.5)

    def test_the_dark_theme_text_uses_the_fill_colours(self):
        for status in STATUSES:
            with self.subTest(status=status):
                self.assertEqual(self.dark[f"{status}-text"], self.dark[status])

    def test_tailwind_text_utilities_use_the_text_tokens(self):
        config = TAILWIND.read_text(encoding="utf-8")
        block = re.search(r"textColor:\s*\{(.*?)\}", config, re.S).group(1)
        for status in STATUSES:
            with self.subTest(status=status):
                self.assertRegex(block, rf"\b{status}:\s*v\('--{status}-text'\)")

    def test_the_built_stylesheet_has_the_same_status_values(self):
        source = SOURCE.read_text(encoding="utf-8")
        built = BUILT.read_text(encoding="utf-8").replace(": ", ":")
        for selector in (":root", ".dark"):
            tokens = _tokens(source, selector)
            for status in STATUSES:
                for name in (status, f"{status}-text"):
                    value = " ".join(str(c) for c in tokens[name])
                    with self.subTest(selector=selector, token=name):
                        self.assertIn(f"--{name}:{value}", built)
        self.assertIn("rgb(var(--ok-text)", built)


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
