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
import datetime
import math

from flask import Flask, render_template, request

from critical_path.jira_client import (
    fetch_page,
    discover_jira_base_and_keys,
    fetch_all_issues,
    extract_jql,
    search_issue_keys,
    JiraClientError,
)
from critical_path.graph import build_graph, compute_critical_path

import requests


def _scenario_durations(issues, mode, opt_mult=0.75, pes_mult=1.5):
    """Return a {key: duration_days} map adjusted for the given scenario.

    opt_mult / pes_mult are applied to every issue's duration_days regardless
    of whether the duration came from a Jira estimate or the configured default,
    since by the time this is called the caller has already baked the user's
    default into unestimated issues.
    """
    result = {}
    for key, issue in issues.items():
        d = issue.duration_days
        if mode == "optimistic":
            result[key] = max(d * opt_mult, 0.25)
        elif mode == "pessimistic":
            result[key] = d * pes_mult
        elif mode == "most_likely":
            result[key] = d
        elif mode == "pert":
            o = max(d * opt_mult, 0.25)
            p = d * pes_mult
            result[key] = (o + 4 * d + p) / 6
    return result


def _add_working_days(start, days):
    """Return the calendar date that is ceiling(days) working days after start."""
    remaining = math.ceil(days)
    current = start
    while remaining > 0:
        current += datetime.timedelta(days=1)
        if current.weekday() < 5:  # Mon–Fri
            remaining -= 1
    return current

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
        "timeline": None,
        "default_duration": 10.0,
        "opt_mult": 0.75,
        "pes_mult": 1.5,
    }

    if request.method == "POST":
        url = request.form.get("url", "").strip()
        email = request.form.get("email", "").strip()
        api_token = request.form.get("api_token", "").strip()
        context["url"] = url
        context["email"] = email

        try:
            default_duration = float(request.form.get("default_duration") or 10.0)
            if default_duration <= 0:
                raise ValueError
        except ValueError:
            default_duration = 10.0
        try:
            opt_mult = float(request.form.get("opt_mult") or 0.75)
            if opt_mult <= 0:
                raise ValueError
        except ValueError:
            opt_mult = 0.75
        try:
            pes_mult = float(request.form.get("pes_mult") or 1.5)
            if pes_mult <= 0:
                raise ValueError
        except ValueError:
            pes_mult = 1.5

        context["default_duration"] = default_duration
        context["opt_mult"] = opt_mult
        context["pes_mult"] = pes_mult

        if opt_mult >= pes_mult:
            context["error"] = "Optimistic multiplier must be smaller than the pessimistic multiplier."
            return render_template("index.html", **context)

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

        # Private/Cloud filter URLs can't be scraped (login-gated, JS-rendered),
        # so if the page yielded nothing but the URL carries a JQL query, resolve
        # the keys through the authenticated search API instead.
        if not keys:
            jql = extract_jql(url)
            if jql:
                try:
                    keys = search_issue_keys(jira_base, jql, auth=auth)
                except JiraClientError as e:
                    context["error"] = str(e)
                    return render_template("index.html", **context)
                except requests.RequestException as e:
                    context["error"] = f"Couldn't query the JIRA search API: {e}"
                    return render_template("index.html", **context)

        if not keys:
            context["error"] = (
                "No JIRA issue keys (e.g. PROJ-123) were found on that page. "
                "Try a JIRA filter/board URL, or a page that links directly to issues."
            )
            return render_template("index.html", **context)

        issues, errors = fetch_all_issues(jira_base, keys, auth=auth)
        context["warnings"].extend(errors)

        for issue in issues.values():
            if not issue.duration_is_estimated:
                issue.duration_days = default_duration

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

        # --- Project start date (earliest created timestamp across all issues) ---
        created_dates = [
            datetime.date.fromisoformat(issue.created[:10])
            for issue in issues.values()
            if issue.created
        ]
        project_start = min(created_dates) if created_dates else None

        # --- Three-scenario timeline (optimistic / most likely / pessimistic / PERT) ---
        if project_start is not None:
            scenario_results = {}
            for mode in ("optimistic", "most_likely", "pessimistic", "pert"):
                overrides = _scenario_durations(issues, mode, opt_mult=opt_mult, pes_mult=pes_mult)
                g_s = build_graph(issues, duration_overrides=overrides)
                scenario_results[mode] = compute_critical_path(g_s).project_duration

            def _fmt_date(d):
                return d.strftime("%-d %b %Y")

            context["timeline"] = {
                "start_date": _fmt_date(project_start),
                "optimistic":  {"duration": round(scenario_results["optimistic"], 1),  "end_date": _fmt_date(_add_working_days(project_start, scenario_results["optimistic"]))},
                "most_likely": {"duration": round(scenario_results["most_likely"], 1), "end_date": _fmt_date(_add_working_days(project_start, scenario_results["most_likely"]))},
                "pert":        {"duration": round(scenario_results["pert"], 1),        "end_date": _fmt_date(_add_working_days(project_start, scenario_results["pert"]))},
                "pessimistic": {"duration": round(scenario_results["pessimistic"], 1), "end_date": _fmt_date(_add_working_days(project_start, scenario_results["pessimistic"]))},
            }

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
