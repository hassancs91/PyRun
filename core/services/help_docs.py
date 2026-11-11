"""PyRun's own documentation, searchable (2.5 Stage 3, T19): ``help.search``.

The corpus is the public documentation of the running version as it ships in
the image: the README, the guides under ``docs/`` that the repository tracks
(``.gitignore`` keeps the rest of ``docs/`` local; ``.dockerignore`` excludes
Markdown from the image and re-includes exactly these), the changelog, and the
per-integration pages under ``docs/integrations/`` when there are any (plan
Stage 6). The list is explicit, so the internal notes a checkout keeps in
``docs/`` are never read.

Each file is split into sections at its Markdown headings (never inside fenced
code). A query is scored against them with BM25 over the section's words, its
heading's words counted three times and its parent headings' once, times the
share of the query's words the section has. A changelog section counts half,
so a guide outranks the entry that announced its feature. No dependency and no
database: the index is built once per process (``pyrunner.wsgi`` builds it at
boot; any other process on its first search) and kept, since the bundled files
don't change while the process runs.

A result links to its section on GitHub at the running version's tag (the
heading's anchor as GitHub writes it), so an answer cites the docs of the
version that's running.
"""

import logging
import math
import re
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from django.conf import settings

from pyrunner.version import __version__

logger = logging.getLogger(__name__)

# The public docs, in the order a tie is broken. A new public guide goes here,
# in .gitignore's exceptions and in .dockerignore's: core.test_help_search
# checks all three agree.
CORPUS = (
    "README.md",
    "docs/run-context.md",
    "docs/scheduling.md",
    "docs/api.md",
    "docs/plugins.md",
    "docs/brain.md",
    "CHANGELOG.md",
)
# One generated page per integration, <key>.md (plan Stage 6); none before it.
INTEGRATIONS_DIR = "docs/integrations"
REPO_URL = "https://github.com/hassancs91/PyRun"
DOC_WEIGHT = {"CHANGELOG.md": 0.5}
TITLE_WEIGHT = 3
# A page's intro (the text under its "# Title") counts this many times more
# when the query and the title are about the same thing: "how do I schedule a
# script?" should open "Scheduling scripts", not one of its subsections.
INTRO_BOOST = 2
K1, B = 1.2, 0.75
SNIPPET_CHARS = 1000
DEFAULT_LIMIT = 5

FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")
IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
TAG = re.compile(r"<[^>]+>")
WORD = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*")
PARAGRAPH_BREAK = re.compile(r"\n\s*\n")
STOPWORDS = frozenset(
    "a an and are as at be by can do does for from how i if in into is it its me my of on "
    "or so that the then this to up use what when where which why with you your".split()
)


@dataclass(frozen=True)
class Section:
    doc: str  # the file, relative to the app root (and to the repository)
    title: str  # its heading
    trail: str  # the headings above it and its own: "Run context › The run event"
    anchor: str  # GitHub's anchor for the heading ("" for text above the first one)
    text: str  # the body, Markdown with link targets, images and HTML left out
    intro: bool = False  # the text under the page's own "# Title"


@dataclass
class Index:
    sections: list = field(default_factory=list)
    postings: dict = field(default_factory=dict)  # term → {section number: weighted count}
    lengths: list = field(default_factory=list)
    files: list = field(default_factory=list)
    missing: list = field(default_factory=list)
    intros: dict = field(default_factory=dict)  # intro section number → its title's roots

    def add(self, section, counts):
        number = len(self.sections)
        self.sections.append(section)
        self.lengths.append(sum(counts.values()))
        for term, count in counts.items():
            self.postings.setdefault(term, {})[number] = count
        if section.intro and (title_roots := roots(section.title)):
            self.intros[number] = title_roots


def plain(markdown):
    """Markdown with images, link targets, HTML tags and ``&nbsp;`` left out."""
    text = IMAGE.sub(r"\1", markdown)
    text = LINK.sub(r"\1", text)
    return TAG.sub(" ", text).replace("&nbsp;", " ")


def stem(word):
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def terms(text):
    """The searchable words of ``text``: lowercased, plurals folded, stopwords out.

    A dotted, snake_case or hyphenated name counts whole and by its parts, so
    ``datastores.set`` matches a search for it and one for "datastore".
    """
    out = []
    for word in WORD.findall(text.lower()):
        parts = re.split(r"[._-]", word)
        if len(parts) > 1:
            out.append(word)
        for part in parts:
            if part in STOPWORDS:
                continue
            part = stem(part)
            if part not in STOPWORDS:
                out.append(part)
    return out


def roots(text):
    """``text``'s terms with common endings off, to compare a query with a title.

    Coarser than ``terms`` (only used for the intro boost): "schedule" and
    "Scheduling" both give "schedul".
    """
    out = set()
    for term in terms(text):
        for ending in ("ing", "ed", "es", "e", "s"):
            if term.endswith(ending) and len(term) - len(ending) >= 4:
                term = term[: -len(ending)]
                break
        out.add(term)
    return frozenset(out)


def anchor_for(title):
    """GitHub's anchor for a heading: lowercase, punctuation out, spaces to hyphens."""
    return re.sub(r"[^\w\- ]", "", title.strip().lower()).replace(" ", "-")


