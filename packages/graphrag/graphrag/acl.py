"""Roles, groups and sensitivity tiers (GraphRAG Slice 1).

A requester is a role, and a role is a fixed set of groups. A document's ACL is
the list of groups allowed to read it. Access is decided by one function,
`can_read`: a requester may read a chunk iff their groups and the chunk's ACL
intersect. Retrieval calls it before any scoring (see retrieval.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

ALL_STAFF = "all-staff"
GROUPS: tuple[str, ...] = ("engineering", "sales", "hr", "finance", "exec")
TIERS: tuple[str, ...] = ("public", "internal", "restricted")

# Each role reads public material through all-staff, plus its own department's group.
ROLES: dict[str, frozenset[str]] = {
    "Engineering": frozenset({ALL_STAFF, "engineering"}),
    "Sales": frozenset({ALL_STAFF, "sales"}),
    "HR": frozenset({ALL_STAFF, "hr"}),
    "Finance": frozenset({ALL_STAFF, "finance"}),
    "Exec": frozenset({ALL_STAFF, "exec"}),
}


@dataclass(frozen=True)
class Requester:
    role: str
    groups: frozenset[str]


def requester(role: str) -> Requester:
    """Resolve a role name (case-insensitive) to its requester; raise ValueError if unknown."""
    for name, groups in ROLES.items():
        if name.casefold() == role.casefold():
            return Requester(name, groups)
    raise ValueError(f"unknown role {role!r}; known roles: {', '.join(ROLES)}")


def can_read(groups: Iterable[str], acl: Iterable[str]) -> bool:
    return not frozenset(groups).isdisjoint(acl)
