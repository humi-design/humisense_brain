"""Dependency intelligence: edges, chains, and cycle detection.

Two rules keep dependencies trustworthy:

* **Explicit dependencies use deterministic rules.** They are derived from structured
  relationships the source states, not from text similarity.
* **Inferred dependencies are labelled and reviewable.** An inferred edge carries
  ``origin=inferred`` and a confidence, and never by itself reassigns work or changes an
  operational status.

Cycle detection is deterministic and reports a conflict rather than "fixing" the cycle.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass

from humisense.db.models import DependencyRelationship

# Relationship pairs that are logical inverses. Adding one should not also add the other for
# the same pair of work items; that would double-count and can create a false cycle.
INVERSE_RELATIONSHIP: dict[DependencyRelationship, DependencyRelationship] = {
    DependencyRelationship.BLOCKS: DependencyRelationship.BLOCKED_BY,
    DependencyRelationship.BLOCKED_BY: DependencyRelationship.BLOCKS,
    DependencyRelationship.PRECEDES: DependencyRelationship.WAITING_FOR,
    DependencyRelationship.WAITING_FOR: DependencyRelationship.PRECEDES,
}

# Relationships that describe a hard ordering (an edge in the dependency graph). The looser
# relationships (RELATED_TO) are recorded but excluded from cycle detection, because a
# mutual "related to" is not a cycle.
ORDERING_RELATIONSHIPS: frozenset[DependencyRelationship] = frozenset(
    {
        DependencyRelationship.BLOCKS,
        DependencyRelationship.BLOCKED_BY,
        DependencyRelationship.REQUIRES,
        DependencyRelationship.WAITING_FOR,
        DependencyRelationship.PRECEDES,
        DependencyRelationship.APPROVAL_REQUIRED,
        DependencyRelationship.MATERIAL_DEPENDENCY,
        DependencyRelationship.INFORMATION_REQUIRED,
    }
)


# Depth-first-search colours for cycle detection. A node is unseen (white), on the current
# path (grey), or fully explored (black).
_WHITE, _GREY, _BLACK = 0, 1, 2


@dataclass(frozen=True, slots=True)
class DependencyEdge:
    """One ordering edge, in the direction "source depends on target"."""

    source_work_id: uuid.UUID
    target_work_id: uuid.UUID
    relationship: DependencyRelationship


def find_cycles(edges: list[DependencyEdge]) -> list[list[uuid.UUID]]:
    """Return every cycle in the ordering graph.

    Uses an iterative depth-first search so a deep chain cannot exhaust the Python stack.
    Each returned cycle is a list of work ids in order; the first id is repeated implicitly
    as the closing edge. Cycles are reported, never silently broken.
    """
    adjacency: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    for edge in edges:
        if edge.relationship in ORDERING_RELATIONSHIPS:
            adjacency[edge.source_work_id].append(edge.target_work_id)

    color: dict[uuid.UUID, int] = {}
    cycles: list[list[uuid.UUID]] = []
    seen_cycles: set[frozenset[uuid.UUID]] = set()

    for start in list(adjacency):
        if color.get(start, _WHITE) != _WHITE:
            continue
        stack: list[tuple[uuid.UUID, int]] = [(start, 0)]
        path: list[uuid.UUID] = []
        on_path: dict[uuid.UUID, int] = {}
        while stack:
            node, index = stack.pop()
            if index == 0:
                color[node] = _GREY
                on_path[node] = len(path)
                path.append(node)
            neighbours = adjacency.get(node, [])
            if index < len(neighbours):
                stack.append((node, index + 1))
                neighbour = neighbours[index]
                state = color.get(neighbour, _WHITE)
                if state == _GREY:
                    cycle = path[on_path[neighbour] :]
                    key = frozenset(cycle)
                    if key not in seen_cycles:
                        seen_cycles.add(key)
                        cycles.append(cycle)
                elif state == _WHITE:
                    stack.append((neighbour, 0))
            else:
                color[node] = _BLACK
                on_path.pop(node, None)
                path.pop()
    return cycles


def dependency_chain_length(edges: list[DependencyEdge], *, max_depth: int = 1000) -> int:
    """Return the longest ordering chain length, bounded to avoid unbounded traversal."""
    adjacency: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    for edge in edges:
        if edge.relationship in ORDERING_RELATIONSHIPS:
            adjacency[edge.source_work_id].append(edge.target_work_id)

    longest = 0
    for start in list(adjacency):
        stack: list[tuple[uuid.UUID, int]] = [(start, 1)]
        visited: set[uuid.UUID] = set()
        while stack and len(visited) < max_depth:
            node, depth = stack.pop()
            if node in visited:
                continue
            visited.add(node)
            longest = max(longest, depth)
            for neighbour in adjacency.get(node, []):
                if neighbour not in visited:
                    stack.append((neighbour, depth + 1))
    return longest


__all__ = [
    "INVERSE_RELATIONSHIP",
    "ORDERING_RELATIONSHIPS",
    "DependencyEdge",
    "dependency_chain_length",
    "find_cycles",
]
