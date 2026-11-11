"""Lucide console icons (D14 item 4): vendored SVGs, the {% icon %} tag, sidebar mapping."""

import re
from pathlib import Path

from django.conf import settings
from django.template import Context, Template
from django.test import SimpleTestCase
from django.urls import reverse

from core.templatetags.icon_tags import ICON_DIR, LUCIDE_VERSION, available_icons, icon_body
from core.test_console_layout import ShellBase

SIDEBAR = {
    "Dashboard": "layout-dashboard", "Scripts": "file-code-2", "Libraries": "library-big", "Brains": "brain",
    "Runs": "circle-play", "Py AI": "sparkles", "Tasks": "list-checks", "Connect": "plug-zap", "Triggers": "plug-zap",
    "Environments": "boxes", "Secrets": "key-round", "Connections": "link", "Tags": "tag", "Data Stores": "hard-drive",
    "Databases": "database", "Vectors": "chart-scatter", "Channels": "messages-square", "Services": "blocks",
    "Settings": "settings", "Logs": "scroll-text", "Workspaces": "layout-grid", "Users": "users", "Plugins": "puzzle",
}


class IconTagTests(SimpleTestCase):
    def test_vendored_set_is_licensed_pinned_and_parses(self):
        license_text = (ICON_DIR / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("ISC License", license_text)
        self.assertIn("Lucide Contributors", license_text)
        for name in available_icons():
            with self.subTest(name=name):
                self.assertIn(f"lucide-static v{LUCIDE_VERSION}", (ICON_DIR / f"{name}.svg").read_text(encoding="utf-8"))
                self.assertTrue(icon_body(name))
        self.assertTrue(set(SIDEBAR.values()) <= set(available_icons()))

    def test_tag_renders_inline_svg_without_scripts_or_remote_refs(self):
        html = Template('{% load icon_tags %}{% icon "brain" class="w-5 h-5 text-ok" %}').render(Context())
        self.assertTrue(html.startswith('<svg class="w-5 h-5 text-ok"'))
        self.assertIn('stroke="currentColor"', html)
        self.assertIn('aria-hidden="true"', html)
        self.assertNotIn("<script", html)
        self.assertNotIn("href", html)
        self.assertEqual(html.count("<svg"), 1)

    def test_default_size_stroke_width_and_unknown_or_hostile_names(self):
        html = Template('{% load icon_tags %}{% icon "tag" stroke_width="1.8" %}').render(Context())
        self.assertIn('class="w-[18px] h-[18px]"', html)
        self.assertIn('stroke-width="1.8"', html)
        for name in ("nope", "../LICENSE", "Tag", ""):
            with self.subTest(name=name):
                self.assertEqual(Template('{% load icon_tags %}{% icon n %}').render(Context({"n": name})), "")
        html = Template('{% load icon_tags %}{% icon "tag" class=c %}').render(Context({"c": '"><script>'}))
        self.assertNotIn("<script>", html)

    def test_every_icon_named_in_a_template_is_vendored(self):
        root = Path(settings.BASE_DIR)
        used = set()
        for path in list((root / "templates").rglob("*.html")) + list((root / "core").rglob("templates/**/*.html")):
            used |= set(re.findall(r'\{%\s*icon\s+"([^"]+)"', path.read_text(encoding="utf-8")))
        self.assertTrue(used)
        self.assertEqual(sorted(used - set(available_icons())), [])


class SidebarIconTests(ShellBase):
    def test_sidebar_uses_the_mapped_lucide_icons_and_keeps_the_logo(self):
        self.client.force_login(self.admin)
        html = self.client.get(reverse("cpanel:script_list")).content.decode()
        aside = html[html.index('<aside id="app-sidebar"'):html.index("</aside>")]
        for label, name in SIDEBAR.items():
            with self.subTest(section=label):
                body = icon_body(name)
                # The link named ``label`` (a whole word: "Connect" isn't "Connections"); an
                # icon may serve two entries (Connect and Triggers share plug-zap).
                named = re.compile(r"(?<!\w)" + re.escape(label) + r"(?!\w)")
                links = [
                    m.group(0) for m in re.finditer(r"<a [^>]*>(?:(?!</a>).)*?</a>", aside, re.S)
                    if named.search(re.sub(r"<[^>]+>", " ", m.group(0)))
                ]
                self.assertEqual(len(links), 1, f"one sidebar link named {label}")
                self.assertIn(body, links[0], f"{label} should use {name}")
        self.assertIn('d="M13 2 4.5 13.5H11l-1 8.5L19.5 10H13z"', aside)  # lightning-bolt logo kept

    def test_plugin_icon_svg_still_renders_with_lucide_fallback(self):
        from core.plugins import NavItem, PyRunnerPlugin, register, unregister

        custom = '<path d="M1 1h22v22H1z"/>'
        register(PyRunnerPlugin(slug="iconful", name="Iconful", nav_items=[
            NavItem(label="Own icon", url_name="cpanel:dashboard", icon_svg=custom),
            NavItem(label="No icon", url_name="cpanel:dashboard"),
        ]))
        self.addCleanup(unregister, "iconful")
        self.client.force_login(self.admin)
        html = self.client.get(reverse("cpanel:script_list")).content.decode()
        aside = html[html.index('<aside id="app-sidebar"'):html.index("</aside>")]
        own = re.search(r"<a [^>]*>(?:(?!</a>).)*Own icon(?:(?!</a>).)*</a>", aside, re.S).group(0)
        self.assertIn(custom, own)
        fallback = re.search(r"<a [^>]*>(?:(?!</a>).)*No icon(?:(?!</a>).)*</a>", aside, re.S).group(0)
        self.assertIn(icon_body("puzzle"), fallback)
