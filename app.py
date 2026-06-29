"""
app.py

A small Flask app: paste a URL to a page that lists/links JIRA issues, and it
will:
  1. Fetch the page and discover the JIRA instance + issue keys mentioned on it
  2. Pull each issue's status/labels/links/estimate from the JIRA REST API
  3. Build a dependency graph from "blocks" / "is blocked by" links
  4. Run the Critical Path Method and show which tasks are actually critical

Run with:  python app.py    (then open http://127.0.0.1:5000)
"""
from flask import Flask, render_template, request

from critical_path.jira_client import (
    fetch_page,
    discover_jira_base_and_keys,
    fetch_all_issues,
)
from critical_path.graph import build_graph, compute_critical_path

import requests

app = Flask(__name__)


@app.route("/", methods=["GET", "POST"])
def index():
    context = {
        "url": "",
        "email": "",
        "error": None,
        "warnings": [],
        "result": None,
        "issues": None,
        "jira_base": None,
    }

    if request.method == "POST":
        url = request.form.get("url", "").strip()
        email = request.form.get("email", "").strip()
        api_token = request.form.get("api_token", "").strip()
        context["url"] = url
        context["email"] = email

        auth = (email, api_token) if email and api_token else None

        if not url:
            context["error"] = "Please provide a URL."
            return render_template("index.html", **context)

        try:
            html = fetch_page(url)
        except ValueError as e:
            context["error"] = str(e)
            return render_template("index.html", **context)
        except requests.RequestException as e:
            context["error"] = f"Couldn't fetch that page: {e}"
            return render_template("index.html", **context)

        jira_base, keys = discover_jira_base_and_keys(url, html)
        context["jira_base"] = jira_base

        if not keys:
            context["error"] = (
                "No JIRA issue keys (e.g. PROJ-123) were found on that page. "
                "Try a JIRA filter/board URL, or a page that links directly to issues."
            )
            return render_template("index.html", **context)

        issues, errors = fetch_all_issues(jira_base, keys, auth=auth)
        context["warnings"].extend(errors)

        if not issues:
            context["error"] = (
                f"Found {len(keys)} possible issue key(s) but couldn't fetch any of them "
                f"from {jira_base}. If this is a private JIRA instance, fill in the email "
                f"+ API token fields."
            )
            return render_template("index.html", **context)

        graph = build_graph(issues)
        result = compute_critical_path(graph)

        if result.cycles_removed:
            context["warnings"].append(
                "Some issue links formed a cycle (e.g. A blocks B and B blocks A) and were "
                "ignored to compute the critical path: "
                + ", ".join(f"{a} -> {b}" for a, b in result.cycles_removed)
            )

        _CLOSED = {"done", "closed", "resolved", "cancelled", "won't do", "wont do", "rejected"}

        def _row_sort_key(r):
            is_open = r["status"].lower() not in _CLOSED
            # Tier 0: critical + open  (most urgent)
            # Tier 1: critical + closed
            # Tier 2: non-critical + open
            # Tier 3: non-critical + closed
            tier = 0 if (r["is_critical"] and is_open) else \
                   1 if r["is_critical"] else \
                   2 if is_open else 3
            return (tier, r["ES"] if r["ES"] is not None else 0, r["key"])

        # Build a display-friendly table of every fetched issue.
        rows = []
        for key, issue in issues.items():
            node = result.nodes.get(key)
            rows.append({
                "key": key,
                "url": issue.url,
                "summary": issue.summary,
                "status": issue.status,
                "priority": issue.priority,
                "labels": issue.labels,
                "duration": issue.duration_days,
                "duration_is_estimated": issue.duration_is_estimated,
                "ES": node["ES"] if node else None,
                "EF": node["EF"] if node else None,
                "LS": node["LS"] if node else None,
                "LF": node["LF"] if node else None,
                "float": node["float"] if node else None,
                "is_critical": node["is_critical"] if node else False,
            })
        rows.sort(key=_row_sort_key)

        context["issues"] = rows
        context["result"] = {
            "project_duration": result.project_duration,
            "critical_path": result.critical_path,
            "num_issues": len(issues),
            "num_critical": sum(1 for r in rows if r["is_critical"]),
        }

    return render_template("index.html", **context)


if __name__ == "__main__":
    app.run(debug=True)
