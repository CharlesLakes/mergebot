"""Turns CODEOWNERS + approvals into a yes/no answer about ownership coverage.

The policy, in one paragraph:

  Every file the pull request touches must be covered by an approval. A file is
  covered when somebody who owns it (per the CODEOWNERS rule that matches it,
  resolved through `teams.yaml`) approved the pull request. When a file has no
  owner at all, or its owners are one single person, that requirement opens up:
  an approval from ANY person other than the author covers it. The author never
  covers anything, not even their own team's files.

Why the escape hatch exists: several teams in this repository have exactly one
member (`@teams/backend`, `@teams/frontend`, `@teams/data`,
`@teams/lead`). Demanding "an owner approved" there would mean the
owner can never merge their own work -- and would block everyone else the day
that person is away. Same story for a path that no rule claims: nobody is
responsible, so any reviewer is as good as any other.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from codeowners import TEAM_PREFIX, CodeOwners, Rule
from identity import IdentityResolver
from teams import TeamRegistry

#: Why a requirement accepts an approval from anybody but the author.
OPEN_NO_RULE = "no_codeowners_rule"
OPEN_UNKNOWN_TEAM = "team_not_declared"
OPEN_EMPTY_TEAM = "team_has_no_members"
OPEN_SINGLE_OWNER = "single_owner"
OPEN_AUTHOR_IS_OWNER = "author_is_the_only_owner"

#: Human wording for each of the above, used in the pull request comments.
OPEN_REASON_TEXT = {
    OPEN_NO_RULE: "no CODEOWNERS rule claims these files, so they have no owner",
    OPEN_UNKNOWN_TEAM: "the team is referenced in CODEOWNERS but not declared in teams.yaml",
    OPEN_EMPTY_TEAM: "the team is declared in teams.yaml but has no members",
    OPEN_SINGLE_OWNER: "the team has a single member, who cannot be its own reviewer",
    OPEN_AUTHOR_IS_OWNER: "the only eligible owner is the author of the pull request",
}

KIND_TEAM = "team"
KIND_GROUP = "group"
KIND_USER = "user"


@dataclass
class Approval:
    """An approval that counts towards a requirement."""

    user: Dict
    #: The member spec from `teams.yaml` this approval was matched to, or None
    #: when it counted only because the requirement was open.
    member: Optional[str] = None
    #: How the identity was matched (see `identity.py`), for transparency.
    method: Optional[str] = None

    @property
    def display_name(self) -> str:
        return self.user.get("display_name") or self.user.get("nickname") or "unknown user"

    @property
    def is_owner(self) -> bool:
        return self.member is not None


@dataclass
class OwnerUnit:
    """One owner token of a CODEOWNERS line, resolved to actual people."""

    token: str
    kind: str
    members: Tuple[str, ...] = ()
    eligible: Tuple[str, ...] = ()
    open_reason: Optional[str] = None
    approvals: List[Approval] = field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return self.open_reason is not None

    @property
    def satisfied(self) -> bool:
        return bool(self.approvals)

    @property
    def label(self) -> str:
        return self.token


@dataclass
class Requirement:
    """The files matched by one CODEOWNERS rule, and who has to approve them."""

    key: str
    paths: Tuple[str, ...]
    units: Tuple[OwnerUnit, ...]
    mode: str = "any"
    rule: Optional[Rule] = None

    @property
    def pattern(self) -> str:
        return self.rule.pattern if self.rule else "(unmatched)"

    @property
    def satisfied(self) -> bool:
        if not self.units:
            return False
        if self.mode == "all":
            return all(unit.satisfied for unit in self.units)
        return any(unit.satisfied for unit in self.units)

    @property
    def satisfying_units(self) -> List[OwnerUnit]:
        return [unit for unit in self.units if unit.satisfied]

    @property
    def approvals(self) -> List[Approval]:
        seen: Dict[str, Approval] = {}
        for unit in self.units:
            for approval in unit.approvals:
                seen.setdefault(_user_key(approval.user), approval)
        return list(seen.values())

    @property
    def is_open(self) -> bool:
        """True when every owner token of the rule needed the escape hatch."""
        return bool(self.units) and all(unit.is_open for unit in self.units)


@dataclass
class OwnershipReport:
    requirements: List[Requirement]
    author: Dict
    approvers: List[Dict]
    #: Approvers that could not be matched to any owner: useful to spot an
    #: identity mapping that is missing from the configuration.
    unmatched_approvers: List[Dict] = field(default_factory=list)

    @property
    def satisfied(self) -> bool:
        return all(requirement.satisfied for requirement in self.requirements)

    @property
    def pending(self) -> List[Requirement]:
        return [requirement for requirement in self.requirements if not requirement.satisfied]

    @property
    def met(self) -> List[Requirement]:
        return [requirement for requirement in self.requirements if requirement.satisfied]

    @property
    def uncovered_paths(self) -> List[str]:
        paths: List[str] = []
        for requirement in self.pending:
            paths.extend(requirement.paths)
        return paths


class OwnershipEvaluator:
    """Builds the requirements of a pull request and decides which are met."""

    def __init__(self, codeowners: CodeOwners, teams: TeamRegistry,
                 resolver: IdentityResolver, rules_config,
                 workspace_groups: Optional[Dict[str, Sequence[str]]] = None) -> None:
        self.codeowners = codeowners
        self.teams = teams
        self.resolver = resolver
        self.config = rules_config
        self.workspace_groups = {key: tuple(value) for key, value
                                 in (workspace_groups or {}).items()}

    # -- building ---------------------------------------------------------

    def evaluate(self, paths: Sequence[str], author: Dict,
                 approvers: Sequence[Dict]) -> OwnershipReport:
        grouped = self._group_paths(paths)
        requirements: List[Requirement] = []
        matched_users: Dict[str, Dict] = {}

        for rule, rule_paths in grouped:
            units = tuple(self._build_unit(token, author, approvers)
                          for token in (rule.owners if rule else ()))
            if not units:
                # Nothing owns these files: one open unit stands for "anyone".
                units = (self._open_unit("(no owner)", OPEN_NO_RULE, author, approvers),)
            requirements.append(Requirement(
                key=_requirement_key(rule, units),
                paths=tuple(rule_paths),
                units=units,
                mode=self.config.multi_owner_mode,
                rule=rule,
            ))
            for unit in units:
                for approval in unit.approvals:
                    if approval.is_owner:
                        matched_users[_user_key(approval.user)] = approval.user

        unmatched = [user for user in approvers
                     if _user_key(user) not in matched_users
                     and not same_user(user, author)]
        return OwnershipReport(requirements=requirements, author=author,
                               approvers=list(approvers), unmatched_approvers=unmatched)

    def _group_paths(self, paths: Sequence[str]) -> List[Tuple[Optional[Rule], List[str]]]:
        """Group the changed paths by the CODEOWNERS rule that owns them."""
        groups: Dict[str, Tuple[Optional[Rule], List[str]]] = {}
        for path in paths:
            rule = self.codeowners.match(path)
            # Zero-padded so the groups sort by CODEOWNERS line number and not
            # as strings, where "10" would come before "9".
            key = "{:05d}#{}".format(rule.line, rule.pattern) if rule else "\x00unowned"
            groups.setdefault(key, (rule, []))[1].append(path)
        # Sorted by rule line so the comments read in CODEOWNERS order.
        return [groups[key] for key in sorted(groups)]

    # -- one owner token --------------------------------------------------

    def _build_unit(self, token: str, author: Dict, approvers: Sequence[Dict]) -> OwnerUnit:
        kind, members, known = self._resolve_token(token)
        eligible = tuple(member for member in members
                         if not self._is_author(member, author))

        open_reason = None
        if not known and self.config.unowned_fallback:
            open_reason = OPEN_UNKNOWN_TEAM
        elif not members and self.config.unowned_fallback:
            open_reason = OPEN_EMPTY_TEAM
        elif len(members) == 1 and self.config.single_owner_fallback:
            open_reason = OPEN_SINGLE_OWNER
        elif not eligible:
            # No fallback flag can help here: every owner is the author, so
            # requiring an owner would make the pull request unmergeable.
            open_reason = OPEN_AUTHOR_IS_OWNER

        unit = OwnerUnit(token=token, kind=kind, members=tuple(members),
                         eligible=eligible, open_reason=open_reason)
        unit.approvals = self._collect_approvals(unit, author, approvers)
        return unit

    def _open_unit(self, token: str, reason: str, author: Dict,
                   approvers: Sequence[Dict]) -> OwnerUnit:
        unit = OwnerUnit(token=token, kind=KIND_TEAM, open_reason=reason)
        unit.approvals = self._collect_approvals(unit, author, approvers)
        return unit

    def _resolve_token(self, token: str) -> Tuple[str, List[str], bool]:
        """`(kind, members, known)` for a CODEOWNERS owner token.

        `known` is False when the token points at something the bot cannot
        expand -- an undeclared team, or a workspace group with no mapping in
        the configuration.
        """
        if token.startswith(TEAM_PREFIX):
            team = self.teams.get(token[len(TEAM_PREFIX):])
            if team is None:
                return KIND_TEAM, [], False
            return KIND_TEAM, list(team.contributors), True
        if token.startswith("@") and "/" in token:
            members = self.workspace_groups.get(token.lstrip("@"))
            if members is None:
                members = self.workspace_groups.get(token)
            if members is None:
                return KIND_GROUP, [], False
            return KIND_GROUP, list(members), True
        return KIND_USER, [token], True

    def _collect_approvals(self, unit: OwnerUnit, author: Dict,
                           approvers: Sequence[Dict]) -> List[Approval]:
        """Approvals that count for this unit, owners first."""
        found: List[Approval] = []
        counted: set = set()

        for member in unit.eligible:
            for user, method in self.resolver.find(member, approvers):
                if self._excluded(user, author):
                    continue
                key = _user_key(user)
                if key in counted:
                    continue
                counted.add(key)
                found.append(Approval(user=user, member=member, method=method))

        if not found and unit.is_open:
            # The escape hatch: anybody other than the author will do.
            for user in approvers:
                if self._excluded(user, author):
                    continue
                found.append(Approval(user=user))
                break
        return found

    def _excluded(self, user: Dict, author: Dict) -> bool:
        if self.config.author_can_approve:
            return False
        return same_user(user, author)

    def _is_author(self, member: str, author: Dict) -> bool:
        return bool(self.resolver.match(member, author))


# -- helpers --------------------------------------------------------------

def _user_key(user: Dict) -> str:
    return str(user.get("uuid") or user.get("account_id")
               or user.get("nickname") or user.get("display_name") or id(user))


def same_user(left: Dict, right: Dict) -> bool:
    if not left or not right:
        return False
    for field_name in ("uuid", "account_id", "nickname"):
        left_value, right_value = left.get(field_name), right.get(field_name)
        if left_value and right_value:
            return str(left_value).strip("{}").lower() == str(right_value).strip("{}").lower()
    return (left.get("display_name") or "\x00") == right.get("display_name")


def _requirement_key(rule: Optional[Rule], units: Tuple[OwnerUnit, ...]) -> str:
    """Stable id for a requirement, used to post its comment exactly once."""
    seed = "{}|{}".format(rule.pattern if rule else "(unmatched)",
                          ",".join(sorted(unit.token for unit in units)))
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:10]


def approvers_of(participants: Iterable[Dict]) -> List[Dict]:
    """The users that approved, out of the raw `participants` payload."""
    return [participant.get("user") or {} for participant in participants
            if participant.get("approved") or participant.get("state") == "approved"]


def changes_requested_by(participants: Iterable[Dict]) -> List[Dict]:
    return [participant.get("user") or {} for participant in participants
            if participant.get("state") == "changes_requested"]