def split_sections(doc, markdown):
    """``markdown``'s sections, one per heading outside fenced code; empty ones left out."""
    sections, parents, seen = [], [], Counter()
    title, anchor, body, fence = doc, "", [], None
    intro, had_page_title = False, False

    def close():
        text = "\n".join(line.rstrip() for line in plain("\n".join(body)).splitlines())
        text = PARAGRAPH_BREAK.sub("\n\n", text).strip()
        if text:
            trail = " › ".join(t for _, t in parents) or title
            sections.append(Section(doc, title, trail, anchor, text, intro))

    for line in markdown.splitlines():
        marker = FENCE.match(line)
        if fence is not None:
            body.append(line)
            if marker and marker.group(1)[0] == fence[0] and len(marker.group(1)) >= len(fence):
                fence = None
            continue
        if marker:
            fence = marker.group(1)
            body.append(line)
            continue
        heading = HEADING.match(line)
        if heading is None or not heading.group(2).strip():
            body.append(line)
            continue
        close()
        level = len(heading.group(1))
        intro = level == 1 and not had_page_title
        had_page_title = had_page_title or level == 1
        title = plain(heading.group(2)).replace("**", "").strip()
        slug = anchor_for(title)
        anchor = f"{slug}-{seen[slug]}" if seen[slug] else slug
        seen[slug] += 1
        parents = [(lvl, t) for lvl, t in parents if lvl < level] + [(level, title)]
        body = []
    close()
    return sections


def section_terms(section):
    """``section``'s words, its heading's counted TITLE_WEIGHT times, its parents' once."""
    counts = Counter(terms(section.text))
    for term in terms(section.title):
        counts[term] += TITLE_WEIGHT
    above = section.trail.rsplit(" › ", 1)[0] if " › " in section.trail else ""
    for term in terms(above):
        counts[term] += 1
    return counts


def build_index(root):
    """The index of the corpus under ``root``; a file it can't read is left out."""
    root = Path(root)
    docs = list(CORPUS)
    integrations = root / INTEGRATIONS_DIR
    if integrations.is_dir():
        docs += sorted(f"{INTEGRATIONS_DIR}/{path.name}" for path in integrations.glob("*.md"))
    index = Index()
    for doc in docs:
        try:
            markdown = (root / doc).read_text(encoding="utf-8", errors="replace")
        except OSError:
            index.missing.append(doc)
            continue
        index.files.append(doc)
        for section in split_sections(doc, markdown):
            index.add(section, section_terms(section))
    return index


_index = None
_lock = threading.Lock()


def get_index():
    """This process's index, built on first use (and logged once)."""
    global _index
    if _index is None:
        with _lock:
            if _index is None:
                started = time.monotonic()
                index = build_index(settings.BASE_DIR)
                if not index.files:
                    logger.warning(
                        "help.search: no bundled docs under %s (a checkout without docs?); "
                        "help.search finds nothing", settings.BASE_DIR,
                    )
                else:
                    if index.missing:
                        logger.warning("help.search: bundled docs missing or unreadable: %s",
                                       ", ".join(index.missing))
                    logger.info("help.search: indexed %d sections from %d files in %d ms",
                                len(index.sections), len(index.files),
                                int((time.monotonic() - started) * 1000))
                _index = index
    return _index


def warm():
    """Build the index now (a web process's boot) rather than on the first search; never raises."""
    try:
        get_index()
    except Exception:
        logger.exception("help.search: building the docs index failed")


def link(section):
    url = f"{REPO_URL}/blob/v{__version__}/{section.doc}"
    return f"{url}#{section.anchor}" if section.anchor else url


def snippet(text, wanted):
    """The section when short; else from its best paragraph on, about SNIPPET_CHARS long."""
    if len(text) <= SNIPPET_CHARS:
        return text
    paragraphs = text.split("\n\n")
    best = max(range(len(paragraphs)),
               key=lambda i: (len(wanted & set(terms(paragraphs[i]))), -i))
    out = "\n\n".join(paragraphs[best:])
    if len(out) > SNIPPET_CHARS:
        cut = max(out.rfind(" ", 0, SNIPPET_CHARS), out.rfind("\n", 0, SNIPPET_CHARS))
        out = out[: cut if cut > 0 else SNIPPET_CHARS].rstrip() + " …"
    return f"… {out}" if best else out


def search(query, limit=DEFAULT_LIMIT, index=None):
    """The best ``limit`` sections for ``query``, best first."""
    index = get_index() if index is None else index
    wanted = list(dict.fromkeys(terms(query)))
    asked = roots(query)
    scores, matched = defaultdict(float), Counter()
    total = len(index.sections)
    if total:
        average = sum(index.lengths) / total
        for term in wanted:
            posting = index.postings.get(term, {})
            idf = math.log(1 + (total - len(posting) + 0.5) / (len(posting) + 0.5))
            for number, count in posting.items():
                length = index.lengths[number] / average
                scores[number] += idf * count * (K1 + 1) / (count + K1 * (1 - B + B * length))
                matched[number] += 1

    def final(number):
        weight = DOC_WEIGHT.get(index.sections[number].doc, 1)
        title = index.intros.get(number)
        if title and asked and (title <= asked or asked <= title):
            weight *= INTRO_BOOST  # the query names the page, or the page's title is the query
        return scores[number] * matched[number] / len(wanted) * weight

    ranked = sorted(scores, key=lambda number: (-final(number), number))[:limit]
    results = []
    for number in ranked:
        section = index.sections[number]
        results.append({
            "title": section.title,
            "section": section.trail,
            "doc": section.doc,
            "link": link(section),
            "snippet": snippet(section.text, set(wanted)),
        })
    return {
        "query": query,
        "version": __version__,
        "docs_bundled": bool(index.files),
        "results": results,
    }
