"""
graph.py

Builds a dependency graph from JIRA "blocks" / "is blocked by" links and runs
the actual Critical Path Method (CPM) over it:

  1. Forward pass  -> Earliest Start (ES) / Earliest Finish (EF) per task
  2. Backward pass -> Latest Start (LS) / Latest Finish (LF) per task
  3. Float = LS - ES. Tasks with float == 0 are on the critical path.

This mirrors the textbook CPM algorithm rather than just "longest chain of
issues" -- it accounts for task duration, multiple parallel paths, and
correctly identifies *all* zero-float tasks (there can be more than one
critical path, or a critical path that merges/splits).
"""

from __future__ import annotations

import dataclasses
import networkx as nx

from .jira_client import Issue


@dataclasses.dataclass
class CPMResult:
    project_duration: float
    nodes: dict  # key -> dict(ES, EF, LS, LF, float, is_critical)
    critical_path: list[str]  # ordered list of keys forming (one) longest critical chain
    cycles_removed: list[tuple[str, str]]  # edges dropped to break cycles, if any


def build_graph(
    issues: dict[str, Issue],
    duration_overrides: dict[str, float] | None = None,
) -> nx.DiGraph:
    """
    Build a directed graph where an edge A -> B means "A blocks B", i.e. A must
    finish before B can start. Only edges between issues we actually fetched
    are kept (an issue blocking something outside our set is ignored, since we
    have no duration/status data for it).

    duration_overrides: optional per-key duration map; used to run CPM with
    alternative scenario durations (optimistic / pessimistic / PERT) without
    mutating the Issue objects.
    """
    overrides = duration_overrides or {}
    g = nx.DiGraph()
    for key, issue in issues.items():
        g.add_node(key, duration=overrides.get(key, issue.duration_days))

    for key, issue in issues.items():
        for blocked_key in issue.blocks:
            if blocked_key in issues:
                g.add_edge(key, blocked_key)
        for blocker_key in issue.blocked_by:
            if blocker_key in issues:
                g.add_edge(blocker_key, key)

    return g


def _break_cycles(g: nx.DiGraph) -> list[tuple[str, str]]:
    """
    JIRA issue links are entered by humans and occasionally form a cycle
    (A blocks B, B blocks A by mistake). CPM requires a DAG, so we detect and
    drop the offending edges, reporting them so the user can see what was
    ignored rather than silently getting a wrong answer.
    """
    removed = []
    while not nx.is_directed_acyclic_graph(g):
        cycle = nx.find_cycle(g)
        edge_to_remove = cycle[0][:2]
        g.remove_edge(*edge_to_remove)
        removed.append(edge_to_remove)
    return removed

def compute_critical_path(g: nx.DiGraph) -> CPMResult:
    g = g.copy()
    cycles_removed = _break_cycles(g)

    topo = list(nx.topological_sort(g))
    durations = nx.get_node_attributes(g, "duration")

    es, ef = {}, {}
    # forward pass
    for n in topo:
        preds = list(g.predecessors(n))
        es[n] = max((ef[p] for p in preds), default=0.0)
        ef[n] = es[n] + durations.get(n, 1.0)

    project_duration = max(ef.values(), default=0.0)

    # backward pass
    lf, ls = {}, {}
    for n in reversed(topo):
        succs = list(g.successors(n))
        lf[n] = min((ls[s] for s in succs), default=project_duration)
        ls[n] = lf[n] - durations.get(n, 1.0)

    nodes = {}
    for n in topo:
        flt = round(ls[n] - es[n], 4)
        nodes[n] = {
            "ES": round(es[n], 2),
            "EF": round(ef[n], 2),
            "LS": round(ls[n], 2),
            "LF": round(lf[n], 2),
            "float": flt,
            "is_critical": flt == 0,
        }

    # Build one representative ordered critical path (there may be several
    # tied zero-float chains -- we walk the longest one for display purposes,
    # while `nodes[*]["is_critical"]` still flags every zero-float task).
    critical_nodes = {n for n, v in nodes.items() if v["is_critical"]}
    critical_path = _order_critical_chain(g, critical_nodes, es)

    return CPMResult(
        project_duration=round(project_duration, 2),
        nodes=nodes,
        critical_path=critical_path,
        cycles_removed=cycles_removed,
    )


def _order_critical_chain(g: nx.DiGraph, critical_nodes: set[str], es: dict[str, float]) -> list[str]:
    """Walk the critical-node subgraph in ES order along actual edges to produce
    one readable ordered chain (start to finish) rather than an unordered set."""
    if not critical_nodes:
        return []
    sub = g.subgraph(critical_nodes)
    # Start from a critical node with no critical predecessor.
    starts = [n for n in sub.nodes if sub.in_degree(n) == 0]
    if not starts:
        starts = list(sub.nodes)
    start = min(starts, key=lambda n: es[n])

    chain = [start]
    current = start
    visited = {start}
    while True:
        succs = [s for s in sub.successors(current) if s not in visited]
        if not succs:
            break
        nxt = min(succs, key=lambda n: es[n])
        chain.append(nxt)
        visited.add(nxt)
        current = nxt
    return chain
