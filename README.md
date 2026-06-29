# Critical Path Finder

A small Flask app: give it a link to a page that lists or links JIRA issues
(a JIRA filter URL, a board, or even a wiki page that just mentions issue
keys), and it will:

1. Fetch the page and find the JIRA instance + issue keys referenced on it.
2. Pull each issue's status, labels, time estimate, and `blocks` / `is blocked by`
   links via the JIRA REST API.
3. Build a dependency graph from those links.
4. Run the actual **Critical Path Method** (forward pass for earliest
   start/finish, backward pass for latest start/finish, float = LS − ES) —
   not just "the longest chain of issues", but the real CPM calculation,
   including float for every non-critical task.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate          # on Windows: venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Then open http://127.0.0.1:5000

## Usage

1. Paste a URL. Good inputs:
   - A JIRA filter/search URL, e.g.
     `https://issues.apache.org/jira/issues/?jql=project=CAMEL`
   - A JIRA board URL
   - Any webpage that links directly to `/browse/KEY-123` issues, or just
     mentions issue keys in its text (e.g. a changelog or wiki page)
2. If the JIRA instance is private, expand "Private JIRA instance? Add
   credentials" and provide an email + API token (for JIRA Cloud, generate
   one at id.atlassian.com/manage-profile/security/api-tokens) or a
   username/password for an on-prem instance.
3. Click "Find critical path". You'll get:
   - The minimum possible project duration (sum of durations along the
     critical chain)
   - The ordered critical chain itself
   - A full table of every issue found, with ES/EF/LS/LF and float, sorted
     by earliest start

## How duration is determined

JIRA's "Original Estimate" field (`timeoriginalestimate`, in seconds) is
used if it's set, converted to days assuming an 8-hour day. If an issue has
no estimate, it defaults to **1 day** — these are marked "(assumed)" in the
table. This means the absolute duration numbers are only as good as your
team's estimating discipline; the *relative* structure (which tasks are
critical vs. have float) is still meaningful even with all-default durations,
since it's driven by the dependency graph shape, not just numbers.

## What counts as a dependency

Only the JIRA link types **"blocks"** and **"is blocked by"** are used to
build the graph. Other link types (relates to, duplicates, clones, etc.) are
intentionally ignored, since they don't imply a hard ordering constraint.

## Limitations / things to know

- **Cycles**: if issue links form a cycle (A blocks B, B blocks A — usually
  a tagging mistake), one edge is dropped to make the graph a valid DAG, and
  you'll see a warning listing exactly which edge was ignored.
- **Scope**: only issues that are actually referenced on the page you give
  it are pulled in. If a critical blocker lives outside that issue set
  (e.g. it's not linked or listed anywhere on the page), it won't be seen.
- **Auth**: public instances (like the Apache Software Foundation's JIRA at
  `issues.apache.org/jira`) need no credentials. Private/internal instances
  do.
- **Issue key detection**: keys are matched with the pattern `PROJECT-123`.
  Very short or unusual project key formats might produce false positives
  on a page with lots of incidental text; these will simply fail to fetch
  and show up as a warning rather than breaking the whole run.

## Project layout

```
app.py                       Flask routes / glue code
critical_path/
  jira_client.py             Page scraping + JIRA REST API calls
  graph.py                   Graph building + CPM algorithm
templates/index.html         UI
static/style.css             Styling
test_cpm.py                  Standalone sanity check of the CPM math
```

Run `python test_cpm.py` any time to confirm the CPM engine is behaving
correctly against a known worked example — useful if you extend the
duration logic (e.g. to pull from a custom "Story Points" field instead).
