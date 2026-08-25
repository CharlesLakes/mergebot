"""Mapping the people declared in `teams.yaml` to Bitbucket accounts.

This is the one genuinely fiddly part of the bot. CODEOWNERS and `teams.yaml`
identify people by **email**, while the Bitbucket API never returns the email
of a pull request participant: an approval only carries `uuid`, `account_id`,
`nickname` and `display_name`.

So a member spec has to be matched against a Bitbucket user somehow. The bot
tries, in order:

  1. an explicit entry in the configuration (`identity.map`) -- always exact,
     always preferred, and the only thing that should be trusted for a member
     whose account cannot be derived from their email;
  2. `nickname` (or `username`) equal to the local part of the email, or equal
     to the `@handle` written in `teams.yaml`;
  3. the "initial + surname" convention used in this workspace, where
     `jperez@example.com` is `Juana Pérez` -- a heuristic, enabled by default
     because it is what the repository actually uses, and switchable in the
     configuration for workspaces where it does not hold.

Anything that cannot be matched is reported as unresolved and NEVER counted as
an approval: the bot fails closed, so an unmapped owner blocks the merge
instead of silently letting it through.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

#: How a member spec was matched to a Bitbucket account, for the PR comment.
MATCH_CONFIGURED = "configured"
MATCH_NICKNAME = "nickname"
MATCH_EMAIL_LOCAL = "email-local-part"
MATCH_INITIAL_SURNAME = "initial+surname"


def _strip_accents(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def _normalise(value: str) -> str:
    """Lowercase, unaccented, punctuation-free form used for comparisons."""
    cleaned = _strip_accents(value or "").lower()
    return "".join(char for char in cleaned if char.isalnum())


def email_local_part(spec: str) -> Optional[str]:
    if "@" not in spec or spec.startswith("@"):
        return None
    return spec.split("@", 1)[0].strip().lower()


def handle_of(spec: str) -> Optional[str]:
    """The `@handle` form of a member spec, without the `@`."""
    if spec.startswith("@") and "/" not in spec:
        return spec[1:].strip().lower()
    return None


@dataclass(frozen=True)
class IdentityEntry:
    """One line of `identity.map`: a person, and how they look on Bitbucket."""

    spec: str
    uuid: Optional[str] = None
    account_id: Optional[str] = None
    nickname: Optional[str] = None
    display_name: Optional[str] = None

    def matches_user(self, user: Dict) -> bool:
        if self.uuid and _same(self.uuid, user.get("uuid")):
            return True
        if self.account_id and _same(self.account_id, user.get("account_id")):
            return True
        if self.nickname and _normalise(self.nickname) == _normalise(
                user.get("nickname") or user.get("username") or ""):
            return True
        if self.display_name and _normalise(self.display_name) == _normalise(
                user.get("display_name") or ""):
            return True
        return False


def _same(left: Optional[str], right: Optional[str]) -> bool:
    if not left or not right:
        return False
    return left.strip().strip("{}").lower() == str(right).strip().strip("{}").lower()


@dataclass
class IdentityResolver:
    """Decides whether a Bitbucket user is the person a member spec names."""

    entries: Tuple[IdentityEntry, ...] = ()
    heuristics: Tuple[str, ...] = (MATCH_NICKNAME, MATCH_EMAIL_LOCAL, MATCH_INITIAL_SURNAME)

    @classmethod
    def from_config(cls, mapping: Sequence[Dict], heuristics: Sequence[str]) -> "IdentityResolver":
        entries = []
        for item in mapping or []:
            spec = item.get("email") or item.get("spec") or item.get("user")
            if not spec:
                continue
            entries.append(IdentityEntry(
                spec=str(spec).strip().lower(),
                uuid=item.get("uuid"),
                account_id=item.get("account_id"),
                nickname=item.get("nickname"),
                display_name=item.get("display_name"),
            ))
        return cls(entries=tuple(entries), heuristics=tuple(heuristics or ()))

    def _configured(self, spec: str) -> Optional[IdentityEntry]:
        wanted = spec.strip().lower()
        for entry in self.entries:
            if entry.spec == wanted:
                return entry
        return None

    def match(self, spec: str, user: Dict) -> Optional[str]:
        """Return how `spec` matches `user`, or None when it does not."""
        configured = self._configured(spec)
        if configured is not None:
            # An explicit mapping is the whole truth for that person: if it
            # does not match this user, no heuristic gets a second chance.
            return MATCH_CONFIGURED if configured.matches_user(user) else None

        nickname = _normalise(user.get("nickname") or user.get("username") or "")
        display = user.get("display_name") or ""

        handle = handle_of(spec)
        if handle and MATCH_NICKNAME in self.heuristics:
            if nickname and nickname == _normalise(handle):
                return MATCH_NICKNAME
            if _normalise(display) == _normalise(handle):
                return MATCH_NICKNAME

        local = email_local_part(spec)
        if local:
            if MATCH_EMAIL_LOCAL in self.heuristics and nickname and nickname == _normalise(local):
                return MATCH_EMAIL_LOCAL
            if MATCH_INITIAL_SURNAME in self.heuristics and _initial_surname_matches(local, display):
                return MATCH_INITIAL_SURNAME
        return None

    def is_resolvable(self, spec: str, users: Iterable[Dict]) -> bool:
        return any(self.match(spec, user) for user in users)

    def find(self, spec: str, users: Iterable[Dict]) -> List[Tuple[Dict, str]]:
        """Every user that `spec` matches, with the method that matched them."""
        found = []
        for user in users:
            method = self.match(spec, user)
            if method:
                found.append((user, method))
        return found


def _initial_surname_matches(local: str, display_name: str) -> bool:
    """`jperez` == `Juana Pérez`, the naming convention of this workspace.

    Deliberately strict: the first letter of the given name plus the full
    surname, with no accents. `mrondon` matches `Martín Rondón`; it does not
    match `Marcela Rondón Silva` unless the surname is the last word, which is
    exactly the ambiguity worth refusing.
    """
    words = [_normalise(word) for word in (display_name or "").split() if _normalise(word)]
    if len(words) < 2 or len(local) < 2:
        return False
    candidate = _normalise(local)
    first, surname, last = words[0], words[1], words[-1]
    return candidate in (first[:1] + surname, first[:1] + last)
