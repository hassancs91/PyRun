"""
GitHub for scripts: list, open, update and comment on issues, read and write files, and start Actions workflows.

A repository is named ``"owner/name"`` (``"octocat/Hello-World"``). Issues and
workflow runs are GitHub's own dicts (``number``, ``title``, ``state``,
``html_url``, ``user``, ``labels``...). Lists come page by page as you read
them; ``limit`` stops early.

The connection is a GitHub account, through Connect (an OAuth app) or a pasted
personal access token. Its scopes decide what it may do: ``public_repo`` (or
``repo`` for private repositories) for issues and files, ``workflow`` to
change files under ``.github/workflows``; a fine-grained token needs the
Issues, Contents and Actions permissions. Starting a workflow needs the
``repo`` scope (``public_repo`` isn't enough; a fine-grained token: Actions
read and write).

GitHub's rate limit (5,000 requests an hour for an account) answers 403 or
429 when it runs out: that raises RATE_LIMITED, with ``retry_after`` the
seconds until it resets.
"""

import base64
import time

from . import _client

PROVIDER = "github"
PAGE_SIZE = 100  # items per page (GitHub's maximum)
API_VERSION = "2022-11-28"
# Sent with every request: GitHub's JSON media type and the REST API version the module was written for.
HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": API_VERSION}
_RAW = "application/vnd.github.raw+json"


def _api(connection):
    return _client.shared(connection, PROVIDER)


def _rate_limited(error):
    """The RATE_LIMITED error for GitHub's 403/429 "rate limit exceeded", else ``error`` as it is."""
    response = error.response
    if response is None or error.status not in (403, 429):
        return error
    headers = response.headers
    wait = _client._retry_after(headers)
    if wait is None and headers.get("x-ratelimit-remaining") == "0":
        try:
            wait = max(0.0, float(headers.get("x-ratelimit-reset")) - time.time())
        except (TypeError, ValueError):
            wait = None
    if wait is None and "rate limit" not in str(error).lower():
        return error
    return _client.IntegrationError(str(error), "RATE_LIMITED", error.status, wait, response)


def _call(connection, method, path, *, headers=None, **kwargs):
    try:
        return _api(connection).call(method, path, headers={**HEADERS, **(headers or {})}, **kwargs)
    except _client.IntegrationError as e:
        raise _rate_limited(e) from None


def _pages(connection, path, params, limit, items=None):
    """Yield every item of a GitHub list (Link-header pages), with the rate limit mapped as in ``_call``."""
    pages = _api(connection).paginate(path, params={**params, "per_page": _page_size(limit)}, items=items,
                                      link=True, limit=limit, headers=dict(HEADERS))
    try:
        yield from pages
    except _client.IntegrationError as e:
        raise _rate_limited(e) from None


def _page_size(limit) -> int:
    return min(PAGE_SIZE, limit) if limit and limit > 0 else PAGE_SIZE


def _repo(repo) -> str:
    """``/repos/<owner>/<name>`` for ``"owner/name"``."""
    owner, sep, name = str(repo).strip("/").partition("/")
    if not sep or not owner or not name or "/" in name:
        raise ValueError(f"A repository is named 'owner/name', not {repo!r}.")
    return f"/repos/{_client.quote(owner)}/{_client.quote(name)}"


def _names(value) -> list:
    return [value] if isinstance(value, str) else list(value)


def _content_path(repo, path) -> str:
    parts = [p for p in str(path).split("/") if p]
    if not parts:
        raise ValueError("A file path is needed (e.g. 'README.md' or 'docs/index.md').")
    return f"{_repo(repo)}/contents/" + "/".join(_client.quote(p) for p in parts)


def client(connection=None, *, timeout=30):
    """The shared client of ``connection``, for GitHub calls this module has no function for.

    Paths are relative to ``https://api.github.com``; send ``headers=github.HEADERS``
    to ask for the API version this module uses. For example:

        pulls = github.client().get("/repos/octocat/Hello-World/pulls", params={"state": "open"},
                                    headers=github.HEADERS)
    """
    return _client.Client(connection, provider=PROVIDER, timeout=timeout)


