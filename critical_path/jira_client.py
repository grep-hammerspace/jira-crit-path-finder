"""
jira_client.py

Two jobs:
  1. Given an arbitrary URL (a JIRA filter/board page, or any webpage that links
     to or mentions JIRA issues), figure out which JIRA instance it belongs to
     and which issue keys it references.
  2. Given a JIRA base URL + a set of issue keys, pull each issue's details
     (status, labels, priority, time estimate, and link relationships) via the
     public JIRA REST API.

Only "blocks" / "is blocked by" link types are used to build dependencies,
since those are the ones that map cleanly onto Critical Path Method semantics
(A blocks B  =>  A must finish before B can start).
"""

from __future__ import annotations

import ipaddress
import re
import socket
import dataclasses
from typing import Optional
from urllib.parse import urlparse, urljoin

import requests
from bs4 import BeautifulSoup

ISSUE_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d+)\b")

_BLOCKED_NETWORKS: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),  # link-local / cloud metadata endpoints
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),   # Tailscale / CGNAT range
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),         # IPv6 ULA
    ipaddress.ip_network("fe80::/10"),        # IPv6 link-local
]


def _assert_public_url(url: str) -> None:
    """
    Reject URLs that resolve to private/reserved IP space to prevent SSRF.
    Checks every address returned by DNS so multi-homed hosts can't sneak through.
    Raises ValueError with a user-visible message on failure.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Only http/https URLs are supported, got: {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise ValueError("URL has no hostname.")
    try:
        results = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"Could not resolve {host!r}: {exc}") from exc
    for _family, _type, _proto, _canon, sockaddr in results:
        addr = ipaddress.ip_address(sockaddr[0])
        if any(addr in net for net in _BLOCKED_NETWORKS):
            raise ValueError(
                f"Blocked: {host!r} resolves to {addr}, which is a private or "
                f"reserved address. Only publicly routable URLs are allowed."
            )

# Default working day length used to convert JIRA's "original estimate"
# (given in seconds) into a duration measured in days.
SECONDS_PER_DAY = 8 * 60 * 60

DEFAULT_DURATION_DAYS = 1.0


@dataclasses.dataclass
class Issue:
    key: str
    summary: str = ""
    status: str = "Unknown"
    priority: str = "Unknown"
    labels: list[str] = dataclasses.field(default_factory=list)
    duration_days: float = DEFAULT_DURATION_DAYS
    blocks: list[str] = dataclasses.field(default_factory=list)        # this issue blocks these keys
    blocked_by: list[str] = dataclasses.field(default_factory=list)    # this issue is blocked by these keys
    url: str = ""
    duration_is_estimated: bool = False  # True if a real estimate was found, False if we used the default


class JiraClientError(Exception):
    pass


def discover_jira_base_and_keys(source_url: str, html: str) -> tuple[str, set[str]]:
    """
    Inspect a page's HTML (and the URL it came from) to figure out:
      - the base URL of the JIRA instance (scheme + host + '/jira' style prefix if present)
      - the set of issue keys referenced on the page

    Strategy:
      1. Look for <a href="...//browse/KEY-123..."> links first -- most reliable,
         since the link itself tells us the JIRA host.
      2. Fall back to scanning all visible text for KEY-123 style patterns if no
         /browse/ links are found (e.g. a plain wiki page that just *mentions*
         issue keys without linking them).
      3. If the source_url itself looks like a JIRA URL (contains /browse/ or
         /issues/ or a jql= param), use its host as a fallback base.
    """
    soup = BeautifulSoup(html, "html.parser")
    parsed_source = urlparse(source_url)

    jira_base: Optional[str] = None
    keys: set[str] = set()

    # Pass 1: anchors that link directly to /browse/KEY-123
    for a in soup.find_all("a", href=True):
        href = a["href"]
        full = urljoin(source_url, href)
        m = re.search(r"^(https?://[^/]+(?:/[^/]+)*?)/browse/([A-Z][A-Z0-9]{1,9}-\d+)", full)
        if m:
            candidate_base, key = m.group(1), m.group(2)
            jira_base = jira_base or candidate_base
            keys.add(key)

    # Pass 2: if we found a base but want more keys, or found nothing at all,
    # scan the whole page text for issue-key-shaped tokens.
    text = soup.get_text(" ")
    for m in ISSUE_KEY_RE.finditer(text):
        keys.add(m.group(1))

    # Also scan raw href strings (covers cases where the key is in a query
    # string rather than the link text, e.g. JQL filter links).
    for a in soup.find_all("a", href=True):
        for m in ISSUE_KEY_RE.finditer(a["href"]):
            keys.add(m.group(1))

    # Pass 3: fall back to the source URL's own host if we never found a base.
    if jira_base is None:
        if "/browse/" in parsed_source.path or "/issues" in parsed_source.path or "jql" in (parsed_source.query or ""):
            jira_base = f"{parsed_source.scheme}://{parsed_source.netloc}"
            # Strip a trailing /issues or /browse/... back to the JIRA root.
            jira_base = re.sub(r"/(browse|issues|projects)(/.*)?$", "", jira_base + parsed_source.path)
            jira_base = f"{parsed_source.scheme}://{parsed_source.netloc}" + (
                re.sub(r"/(browse|issues|projects).*$", "", parsed_source.path)
            )

    if jira_base is None:
        # Last resort: assume the page itself is hosted on the JIRA instance.
        jira_base = f"{parsed_source.scheme}://{parsed_source.netloc}"

    return jira_base.rstrip("/"), keys


def fetch_page(url: str, timeout: int = 20) -> str:
    _assert_public_url(url)
    resp = requests.get(url, timeout=timeout, headers={"User-Agent": "critical-path-finder/1.0"})
    resp.raise_for_status()
    return resp.text


def fetch_issue(
    jira_base: str,
    key: str,
    auth: Optional[tuple[str, str]] = None,
    timeout: int = 15,
) -> Issue:
    """Fetch one issue's details from the JIRA REST API (v2)."""
    api_url = f"{jira_base}/rest/api/2/issue/{key}"
    _assert_public_url(api_url)
    params = {"fields": "summary,status,priority,labels,issuelinks,timetracking,timeoriginalestimate"}
    resp = requests.get(api_url, params=params, auth=auth, timeout=timeout,
                        headers={"User-Agent": "critical-path-finder/1.0", "Accept": "application/json"})
    if resp.status_code == 401:
        raise JiraClientError(
            f"{key}: got 401 Unauthorized. This JIRA instance needs credentials -- "
            f"provide an email + API token (or username + password) in the form."
        )
    if resp.status_code == 404:
        raise JiraClientError(f"{key}: not found (404) at {jira_base}.")
    resp.raise_for_status()
    data = resp.json()
    fields = data.get("fields", {})

    issue = Issue(key=key, url=f"{jira_base}/browse/{key}")
    issue.summary = fields.get("summary") or ""
    status = fields.get("status") or {}
    issue.status = status.get("name", "Unknown")
    priority = fields.get("priority") or {}
    issue.priority = priority.get("name", "Unknown") if priority else "Unknown"
    issue.labels = fields.get("labels") or []

    # Duration: prefer an explicit original time estimate, else default to 1 day.
    est_seconds = fields.get("timeoriginalestimate")
    if est_seconds:
        issue.duration_days = max(round(est_seconds / SECONDS_PER_DAY, 2), 0.25)
        issue.duration_is_estimated = True

    for link in fields.get("issuelinks", []) or []:
        link_type = link.get("type", {})
        outward = link.get("outwardIssue")
        inward = link.get("inwardIssue")
        if outward and link_type.get("outward", "").lower() == "blocks":
            issue.blocks.append(outward["key"])
        if inward and link_type.get("inward", "").lower() == "is blocked by":
            issue.blocked_by.append(inward["key"])

    return issue


def fetch_all_issues(
    jira_base: str,
    keys: set[str],
    auth: Optional[tuple[str, str]] = None,
) -> tuple[dict[str, Issue], list[str]]:
    """
    Fetch every issue in `keys`. Returns (issues_by_key, errors).
    Issues that fail to fetch are skipped (and reported in `errors`) rather than
    aborting the whole run, since one bad/private key shouldn't block everything else.
    """
    issues: dict[str, Issue] = {}
    errors: list[str] = []
    for key in sorted(keys):
        try:
            issues[key] = fetch_issue(jira_base, key, auth=auth)
        except (JiraClientError, requests.RequestException) as e:
            errors.append(str(e))
    return issues, errors
