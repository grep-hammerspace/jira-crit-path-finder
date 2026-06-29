"""
seed_jira.py

Populate a JIRA Cloud project with a sample set of issues + dependencies so you
can test the Critical Path Finder against a real board instead of hand-creating
tickets.

It creates each issue (with an Original Estimate) and then wires up the
"Blocks" links between them. Those are exactly the fields the app reads back:
  - timeoriginalestimate  -> task duration
  - "blocks" / "is blocked by" issue links -> the dependency graph

Usage:
    export JIRA_BASE="https://YOURSITE.atlassian.net"
    export JIRA_EMAIL="you@example.com"
    export JIRA_TOKEN="<api token from id.atlassian.com/manage-profile/security/api-tokens>"
    export JIRA_PROJECT="CPF"        # your project key
    python seed_jira.py              # add --delete-existing to wipe seeded issues first (DESTRUCTIVE)

Notes:
  - "Time tracking" must be enabled in the project for Original Estimate to take.
    If it isn't, issues still get created; durations just fall back to the app's
    1-day default.
  - The default issue type is "Task". Override with JIRA_ISSUETYPE if your
    project doesn't have one.
"""
from __future__ import annotations

import os
import sys

import requests

JIRA_BASE = os.environ.get("JIRA_BASE", "").rstrip("/")
JIRA_EMAIL = os.environ.get("JIRA_EMAIL", "")
JIRA_TOKEN = os.environ.get("JIRA_TOKEN", "")
JIRA_PROJECT = os.environ.get("JIRA_PROJECT", "")
JIRA_ISSUETYPE = os.environ.get("JIRA_ISSUETYPE", "Task")

AUTH = (JIRA_EMAIL, JIRA_TOKEN)
HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}

# ---------------------------------------------------------------------------
# Sample task graph: building the Critical Path Finder itself.
#
# Each task has a local id (used only here to express dependencies), a summary,
# an original estimate in JIRA's format ("2d", "4h", "1d 4h"), optional labels,
# and a list of local ids it is BLOCKED BY (i.e. must finish before this starts).
#
# Tweak freely. The shape below has a clear critical chain plus some parallel
# work with float, which makes for a good test of the CPM output.
# ---------------------------------------------------------------------------
TASKS = [
    {"id": "scaffold", "summary": "Project scaffolding & Flask app skeleton",
     "estimate": "1d", "labels": ["backend"], "blocked_by": []},

    {"id": "scraper", "summary": "Page scraper: discover JIRA base + issue keys",
     "estimate": "2d", "labels": ["backend"], "blocked_by": ["scaffold"]},

    {"id": "client", "summary": "JIRA REST client: fetch issue fields + links",
     "estimate": "3d", "labels": ["backend"], "blocked_by": ["scaffold"]},

    {"id": "graph", "summary": "Build dependency graph from blocks links",
     "estimate": "2d", "labels": ["algo"], "blocked_by": ["client"]},

    {"id": "cpm", "summary": "Implement CPM forward/backward pass + float",
     "estimate": "3d", "labels": ["algo"], "blocked_by": ["graph"]},

    {"id": "cycles", "summary": "Cycle detection & edge-dropping for invalid DAGs",
     "estimate": "1d", "labels": ["algo"], "blocked_by": ["graph"]},

    {"id": "ui", "summary": "Results UI: critical chain + per-issue table",
     "estimate": "2d", "labels": ["frontend"], "blocked_by": ["cpm"]},

    {"id": "style", "summary": "Styling & polish",
     "estimate": "1d", "labels": ["frontend"], "blocked_by": ["ui"]},

    {"id": "tests", "summary": "CPM unit tests against a known worked example",
     "estimate": "1d 4h", "labels": ["qa"], "blocked_by": ["cpm"]},

    {"id": "docs", "summary": "README + usage docs",
     "estimate": "4h", "labels": ["docs"], "blocked_by": ["scaffold"]},

    {"id": "release", "summary": "End-to-end test & release",
     "estimate": "1d", "labels": ["release"], "blocked_by": ["style", "tests", "cycles", "docs"]},
]

SEED_LABEL = "cpf-seed"  # tag everything so it's easy to find/clean up later


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def check_config() -> None:
    missing = [n for n, v in [
        ("JIRA_BASE", JIRA_BASE), ("JIRA_EMAIL", JIRA_EMAIL),
        ("JIRA_TOKEN", JIRA_TOKEN), ("JIRA_PROJECT", JIRA_PROJECT),
    ] if not v]
    if missing:
        die("Missing required env vars: " + ", ".join(missing))


