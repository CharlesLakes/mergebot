"""CODEOWNERS parsing and path matching (Bitbucket Cloud flavour).

Bitbucket reads the CODEOWNERS file from the DESTINATION branch of a pull
request, and uses `.gitignore`-style patterns with a few particularities that
this module reproduces on purpose:

  * the LAST rule that matches a path wins -- not the first one, as it would in
    `.gitignore`. That is why the repository file lists the generic rules first
    and the specific ones last;
  * negation (`!`), character ranges (`[a-z]`) and escaped `#` are not
    supported, so they are not implemented here either;
  * a rule may list several owners (`@teams/testing @teams/backend`); how
    many of them have to approve is a policy decision and lives in
    `approvals.py`, not here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterator, List, Optional, Tuple

#: Token that references a team declared in `.bitbucket/teams.yaml`.
TEAM_PREFIX = "@teams/"


@dataclass(frozen=True)
class Rule:
    """One non-empty, non-comment line of the CODEOWNERS file."""

    pattern: str
    owners: Tuple[str, ...]
    line: int
    regex: "re.Pattern[str]"

    def matches(self, path: str) -> bool:
        return bool(self.regex.match(path))

    def __str__(self) -> str:
        return "{} {}".format(self.pattern, " ".join(self.owners))


def _pattern_to_regex(pattern: str) -> "re.Pattern[str]":
    """Translate a `.gitignore`-style CODEOWNERS pattern into a regex.

    The rules that matter for matching a *file path*:

      * a trailing `/` makes the pattern a directory, and a directory owns
        every file below it;
      * a slash at the beginning or in the middle anchors the pattern to the
        repository root, otherwise it may match at any depth;
      * `*` and `?` never cross a `/`, `**` does.
    """
    directory_only = pattern.endswith("/")
    core = pattern[:-1] if directory_only else pattern
    # A trailing slash does not anchor; a leading or middle one does.
    anchored = "/" in core
    core = core.lstrip("/")

    parts: List[str] = []
    index = 0
    while index < len(core):
        char = core[index]
        if char == "*":
            if core[index:index + 3] == "**/":
                parts.append("(?:.*/)?")
                index += 3
                continue
            if core[index:index + 2] == "**":
                parts.append(".*")
                index += 2
                continue
            parts.append("[^/]*")
        elif char == "?":
            parts.append("[^/]")
        else:
            parts.append(re.escape(char))
        index += 1

    prefix = "" if anchored else "(?:.*/)?"
    # A directory pattern only owns what is inside it; a plain pattern owns the
    # path itself and, when it happens to name a directory, its contents too.
    suffix = "/.*" if directory_only else "(?:/.*)?"
    return re.compile("^" + prefix + "".join(parts) + suffix + "$")


def _split_line(raw: str) -> Optional[Tuple[str, Tuple[str, ...]]]:
    """Return `(pattern, owners)` for a CODEOWNERS line, or None if it is noise."""
    line = raw.split("#", 1)[0].strip()
    if not line:
        return None
    fields = line.split()
    pattern, owners = fields[0], tuple(fields[1:])
    return pattern, owners


class CodeOwners:
    """The parsed CODEOWNERS file, ready to answer "who owns this path?"."""

    def __init__(self, rules: List[Rule], source: str = "CODEOWNERS") -> None:
        self.rules = rules
        self.source = source

    @classmethod
    def parse(cls, text: str, source: str = "CODEOWNERS") -> "CodeOwners":
        rules: List[Rule] = []
        for number, raw in enumerate(text.splitlines(), start=1):
            parsed = _split_line(raw)
            if parsed is None:
                continue
            pattern, owners = parsed
            rules.append(Rule(pattern=pattern, owners=owners, line=number,
                              regex=_pattern_to_regex(pattern)))
        return cls(rules, source=source)

    def match(self, path: str) -> Optional[Rule]:
        """The rule that owns `path`, or None when no rule matches it.

        Last match wins, so the file is walked backwards and the first hit is
        the answer.
        """
        # Only a leading `./` is dropped. `lstrip("./")` would eat the dot of
        # a dotfile as well, and `.bitbucket/CODEOWNERS` -- the file that
        # decides who has to approve -- would stop matching `/.bitbucket/`.
        normalised = path[2:] if path.startswith("./") else path
        normalised = normalised.lstrip("/")
        for rule in reversed(self.rules):
            if rule.matches(normalised):
                return rule
        return None

    def owners_for(self, path: str) -> Tuple[str, ...]:
        rule = self.match(path)
        return rule.owners if rule else ()

    def __iter__(self) -> Iterator[Rule]:
        return iter(self.rules)

    def __len__(self) -> int:
        return len(self.rules)
