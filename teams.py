"""Reader for `.bitbucket/teams.yaml`.

A "team" in this repository is just a versioned list of people: it is not a
workspace group, it grants no permission and nobody has to create it. Teams are
referenced from CODEOWNERS as `@teams/<name>` and their members are declared
either by email (`someone@example.com`) or by Bitbucket username (`"@someone"`).

The `reviews` block of the file (strategy / select) only drives which reviewers
Bitbucket *suggests* when the pull request is opened. It has no say in whether
an approval counts, so it is parsed and kept for reporting but never used as a
gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import yaml


@dataclass(frozen=True)
class Team:
    """A team as declared in `teams.yaml`."""

    name: str
    contributors: Tuple[str, ...] = ()
    strategy: str = "all"
    select: Optional[int] = None

    @property
    def is_empty(self) -> bool:
        return not self.contributors

    @property
    def is_single_owner(self) -> bool:
        """True when one single person owns everything this team owns.

        This is the case that needs the escape hatch: a team of one cannot
        approve a pull request written by that very person, and even when the
        author is somebody else there is no second owner to fall back on.
        """
        return len(self.contributors) == 1


class TeamRegistry:
    """All the teams of `teams.yaml`, indexed by name."""

    def __init__(self, teams: Dict[str, Team]) -> None:
        self._teams = teams

    @classmethod
    def parse(cls, text: str) -> "TeamRegistry":
        document = yaml.safe_load(text) or {}
        if not isinstance(document, dict):
            raise ValueError("teams.yaml must be a mapping of team name -> definition")

        teams: Dict[str, Team] = {}
        for name, body in document.items():
            body = body or {}
            reviews = body.get("reviews") or {}
            contributors = [str(person).strip()
                            for person in (body.get("contributors") or [])
                            if str(person).strip()]
            teams[str(name)] = Team(
                name=str(name),
                contributors=tuple(contributors),
                strategy=str(reviews.get("strategy", "all")),
                select=reviews.get("select"),
            )
        return cls(teams)

    def get(self, name: str) -> Optional[Team]:
        return self._teams.get(name)

    def names(self) -> List[str]:
        return sorted(self._teams)

    def __contains__(self, name: str) -> bool:
        return name in self._teams

    def __len__(self) -> int:
        return len(self._teams)