def list_issues(repo, *, state="open", labels=None, assignee=None, creator=None, since=None, sort="created",
                direction="desc", pull_requests=False, limit=None, connection=None):
    """Yield a repository's issues, newest first unless ``sort``/``direction`` say otherwise.

    ``state`` is ``"open"``, ``"closed"`` or ``"all"``. ``labels`` (one name or
    a list) keeps issues with every one of them; ``assignee`` / ``creator`` a
    login (``assignee="none"`` for unassigned); ``since`` an ISO time: issues
    updated since. ``sort`` is ``created``, ``updated`` or ``comments``. Pull
    requests (which GitHub lists as issues) are left out unless
    ``pull_requests=True``. For example:

        for issue in github.list_issues("octo/shop", labels="bug", limit=20):
            print(issue["number"], issue["title"], issue["html_url"])
    """
    params = {"state": state, "sort": sort, "direction": direction}
    if labels:
        params["labels"] = ",".join(_names(labels))
    if assignee:
        params["assignee"] = assignee
    if creator:
        params["creator"] = creator
    if since:
        params["since"] = since
    # Pull requests are filtered here, so a page may yield fewer items: the limit counts what is yielded.
    found = _pages(connection, f"{_repo(repo)}/issues", params, None if not pull_requests else limit)
    return _client.limited((i for i in found if pull_requests or "pull_request" not in i), limit)


def get_issue(repo, number, *, connection=None):
    """One issue (or pull request) by its number; one that doesn't exist raises NOT_FOUND.

    For example:

        issue = github.get_issue("octo/shop", 42)
        print(issue["state"], [label["name"] for label in issue["labels"]])
    """
    return _call(connection, "GET", f"{_repo(repo)}/issues/{int(number)}")


def create_issue(repo, title, body=None, *, labels=None, assignees=None, milestone=None, connection=None):
    """Open an issue and return it (its ``number`` and ``html_url`` among the rest).

    ``body`` is Markdown. ``labels`` and ``assignees`` are one name/login or a
    list (setting them needs push access; without it GitHub drops them
    silently); ``milestone`` is a milestone's number. For example:

        issue = github.create_issue("octo/shop", "Checkout fails on Safari", "Steps:\\n1. ...", labels=["bug"])
        print(issue["html_url"])
    """
    data = {"title": str(title)}
    if body is not None:
        data["body"] = body
    if labels:
        data["labels"] = _names(labels)
    if assignees:
        data["assignees"] = _names(assignees)
    if milestone is not None:
        data["milestone"] = milestone
    return _call(connection, "POST", f"{_repo(repo)}/issues", json=data)


def update_issue(repo, number, *, title=None, body=None, state=None, state_reason=None, labels=None,
                 assignees=None, connection=None):
    """Change an issue and return it as saved; only what is given changes.

    ``state`` is ``"open"`` or ``"closed"`` (``state_reason``: ``"completed"``,
    ``"not_planned"`` or ``"reopened"``). ``labels`` / ``assignees`` replace the
    current ones (``[]`` removes them all). For example:

        github.update_issue("octo/shop", 42, state="closed", state_reason="completed")
        github.update_issue("octo/shop", 43, labels=["bug", "triaged"])
    """
    data = {}
    for key, value in (("title", title), ("body", body), ("state", state), ("state_reason", state_reason)):
        if value is not None:
            data[key] = value
    if labels is not None:
        data["labels"] = _names(labels)
    if assignees is not None:
        data["assignees"] = _names(assignees)
    if not data:
        raise ValueError("update_issue() needs something to change (title=, body=, state=, labels=...).")
    return _call(connection, "PATCH", f"{_repo(repo)}/issues/{int(number)}", json=data)


