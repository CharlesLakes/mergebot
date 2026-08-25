"""Configuration loading for Automerge.

Everything the bot decides is driven from here, so that a policy change is a
YAML edit and not a code change. Credentials are the exception: they are read
from environment variables only, so secrets live in the CI's variable store
and never in the repository.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import yaml

from identity import (MATCH_EMAIL_LOCAL, MATCH_INITIAL_SURNAME, MATCH_NICKNAME)

DEFAULT_CONFIG_NAME = "automerge.yaml"

#: Namespace of the invisible marker that makes the bot idempotent (README §6).
#: Invisible to the team, so it carries the project's name with no cost: what
#: people actually see next to a comment is the name of the ACCOUNT that wrote
#: it, which Bitbucket takes from the credentials and the bot cannot choose.
DEFAULT_MARKER_NAMESPACE = "automerge"

#: A marker namespace ends up inside an HTML comment and inside a regex, so it
#: is kept to characters that cannot close the one or confuse the other.
_MARKER_NAMESPACE_RE = re.compile(r"^[A-Za-z0-9._-]+$")


#: Environment variables holding the credentials. They are the ONLY thing the
#: environment provides: secrets stay out of the repository, and everything
#: else -- which repository is merged, the policy, the merge strategy -- lives
#: in `automerge.yaml`, where it is versioned and reviewed like any other code.
ENV_TOKEN = "AUTOMERGE_BITBUCKET_TOKEN"
ENV_USERNAME = "AUTOMERGE_BITBUCKET_USERNAME"
ENV_APP_PASSWORD = "AUTOMERGE_BITBUCKET_APP_PASSWORD"

#: The three strategies Bitbucket Cloud offers on the merge endpoint.
MERGE_STRATEGIES = ("merge_commit", "squash", "fast_forward")


class ConfigError(Exception):
    """Raised when the configuration file cannot be used as it is."""


@dataclass
class BotConfig:
    """The bot's own bookkeeping. NOT its name.

    There is no name to configure: every comment and every merge is attributed
    by Bitbucket to whoever owns the credentials -- the access token's name, or
    the display name of the dedicated account -- so the bot cannot choose how it
    is announced, and repeating a name inside the comment body would only
    disagree with the one shown above it. What the bot writes says `automerge`,
    which is what it does, and stays true whatever the account is called.
    """

    #: Namespace of the invisible marker every comment carries
    #: (`<!-- automerge:v1 key=... -->`, README §6). That marker is the bot's
    #: entire memory, so CHANGING IT ON A LIVE REPOSITORY MAKES IT FORGET: every
    #: requirement comment is posted a second time and the old checklist is
    #: orphaned instead of edited. Change it only to run two bots side by side
    #: on the same pull requests.
    marker: str = DEFAULT_MARKER_NAMESPACE


@dataclass
class BitbucketConfig:
    workspace: str = ""
    repository: str = ""
    base_url: str = "https://api.bitbucket.org/2.0"
    timeout: int = 30
    max_retries: int = 3


@dataclass
class TargetConfig:
    """One branch the bot is allowed to merge into, and under what conditions.

    A typical setup has two automatable hops (`* -> develop` and
    `develop -> stg`) and one that is not: `stg -> main` is
    the production promotion and stays a human decision, which is why `main`
    lives in `Config.manual_only_branches` instead of here.
    """

    branch: str = "develop"
    #: Source branches allowed to merge into it. Empty means any of them.
    from_branches: Tuple[str, ...] = ()
    #: Whether every changed file has to be covered by its owners' approval.
    #: Off for a promotion between long-lived branches: whatever it carries was
    #: already reviewed by its owners on the way into `develop`, and the diff of
    #: a promotion spans every team at once.
    require_codeowners: bool = True
    #: Overrides of the global policy for this branch. None = use the global one.
    min_approvals: Optional[int] = None
    required_pipelines: Optional[Tuple[str, ...]] = None


@dataclass
class OwnersConfig:
    """Where CODEOWNERS and teams.yaml are read from.

    The bot does not need to live in the repository it merges: by default it
    reads both files through the API, from the destination branch of each pull
    request, exactly where Bitbucket reads them. `repository` pins them to a
    fixed repository and ref instead, which is what a bot hosted in its own
    repository uses when the rules are centralised somewhere else.
    """

    codeowners_path: str = ".bitbucket/CODEOWNERS"
    teams_path: str = ".bitbucket/teams.yaml"
    #: `destination` -> the branch the pull request targets, in the merged repo.
    #: `repository`  -> the fixed `repository`/`ref` below.
    #: `local`       -> the working copy, for testing without the API.
    read_from: str = "destination"
    #: `workspace/repo-slug`. Empty means the repository being merged.
    repository: str = ""
    #: Branch or commit. Empty means the pull request's destination branch.
    ref: str = ""
    local_root: str = "."


@dataclass
class RulesConfig:
    """The approval policy."""

    #: `any` -> one approval from any of the owners listed on the matching
    #: CODEOWNERS line is enough (standard CODEOWNERS semantics).
    #: `all` -> every team listed on the line needs one approval of its own.
    multi_owner_mode: str = "any"
    #: A team of one cannot review its own owner's work, so any approval from
    #: somebody who is not the author covers what that team owns.
    single_owner_fallback: bool = True
    #: Same escape hatch for paths that no CODEOWNERS rule claims, or whose
    #: team is not declared (or is empty) in `teams.yaml`.
    unowned_fallback: bool = True
    #: At least one approver other than the author.
    min_approvals: int = 1
    #: Bitbucket lets an author approve their own pull request. Never count it.
    author_can_approve: bool = False


@dataclass
class ChecksConfig:
    require_pipeline: bool = True
    #: Names of pipeline steps/selectors that must be present and green. Empty
    #: means "whatever ran on that commit has to be green".
    required_pipelines: Tuple[str, ...] = ()
    block_on_open_tasks: bool = True
    block_on_changes_requested: bool = True
    block_on_draft: bool = True
    #: Pull requests whose title contains one of these markers are left alone.
    #: `[no-automerge]` says what it does instead of naming the bot, so it keeps
    #: working whatever the account posting the comments is called.
    skip_title_markers: Tuple[str, ...] = ("[wip]", "wip:", "[no-automerge]")


@dataclass
class MergeConfig:
    enabled: bool = True
    #: `merge_commit` keeps the branch history, which is how this repository
    #: merges (`Merged in <branch> (pull request #NNN)`).
    strategy: str = "merge_commit"
    close_source_branch: bool = True


@dataclass
class CommentsConfig:
    #: One comment the first time each requirement is met -- the running
    #: commentary the team reads on the pull request.
    per_requirement: bool = True
    #: A single checklist comment, edited in place on every run.
    status_summary: bool = True
    #: A comment when the bot merges.
    on_merge: bool = True
    #: A comment when a merge attempt fails (conflicts, branch restrictions).
    on_failure: bool = True


@dataclass
class IdentityConfig:
    heuristics: Tuple[str, ...] = (MATCH_NICKNAME, MATCH_EMAIL_LOCAL, MATCH_INITIAL_SURNAME)
    map: Tuple[Dict[str, Any], ...] = ()


@dataclass
class Config:
    bot: BotConfig = field(default_factory=BotConfig)
    bitbucket: BitbucketConfig = field(default_factory=BitbucketConfig)
    #: Branches the bot may merge into, in the order they are evaluated.
    targets: Tuple[TargetConfig, ...] = (TargetConfig(branch="develop"),)
    #: Branches the bot must never merge into, whatever the rest of the file
    #: says. `main` is production and is promoted by hand.
    manual_only_branches: Tuple[str, ...] = ("main",)
    owners: OwnersConfig = field(default_factory=OwnersConfig)
    rules: RulesConfig = field(default_factory=RulesConfig)
    checks: ChecksConfig = field(default_factory=ChecksConfig)
    merge: MergeConfig = field(default_factory=MergeConfig)
    comments: CommentsConfig = field(default_factory=CommentsConfig)
    identity: IdentityConfig = field(default_factory=IdentityConfig)
    #: Workspace groups (`@workspace/group`) cannot be expanded through the
    #: API, so their members are declared here when CODEOWNERS uses them.
    workspace_groups: Dict[str, List[str]] = field(default_factory=dict)
    #: Never merge, never comment: evaluate and print. Also set by `--dry-run`.
    dry_run: bool = False

    @classmethod
    def load(cls, path: Optional[str] = None) -> "Config":
        document: Dict[str, Any] = {}
        if path:
            if not os.path.exists(path):
                raise ConfigError(
                    "configuration file not found: {}. Create it with "
                    "`cp automerge.example.yaml automerge.yaml`, or pass --config.".format(path))
            with open(path, "r", encoding="utf-8") as handle:
                document = yaml.safe_load(handle) or {}
        return cls.from_dict(document)

    @classmethod
    def from_dict(cls, document: Dict[str, Any]) -> "Config":
        document = document or {}
        config = cls(
            bot=_section(BotConfig, document.get("bot")),
            bitbucket=_section(BitbucketConfig, document.get("bitbucket")),
            targets=_targets(document.get("targets")),
            manual_only_branches=tuple(document.get("manual_only_branches") or ("main",)),
            owners=_section(OwnersConfig, document.get("owners")),
            rules=_section(RulesConfig, document.get("rules")),
            checks=_section(ChecksConfig, document.get("checks")),
            merge=_section(MergeConfig, document.get("merge")),
            comments=_section(CommentsConfig, document.get("comments")),
            identity=_section(IdentityConfig, document.get("identity")),
            workspace_groups={str(key): list(value or [])
                              for key, value in (document.get("workspace_groups") or {}).items()},
            dry_run=bool(document.get("dry_run", False)),
        )
        if config.owners.repository and config.owners.read_from == "destination":
            # Naming another repository only makes sense with that source, so
            # take it as the intent rather than ignoring it.
            config.owners.read_from = "repository"
        config.validate()
        return config

    def validate(self) -> None:
        if not _MARKER_NAMESPACE_RE.match(self.bot.marker or ""):
            raise ConfigError(
                "bot.marker must be made of letters, digits, '.', '_' or '-' (got '{}'). It is "
                "written inside an HTML comment, so anything else would break it.".format(
                    self.bot.marker))
        if not self.targets:
            raise ConfigError("at least one entry is needed under 'targets'")
        manual = {branch.lower() for branch in self.manual_only_branches}
        for target in self.targets:
            if target.branch.lower() in manual:
                raise ConfigError(
                    "'{}' is listed in manual_only_branches: the bot will not merge into it. "
                    "Remove it from 'targets', or from 'manual_only_branches' if the policy "
                    "really changed.".format(target.branch))
        if len({target.branch for target in self.targets}) != len(self.targets):
            raise ConfigError("a branch appears more than once under 'targets'")
        if self.rules.multi_owner_mode not in ("any", "all"):
            raise ConfigError("rules.multi_owner_mode must be 'any' or 'all'")
        if self.owners.read_from not in ("destination", "repository", "local"):
            raise ConfigError("owners.read_from must be 'destination', 'repository' or 'local'")
        if self.owners.read_from == "repository" and not self.owners.repository:
            raise ConfigError("owners.read_from is 'repository' but owners.repository is empty "
                              "(expected 'workspace/repo-slug')")
        if self.owners.repository and self.owners.repository.count("/") != 1:
            raise ConfigError("owners.repository must be 'workspace/repo-slug', got '{}'".format(
                self.owners.repository))
        if self.merge.strategy not in MERGE_STRATEGIES:
            raise ConfigError("merge.strategy must be one of {} (got '{}')".format(
                ", ".join(MERGE_STRATEGIES), self.merge.strategy))
        if not self.bitbucket.workspace or not self.bitbucket.repository:
            raise ConfigError("bitbucket.workspace and bitbucket.repository are required in "
                              "automerge.yaml")

    def credentials(self) -> Tuple[Optional[str], Optional[Tuple[str, str]]]:
        """`(token, (username, app_password))` -- exactly one of them is set."""
        token = os.environ.get(ENV_TOKEN)
        username = os.environ.get(ENV_USERNAME)
        app_password = os.environ.get(ENV_APP_PASSWORD)
        if token:
            return token, None
        if username and app_password:
            return None, (username, app_password)
        raise ConfigError(
            "no credentials: set {} or both {} and {}".format(
                ENV_TOKEN, ENV_USERNAME, ENV_APP_PASSWORD))


def _targets(values: Optional[Sequence[Any]]) -> Tuple[TargetConfig, ...]:
    """Read the `targets` list, accepting a bare branch name as a shorthand."""
    if not values:
        return (TargetConfig(branch="develop"),)

    targets = []
    for entry in values:
        if isinstance(entry, str):
            targets.append(TargetConfig(branch=entry))
            continue
        entry = dict(entry or {})
        # `from` reads better in YAML than `from_branches`, and is a keyword.
        sources = entry.pop("from", None) or entry.pop("from_branches", None) or ()
        required = entry.pop("required_pipelines", None)
        unknown = set(entry) - {"branch", "require_codeowners", "min_approvals"}
        if unknown:
            raise ConfigError("unknown option(s) in targets: {}".format(
                ", ".join(sorted(unknown))))
        if not entry.get("branch"):
            raise ConfigError("every entry under 'targets' needs a 'branch'")
        targets.append(TargetConfig(
            branch=str(entry["branch"]),
            from_branches=tuple(sources),
            require_codeowners=bool(entry.get("require_codeowners", True)),
            min_approvals=entry.get("min_approvals"),
            required_pipelines=tuple(required) if required is not None else None,
        ))
    return tuple(targets)


def _section(kind, values: Optional[Dict[str, Any]]):
    """Build a config dataclass, refusing keys that do not exist in it.

    A typo in the YAML is a policy that silently does not apply, which is the
    worst possible failure mode for a bot that merges code.
    """
    values = values or {}
    known = {f.name for f in kind.__dataclass_fields__.values()}
    unknown = set(values) - known
    if unknown:
        raise ConfigError("unknown option(s) in {}: {}".format(
            kind.__name__, ", ".join(sorted(unknown))))
    coerced = {}
    for key, value in values.items():
        current = kind.__dataclass_fields__[key].default
        coerced[key] = tuple(value) if isinstance(current, tuple) and isinstance(value, list) else value
    return kind(**coerced)
