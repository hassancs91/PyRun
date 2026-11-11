"""The github integration: issues, files and Actions workflows on GitHub (see core/integrations/framework.py)."""

from core.integrations.framework import Integration, Smoke

# GitHub's own sample repository: public, so any token (any scope) can read it.
SAMPLE_REPO = "octocat/Hello-World"


def _smoke_read(module, connection, config):
    """Who the token is, then issues and a file of GitHub's public sample repository (read-only)."""
    user = module.client(connection).get("/user", headers=module.HEADERS)
    issues = list(module.list_issues(SAMPLE_REPO, state="all", limit=3, connection=connection))
    readme = module.get_file(SAMPLE_REPO, "README", connection=connection)
    return (f"signed in as {user.get('login')!r}; list_issues answered with {len(issues)} issue(s) and get_file "
            f"with {readme['size']} byte(s) of {SAMPLE_REPO}")


EXAMPLE = '''\
import json
from pyrunner_integrations import github

REPO = "octo/shop"

# Open an issue for a failed check, unless one is open already.
open_bugs = [i["title"] for i in github.list_issues(REPO, labels="nightly-check")]
if "Nightly check failed" not in open_bugs:
    issue = github.create_issue(REPO, "Nightly check failed", "See the run log in PyRun.",
                                labels=["bug", "nightly-check"])
    github.add_comment(REPO, issue["number"], "Assigning to the on-call person.")

# Read a file, change it, commit it back.
config = github.get_file(REPO, "config/flags.json")
flags = json.loads(config["content"])
flags["new_checkout"] = True
github.put_file(REPO, "config/flags.json", json.dumps(flags, indent=2) + "\\n", "Turn on new_checkout",
                sha=config["sha"])

# Start the deploy workflow on main, then look at its latest run.
github.dispatch_workflow(REPO, "deploy.yml", "main", inputs={"environment": "staging"})
for run in github.list_workflow_runs(REPO, "deploy.yml", limit=1):
    print(run["status"], run["html_url"])
'''

INTEGRATION = Integration(
    key="github",
    provider="github",
    title="GitHub",
    functions=("list_issues", "get_issue", "create_issue", "update_issue", "add_comment", "list_comments",
               "get_file", "put_file", "dispatch_workflow", "list_workflow_runs", "new_issues", "client"),
    example=EXAMPLE,
    smoke=(Smoke("read", _smoke_read),),
)
