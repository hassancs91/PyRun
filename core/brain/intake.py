"""Capture a source once; fetching and extraction never write a brain revision."""

import hashlib
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from html.parser import HTMLParser

from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from core.api.exceptions import ApiError
from core.api.schema import validate
from core.models import BrainFeed
from core.netguard import guarded_opener, is_safe_endpoint_url
from .access import access_for, resolve, unknown
from .imports import _NoRedirect
from .paths import utf8_size
from .service import BrainService

log = logging.getLogger(__name__)
MAX_PAYLOAD = 512 * 1024
SOURCE_ID_MAX = BrainFeed._meta.get_field("source_id").max_length
SLUG_HASH = 6
SOURCE_KINDS = ("doc", "article", "video", "podcast", "thread", "thought", "conversation", "other")
INTAKE_PROPERTIES = {
    "source_kind": {"type": "string", "enum": list(SOURCE_KINDS)},
    "title": {"type": "string", "maxLength": 500},
    "url": {"type": "string", "maxLength": 2048},
    "content": {"type": "string", "maxLength": MAX_PAYLOAD},
    "notes": {"type": "string", "maxLength": MAX_PAYLOAD},
}


class HTMLText(HTMLParser):
    SKIP = {"script", "style", "noscript", "template", "head", "svg"}
    BREAK = {"p", "div", "br", "li", "h1", "h2", "h3", "section", "article"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = []
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.hidden.append(tag)
        if not self.hidden and tag in self.BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self.hidden and tag == self.hidden[-1]:
            self.hidden.pop()
        if not self.hidden and tag in self.BREAK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def safe_url(url):
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
            raise ValueError
        safe, reason = is_safe_endpoint_url(url)
    except (ValueError, TypeError):
        raise ApiError(
            "unsafe_url", "Enter an allowed HTTP or HTTPS URL without credentials."
        ) from None
    if not safe:
        # Name the guard's reason (cloud metadata, private network + the flag), like
        # notification webhooks do since F47 (U66).
        raise ApiError("unsafe_url", f"This URL is not allowed: {reason}")


def fetch_text(url, *, deadline=None):
    """One 15-second budget, three explicitly checked redirects, bounded reads."""
    end = min(deadline or float("inf"), time.monotonic() + 15)
    opener = guarded_opener(_NoRedirect())
    for hop in range(4):
        safe_url(url)
        remaining = end - time.monotonic()
        if remaining <= 0:
            raise ApiError("fetch_failed", "URL fetch timed out. Paste the source content instead.")
        request = urllib.request.Request(
            url, headers={"User-Agent": "PyRun-Brain/2", "Accept-Encoding": "identity"}
        )
        try:
            response = opener.open(request, timeout=remaining)
        except urllib.error.HTTPError as exc:
            with exc:
                if (
                    exc.code in {301, 302, 303, 307, 308}
                    and hop < 3
                    and exc.headers.get("Location")
                ):
                    url = urllib.parse.urljoin(url, exc.headers["Location"])
                    continue
            raise ApiError(
                "fetch_failed",
                "URL fetch failed or exceeded three redirects. Paste the source content.",
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ApiError(
                "fetch_failed", "URL fetch failed. Paste the source content or retry later."
            ) from None
        with response:
            media = response.headers.get_content_type()
            if media not in {"text/plain", "text/html", "text/markdown", "application/xhtml+xml"}:
                raise ApiError("fetch_failed", "URL did not return text. Paste the source content.")
            data = bytearray()
            try:
                while True:
                    remaining = end - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError
                    # urllib's timeout otherwise restarts at its original value
                    # for each socket read, allowing a slow stream past budget.
                    sock = getattr(
                        getattr(getattr(response, "fp", None), "raw", None), "_sock", None
                    )
                    if sock is not None:
                        sock.settimeout(remaining)
                    chunk = response.read1(min(16384, MAX_PAYLOAD + 1 - len(data)))
                    if not chunk:
                        break
                    data.extend(chunk)
                    if len(data) > MAX_PAYLOAD:
                        raise ApiError(
                            "too_large", "Source exceeds 512 KB. Paste a shorter excerpt.", 413
                        )
            except (TimeoutError, OSError):
                raise ApiError(
                    "fetch_failed", "URL fetch timed out. Paste the source content."
                ) from None
            encoding = response.headers.get_content_charset() or "utf-8"
            try:
                content = bytes(data).decode(encoding, errors="replace")
            except LookupError:
                content = bytes(data).decode("utf-8", errors="replace")
        if media in {"text/html", "application/xhtml+xml"}:
            parser = HTMLText()
            parser.feed(content)
            content = "\n".join(
                line.strip() for line in "".join(parser.parts).splitlines() if line.strip()
            )
        if not content.strip():
            raise ApiError(
                "fetch_failed", "URL returned no readable text. Paste the source content."
            )
        return content
    raise ApiError("fetch_failed", "URL exceeded three redirects.")


def source_slug(title, url, content):
    """The last part of a source id: the title, else the URL path, else a content hash.

    An untitled source with no URL path is named by a short hash of its content
    (``3f9a1c``), never by its words: a source id is cited by every note and
    shown in approvals, and a thought's opening words can be private.
    """
    for text in (title, urllib.parse.urlsplit(url).path):
        if slug := slugify(text)[:60]:
            return slug
    return hashlib.sha256(content.encode("utf-8", "surrogatepass")).hexdigest()[:SLUG_HASH]


def unique_source_id(brain, source_id, feed_id):
    """Keep the readable id unless this brain already has it; then append the new feed's id.

    Called under the brain row lock, so two proposals can't both take one id. The
    suffix grows on the rare clash; the full feed id is unique.
    """
    feeds = BrainFeed.objects.filter(brain=brain)
    if not feeds.filter(source_id=source_id).exists():
        return source_id
    for size in (4, 8, 12, 32):
        keep = SOURCE_ID_MAX - size - 1  # trim the readable part, never the suffix
        candidate = f"{source_id[:keep].rstrip('-')}-{feed_id.hex[:size]}"
        if size == 32 or not feeds.filter(source_id=candidate).exists():
            return candidate


def enqueue(workspace_id, feed_id):
    from django_q.tasks import async_task

    try:
        async_task("core.brain.feeder.extract", str(workspace_id), str(feed_id))
    except Exception:
        log.exception("brain_enqueue_failed workspace=%s feed=%s", workspace_id, feed_id)
        BrainFeed.objects.filter(
            pk=feed_id, brain__workspace_id=workspace_id, status="pending"
        ).update(queued_at=None, error="The worker queue is unavailable. Retry from the inbox.")


def capture(workspace, brain_id, params, *, channel, principal, deadline=None, ctx=None):
    validate(
        params, {"type": "object", "properties": INTAKE_PROPERTIES, "additionalProperties": False}
    )
    title, url = params.get("title", "").strip(), params.get("url", "").strip()
    content, notes = params.get("content", ""), params.get("notes", "")
    if not content.strip() and not url:
        raise ApiError("invalid_input", "Provide source content or a URL.")
    if channel == "sync" or channel not in dict(BrainFeed._meta.get_field("channel").choices) or len(principal) > 256:
        raise ApiError("invalid_input", "Unknown intake channel or principal.")
    error = ""
    if url:
        safe_url(url)
    if url and not content.strip():
        try:
            content = fetch_text(url, deadline=deadline)
        except ApiError as exc:
            if exc.code == "unsafe_url":
                raise
            error = exc.message
    if sum(utf8_size(value) for value in (title, url, content, notes)) > MAX_PAYLOAD:
        raise ApiError(
            "too_large", "A source payload may contain at most 512 KB of UTF-8 text.", 413
        )
    kind = params.get("source_kind", "doc")
    source_id = f"{kind}-{timezone.localdate():%Y-%m}-{source_slug(title, url, content)}"
    with transaction.atomic():
        brain = BrainService.get(workspace, brain_id, lock=True)
        if deadline is not None and time.monotonic() >= deadline:
            raise ApiError("timeout", "The call exceeded its time budget before capture.", 504)
        if ctx is not None and not access_for(ctx, brain).can_propose:
            raise unknown()
        feed_id = uuid.uuid4()
        feed = BrainFeed.objects.create(
            id=feed_id,
            brain=brain,
            base_revision=brain.head_revision,
            source_id=unique_source_id(brain, source_id, feed_id),
            channel=channel,
            principal=principal,
            error=error,
            raw_payload={
                "source_kind": kind,
                "title": title,
                "source_url": url,
                "content": content,
                "notes": notes,
            },
            queued_at=None if error else timezone.now(),
        )
        if not error:
            transaction.on_commit(lambda: enqueue(workspace.pk, feed.pk))
    return feed


def propose(ctx, params):
    brain, access = resolve(ctx, params)
    if not access.can_propose:
        raise unknown()
    channel = {"rest": "api", "mcp": "mcp", "telegram": "telegram"}.get(
        ctx.transport, ctx.principal
    )
    feed = capture(
        ctx.workspace,
        brain.pk,
        {k: v for k, v in params.items() if k != "brain"},
        channel=channel,
        principal=f"{ctx.principal}:{ctx.principal_id}",
        deadline=ctx.deadline,
        ctx=ctx,
    )
    return {
        "feed_id": str(feed.pk),
        "source_id": feed.source_id,
        "status": feed.status,
        "message": "Source captured for human review.",
        "error": feed.error,
    }
