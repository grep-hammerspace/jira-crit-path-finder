"""Sanity-check the CPM engine against a known example before wiring up the web app.

Mirrors the textbook example used earlier in conversation:
  A -> B -> D   (2 + 5 + 3 = 10 days)   <- should be critical
  A -> C -> D   (2 + 2 + 3 = 7 days)    <- C should have 3 days of float
"""
import sys
sys.path.insert(0, ".")

from critical_path.jira_client import Issue
from critical_path.graph import build_graph, compute_critical_path

issues = {
    "A": Issue(key="A", summary="Kickoff", duration_days=2, blocks=["B", "C"]),
    "B": Issue(key="B", summary="Main build", duration_days=5, blocked_by=["A"], blocks=["D"]),
    "C": Issue(key="C", summary="Side task", duration_days=2, blocked_by=["A"], blocks=["D"]),
    "D": Issue(key="D", summary="Final integration", duration_days=3, blocked_by=["B", "C"]),
}

g = build_graph(issues)
result = compute_critical_path(g)

print("Project duration:", result.project_duration, "(expected 10.0)")
print("Critical path:", result.critical_path, "(expected ['A', 'B', 'D'])")
print()
for k, v in result.nodes.items():
    print(k, v)

assert result.project_duration == 10.0
assert result.critical_path == ["A", "B", "D"]
assert result.nodes["C"]["float"] == 3.0
assert result.nodes["A"]["is_critical"] is True
assert result.nodes["C"]["is_critical"] is False
print("\nAll assertions passed.")