def add_comment(repo, number, body, *, connection=None):
    """Comment on an issue or pull request (``body`` is Markdown); returns the comment (``id``, ``html_url``).

    For example:

        github.add_comment("octo/shop", 42, "Fixed in v2.5 :tada:")
    """
    return _call(connection, "POST", f"{_repo(repo)}/issues/{int(number)}/comments", json={"body": str(body)})


def list_comments(repo, number, *, limit=None, connection=None):
    """Yield an issue's comments, oldest first (``id``, ``user``, ``body``, ``created_at``...).

    For example:

        for comment in github.list_comments("octo/shop", 42):
            print(comment["user"]["login"], comment["body"][:80])
    """
    return _pages(connection, f"{_repo(repo)}/issues/{int(number)}/comments", {}, limit)


def get_file(repo, path, *, ref=None, binary=False, connection=None):
    """A file's contents and details: ``{"path", "name", "sha", "size", "html_url", "content"}``.

    ``content`` is the text (UTF-8), or bytes with ``binary=True``. ``ref`` is a
    branch, tag or commit (default: the repository's default branch). Files up
    to 100 MB; a directory raises ValueError. Keep ``sha`` to change the file
    with ``put_file``. For example:

        readme = github.get_file("octo/shop", "README.md")
        print(readme["content"][:200])
        logo = github.get_file("octo/shop", "docs/logo.png", binary=True)["content"]
    """
    where = _content_path(repo, path)
    params = {"ref": ref} if ref else None
    meta = _call(connection, "GET", where, params=params)
    if isinstance(meta, list) or not isinstance(meta, dict) or meta.get("type") not in (None, "file"):
        kind = "a directory" if isinstance(meta, list) else f"a {meta.get('type')}" if isinstance(meta, dict) \
            else "not a file"
        raise ValueError(f"{path!r} in {repo} is {kind}, not a file.")
    if meta.get("encoding") == "base64":
        data = base64.b64decode(meta.get("content") or "")
    else:
        # Over 1 MB GitHub leaves the content out (encoding "none"): ask for the raw bytes.
        try:
            data = _api(connection).request("GET", where, params=params, headers={**HEADERS, "Accept": _RAW}).content
        except _client.IntegrationError as e:
            raise _rate_limited(e) from None
    if binary:
        content = data
    else:
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError(f"{path!r} in {repo} isn't UTF-8 text: pass binary=True for its bytes.") from None
    keep = ("path", "name", "sha", "size", "html_url", "download_url")
    return {**{k: meta.get(k) for k in keep}, "content": content}


def put_file(repo, path, content, message, *, branch=None, sha=None, connection=None):
    """Create or replace a file with one commit; returns ``{"content": <the file>, "commit": <the commit>}``.

    ``content`` is text (saved as UTF-8) or bytes; ``message`` the commit
    message; ``branch`` defaults to the repository's default branch. Replacing
    a file needs its current ``sha``: given, it must still be the file's (else
    CONFLICT: someone changed it meanwhile); not given, it is looked up first.
    A file under ``.github/workflows`` needs the ``workflow`` scope. For example:

        github.put_file("octo/site", "data/stats.json", json.dumps(stats, indent=2), "Update stats")
    """
    where = _content_path(repo, path)
    raw = content.encode("utf-8") if isinstance(content, str) else bytes(content)
    if sha is None:
        try:
            current = _call(connection, "GET", where, params={"ref": branch} if branch else None)
        except _client.IntegrationError as e:
            if e.code != "NOT_FOUND":
                raise
            current = None
        if isinstance(current, list):
            raise ValueError(f"{path!r} in {repo} is a directory, not a file.")
        sha = current.get("sha") if isinstance(current, dict) else None
    data = {"message": str(message), "content": base64.b64encode(raw).decode("ascii")}
    if branch:
        data["branch"] = branch
    if sha:
        data["sha"] = sha
    # Not repeated after a 5xx: a commit that went through would make the repeat a CONFLICT.
    return _call(connection, "PUT", where, json=data, idempotent=False)