def find_blocks_link_type() -> str:
    """Return the name of the 'Blocks' issue link type (usually 'Blocks')."""
    r = requests.get(f"{JIRA_BASE}/rest/api/3/issueLinkType", auth=AUTH, headers=HEADERS)
    r.raise_for_status()
    for lt in r.json().get("issueLinkTypes", []):
        if lt.get("outward", "").lower() == "blocks":
            return lt["name"]
    die("This JIRA instance has no 'Blocks' link type. Add one under "
        "Settings > Issues > Issue linking, then re-run.")


def create_issue(task: dict) -> str:
    fields = {
        "project": {"key": JIRA_PROJECT},
        "summary": task["summary"],
        "issuetype": {"name": JIRA_ISSUETYPE},
        "labels": [SEED_LABEL] + task.get("labels", []),
    }
    if task.get("estimate"):
        fields["timetracking"] = {"originalEstimate": task["estimate"]}

    r = requests.post(f"{JIRA_BASE}/rest/api/3/issue", json={"fields": fields},
                      auth=AUTH, headers=HEADERS)
    if r.status_code >= 400:
        # Retry without timetracking in case the field isn't on the create screen.
        if "timetracking" in fields:
            fields.pop("timetracking")
            r = requests.post(f"{JIRA_BASE}/rest/api/3/issue", json={"fields": fields},
                              auth=AUTH, headers=HEADERS)
    if r.status_code >= 400:
        die(f"Failed to create '{task['summary']}': {r.status_code} {r.text}")
    return r.json()["key"]


def link_blocks(blocker_key: str, blocked_key: str, link_type_name: str) -> None:
    """blocker_key BLOCKS blocked_key."""
    body = {
        "type": {"name": link_type_name},
        "inwardIssue": {"key": blocked_key},   # "is blocked by"
        "outwardIssue": {"key": blocker_key},  # "blocks"
    }
    r = requests.post(f"{JIRA_BASE}/rest/api/3/issueLink", json=body,
                      auth=AUTH, headers=HEADERS)
    if r.status_code >= 400:
        print(f"  WARN: couldn't link {blocker_key} blocks {blocked_key}: "
              f"{r.status_code} {r.text}", file=sys.stderr)


def delete_existing() -> None:
    """Delete every issue carrying the SEED_LABEL (so you can re-seed cleanly)."""
    jql = f'project = "{JIRA_PROJECT}" AND labels = "{SEED_LABEL}"'
    keys, next_token = [], None
    while True:
        params = {"jql": jql, "fields": "key", "maxResults": 100}
        if next_token:
            params["nextPageToken"] = next_token
        r = requests.get(f"{JIRA_BASE}/rest/api/3/search/jql", params=params,
                         auth=AUTH, headers=HEADERS)
        r.raise_for_status()
        data = r.json()
        keys.extend(i["key"] for i in data.get("issues", []))
        next_token = data.get("nextPageToken")
        if not next_token:
            break
    for k in keys:
        d = requests.delete(f"{JIRA_BASE}/rest/api/3/issue/{k}", auth=AUTH, headers=HEADERS)
        print(f"  deleted {k}: {d.status_code}")
    print(f"Removed {len(keys)} previously-seeded issue(s).")


def main() -> None:
    check_config()

    if "--delete-existing" in sys.argv:
        delete_existing()

    link_type = find_blocks_link_type()
    print(f"Using link type: {link_type!r}")
    print(f"Creating {len(TASKS)} issues in {JIRA_PROJECT}...")

    local_to_key: dict[str, str] = {}
    for task in TASKS:
        key = create_issue(task)
        local_to_key[task["id"]] = key
        print(f"  created {key}  {task['summary']}")

    print("Wiring up 'Blocks' dependencies...")
    for task in TASKS:
        blocked_key = local_to_key[task["id"]]
        for dep_id in task.get("blocked_by", []):
            blocker_key = local_to_key[dep_id]
            link_blocks(blocker_key, blocked_key, link_type)
            print(f"  {blocker_key} blocks {blocked_key}")

    sample = next(iter(local_to_key.values()))
    print("\nDone. Point the app at your board, e.g. a filter URL like:")
    print(f"  {JIRA_BASE}/issues/?jql=project%3D{JIRA_PROJECT}%20AND%20labels%3D{SEED_LABEL}")
    print(f"(or any page that lists/links these issues, e.g. {JIRA_BASE}/browse/{sample})")


if __name__ == "__main__":
    main()