def dispatch_workflow(repo, workflow, ref=None, inputs=None, *, connection=None):
    """Start a workflow that has a ``workflow_dispatch`` trigger; returns GitHub's reply (None: it says nothing).

    ``workflow`` is the workflow's file name (``"deploy.yml"``) or its id;
    ``ref`` the branch or tag to run it on (default: the repository's default
    branch); ``inputs`` the workflow's inputs (``{"environment": "staging"}``).
    Needs the ``repo`` scope (a fine-grained token: Actions read and write).
    GitHub starts the run a moment later, and
    ``list_workflow_runs(repo, event="workflow_dispatch", limit=1)`` finds it. For example:

        github.dispatch_workflow("octo/shop", "deploy.yml", inputs={"environment": "staging"})
    """
    base = _repo(repo)
    if not ref:
        ref = _call(connection, "GET", base)["default_branch"]
    data = {"ref": ref}
    if inputs:
        data["inputs"] = dict(inputs)
    return _call(connection, "POST", f"{base}/actions/workflows/{_client.quote(workflow)}/dispatches", json=data)


def list_workflow_runs(repo, workflow=None, *, branch=None, status=None, event=None, limit=None, connection=None):
    """Yield a repository's Actions runs, newest first (``id``, ``status``, ``conclusion``, ``html_url``...).

    ``workflow`` (a file name or id) keeps that workflow's runs; ``branch``,
    ``event`` (``"push"``, ``"workflow_dispatch"``...) and ``status``
    (``"queued"``, ``"in_progress"``, ``"completed"``, ``"success"``,
    ``"failure"``...) filter them. For example:

        for run in github.list_workflow_runs("octo/shop", "deploy.yml", limit=5):
            print(run["id"], run["status"], run["conclusion"], run["html_url"])
    """
    base = _repo(repo)
    path = f"{base}/actions/workflows/{_client.quote(workflow)}/runs" if workflow else f"{base}/actions/runs"
    params = {k: v for k, v in (("branch", branch), ("status", status), ("event", event)) if v}
    return _pages(connection, path, params, limit, items="workflow_runs")


def new_issues(repo, state=None, *, labels=None, limit=None, connection=None):
    """The issues opened since the last call, oldest first, and the state to pass next time: ``(issues, state)``.

    For a scheduled script that acts on new issues (and the "new GitHub issue"
    watcher). ``state`` is what the previous call returned (a dict to keep, for
    example in a datastore); the first call (``state=None``) returns no issues
    and marks the newest one. Issues count by number, so one opened and closed
    between two calls is still reported; pull requests are left out.
    ``labels`` (one name or a list) keeps issues that have them when the call
    looks (one labelled after a newer issue was reported is missed). ``limit``
    caps how many one call returns; the rest come next time. For example:

        issues, state = github.new_issues("octo/shop", store.get("issues_state"))
        for issue in issues:
            slack.post_message("#triage", f"New issue: <{issue['html_url']}|{issue['title']}>")
        store.set("issues_state", state)
    """
    params = {"state": "all", "sort": "created", "direction": "desc"}
    if labels:
        params["labels"] = ",".join(_names(labels))
    path = f"{_repo(repo)}/issues"
    if not state or "last_number" not in state:
        newest = next(iter(_pages(connection, path, params, 1)), None)
        return [], {"last_number": int(newest["number"]) if newest else 0}
    mark = int(state["last_number"])
    if limit is not None and limit <= 0:
        return [], {"last_number": mark}
    found, top = [], mark
    for item in _pages(connection, path, params, None):
        number = int(item["number"])
        if number <= mark:
            break
        top = max(top, number)
        if "pull_request" not in item:
            found.append(item)
    found.sort(key=lambda issue: int(issue["number"]))
    if limit is not None and len(found) > limit:
        found = found[:limit]
        top = int(found[-1]["number"])
    return found, {"last_number": top}
