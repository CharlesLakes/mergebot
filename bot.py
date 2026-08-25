"""The bot itself: read a pull request, decide, comment, merge.

The order of the gates below is the order of the workflow rules the repository
being merged has agreed on:

  1. the pull request is open, not a draft, and its destination is a branch the
     bot is allowed to merge into, coming from an allowed source;
  2. nobody requested changes and no task is left open;
  3. the pipeline is green **on the commit that would be merged**;
  4. every changed file is covered by an approval (see `approvals.py`), when
     the target asks for owner coverage;
  5. at least one approval comes from somebody other than the author.

Two hops are automatable and are configured as targets: any branch into
`develop`, and `develop` into `stg` (preproduction). The third one,
`stg` into `main`, is the production promotion and is never automated -- the
configuration refuses to even list it.

When everything holds the bot merges, with the same merge message Bitbucket
itself writes: `Merged in <branch> (pull request #NNN)`.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

import comments as render
from approvals import (OwnershipEvaluator, OwnershipReport, approvers_of,
                       changes_requested_by, same_user)
from bitbucket import BitbucketClient, BitbucketError
from codeowners import CodeOwners
from comments import CommentJournal
from config import Config, TargetConfig
from identity import IdentityResolver
from pipelines import PipelineGate, PipelineStatus
from teams import TeamRegistry

LOG = logging.getLogger("automerge")

#: What the bot did with one pull request.
ACTION_MERGED = "merged"
ACTION_WAITING = "waiting"
ACTION_SKIPPED = "skipped"
ACTION_FAILED = "failed"


@dataclass
class GateFacts:
    """Everything the gates looked at, kept as data rather than as prose.

    The pull request comments are written in Spanish and the logs in English,
    so neither can be built from the other's sentences: both are rendered from
    this object instead.
    """

    target: TargetConfig
    pipeline: PipelineStatus
    report: OwnershipReport
    changes_requested: List[Dict] = field(default_factory=list)
    open_tasks: int = 0
    approvals_counted: int = 0
    min_approvals: int = 1
    #: Files the API listed for the pull request.
    changed_paths: int = 0
    #: True when the API listed fewer entries than it announced. An empty pull
    #: request is fine; a truncated answer is not, and only that one blocks.
    diffstat_incomplete: bool = False

    def blockers(self) -> List[str]:
        """Why the merge cannot happen, in English, for the logs and the CLI."""
        blockers: List[str] = []
        if self.changes_requested:
            blockers.append("changes requested by {}".format(
                ", ".join(user.get("display_name", "?") for user in self.changes_requested)))
        if self.open_tasks:
            blockers.append("{} unresolved task(s) on the pull request".format(self.open_tasks))
        if not self.pipeline.satisfied:
            blockers.append("pipeline: {}".format(self.pipeline.detail))
        if self.target.require_codeowners and self.diffstat_incomplete:
            # A pull request that changes nothing is a legitimate, mergeable
            # thing: no files, no owners to ask, and the other gates still
            # apply. What must never pass is a listing that came back short of
            # what it announced, because then "no requirements" means "the
            # answer was cut off", not "there is nothing to review".
            blockers.append("the diffstat came back incomplete: owner coverage cannot be "
                            "evaluated, so the merge is not attempted")
        if self.approvals_counted < self.min_approvals:
            blockers.append("needs at least {} approval(s) from someone other than the author "
                            "(has {})".format(self.min_approvals, self.approvals_counted))
        for requirement in self.report.pending:
            owners = ", ".join(unit.token for unit in requirement.units)
            blockers.append("{} file(s) matched by `{}` are not covered: {}".format(
                len(requirement.paths), requirement.pattern, owners))
        return blockers


@dataclass
class Decision:
    pr_id: int
    title: str
    action: str
    blockers: List[str] = field(default_factory=list)
    facts: Optional[GateFacts] = None
    comments_posted: List[str] = field(default_factory=list)

    @property
    def merged(self) -> bool:
        return self.action == ACTION_MERGED

    @property
    def report(self) -> Optional[OwnershipReport]:
        return self.facts.report if self.facts else None

    @property
    def pipeline(self) -> Optional[PipelineStatus]:
        return self.facts.pipeline if self.facts else None

    def summary(self) -> str:
        head = "PR #{} [{}] {}".format(self.pr_id, self.action, self.title)
        if self.blockers:
            head += "\n  " + "\n  ".join("- {}".format(item) for item in self.blockers)
        return head


class Automerge:
    def __init__(self, config: Config, client: BitbucketClient) -> None:
        self.config = config
        self.client = client
        self._owners_cache: Dict[str, OwnershipEvaluator] = {}

    # -- entry points -----------------------------------------------------

    def run(self, pr_ids: Optional[Sequence[int]] = None) -> List[Decision]:
        if pr_ids:
            pull_requests = [self.client.get_pull_request(pr_id) for pr_id in pr_ids]
        else:
            branches = [target.branch for target in self.config.targets]
            pull_requests = self.client.list_open_pull_requests(branches)
            LOG.info("%s open pull request(s) targeting %s",
                     len(pull_requests), ", ".join(branches))

        decisions = []
        for pull_request in pull_requests:
            try:
                decisions.append(self.process(pull_request))
            except BitbucketError as error:
                LOG.error("PR #%s could not be processed: %s", pull_request.get("id"), error)
                decisions.append(Decision(pr_id=pull_request.get("id", 0),
                                          title=pull_request.get("title", ""),
                                          action=ACTION_FAILED, blockers=[str(error)]))
        return decisions

    def process(self, pull_request: Dict) -> Decision:
        pr_id = pull_request["id"]
        title = pull_request.get("title", "")
        # The list endpoint returns a trimmed payload; the merge decision needs
        # the full one (participants above all).
        if "participants" not in pull_request:
            pull_request = self.client.get_pull_request(pr_id)

        target, skip = self._target_for(pull_request)
        if skip:
            LOG.info("PR #%s skipped: %s", pr_id, skip)
            return Decision(pr_id=pr_id, title=title, action=ACTION_SKIPPED, blockers=[skip])

        facts = self._gather(pull_request, target)
        blockers = facts.blockers()

        journal = CommentJournal(self.client, pr_id, dry_run=self.config.dry_run,
                                 namespace=self.config.bot.marker)
        self._announce(journal, facts)

        merge_allowed = self.config.merge.enabled and not self.config.dry_run
        if self.config.comments.status_summary:
            journal.upsert(render.KEY_STATUS,
                           render.render_status(pull_request, facts,
                                                will_merge=not blockers and merge_allowed))

        decision = Decision(pr_id=pr_id, title=title, action=ACTION_WAITING,
                            blockers=blockers, facts=facts,
                            comments_posted=list(journal.posted))
        if blockers:
            return decision
        if not merge_allowed:
            decision.blockers = ["everything is met; merge not performed ({})".format(
                "dry run" if self.config.dry_run else "merge disabled in the configuration")]
            return decision
        return self._merge(pull_request, journal, decision)

    # -- gates ------------------------------------------------------------

    def _target_for(self, pull_request: Dict):
        """`(target, reason)`: which target applies, or why the PR is skipped."""
        if pull_request.get("state") != "OPEN":
            return None, "pull request is {}".format(pull_request.get("state"))

        destination = ((pull_request.get("destination") or {}).get("branch") or {}).get("name")
        source = ((pull_request.get("source") or {}).get("branch") or {}).get("name")

        if destination in self.config.manual_only_branches:
            return None, "`{}` is merged by hand, never by the bot".format(destination)

        target = next((item for item in self.config.targets
                       if item.branch == destination), None)
        if target is None:
            return None, "no target configured for `{}`".format(destination)
        if target.from_branches and source not in target.from_branches:
            return None, "`{}` may only be merged into `{}` from {}".format(
                source, target.branch,
                ", ".join("`{}`".format(branch) for branch in target.from_branches))

        if self.config.checks.block_on_draft and pull_request.get("draft"):
            return None, "pull request is a draft"
        title = (pull_request.get("title") or "").lower()
        for token in self.config.checks.skip_title_markers:
            if token.lower() in title:
                return None, "title contains the marker `{}`".format(token)
        return target, None

    def _gather(self, pull_request: Dict, target: TargetConfig) -> GateFacts:
        pr_id = pull_request["id"]
        head = ((pull_request.get("source") or {}).get("commit") or {}).get("hash") or ""
        participants = self.client.list_participants(pull_request)
        author = pull_request.get("author") or {}
        approvers = approvers_of(participants)

        paths: List[str] = []
        incomplete = False
        if target.require_codeowners:
            evaluator = self._evaluator_for(target.branch)
            paths, reported = self.client.get_diffstat(pr_id)
            incomplete = bool(reported) and not paths
            report = evaluator.evaluate(paths, author, approvers)
        else:
            # A promotion between long-lived branches carries changes that were
            # already reviewed by their owners on the way into `develop`; its
            # diff spans every team at once, so owner coverage is not asked for
            # again. An empty report is a satisfied report.
            report = OwnershipReport(requirements=[], author=author, approvers=list(approvers))

        pipeline = PipelineGate(self.client, self.config.checks).evaluate(
            head, required_pipelines=target.required_pipelines, pull_request_id=pr_id)

        objectors = (changes_requested_by(participants)
                     if self.config.checks.block_on_changes_requested else [])
        open_tasks = 0
        if self.config.checks.block_on_open_tasks:
            open_tasks = len([task for task in self.client.list_tasks(pr_id)
                              if (task.get("state") or "").upper() != "RESOLVED"])

        counted = [user for user in approvers
                   if self.config.rules.author_can_approve or not same_user(user, author)]
        min_approvals = (target.min_approvals if target.min_approvals is not None
                         else self.config.rules.min_approvals)

        return GateFacts(target=target, pipeline=pipeline, report=report,
                         changes_requested=objectors, open_tasks=open_tasks,
                         approvals_counted=len(counted), min_approvals=min_approvals,
                         changed_paths=len(paths), diffstat_incomplete=incomplete)

    # -- side effects -----------------------------------------------------

    def _announce(self, journal: CommentJournal, facts: GateFacts) -> None:
        """Say out loud, once, which requirements are now met."""
        if self.config.comments.per_requirement:
            for requirement in facts.report.met:
                journal.post_once(render.requirement_key(requirement),
                                  render.render_requirement_met(requirement))
        if facts.pipeline.satisfied and facts.pipeline.runs:
            journal.post_once("{}:{}".format(render.KEY_PIPELINE, facts.pipeline.commit[:12]),
                              render.render_pipeline_green(facts.pipeline))

    def _merge(self, pull_request: Dict, journal: CommentJournal,
               decision: Decision) -> Decision:
        moved = self._head_moved(pull_request)
        if moved:
            # Everything was green for the commit that was evaluated, but the
            # branch moved: the next pass will evaluate the new commit.
            LOG.info("PR #%s not merged: %s", decision.pr_id, moved)
            decision.blockers = [moved]
            return decision

        message = merge_message(pull_request, decision.report)
        LOG.info("merging PR #%s into %s", decision.pr_id, decision.facts.target.branch)
        try:
            self.client.merge_pull_request(
                pull_request["id"], message=message,
                strategy=self.config.merge.strategy,
                close_source_branch=self.config.merge.close_source_branch)
        except BitbucketError as error:
            decision.action = ACTION_FAILED
            decision.blockers = ["merge refused by Bitbucket: {}".format(error)]
            if self.config.comments.on_failure:
                journal.post_once("merge-failed:{}".format(
                    ((pull_request.get("source") or {}).get("commit") or {}).get("hash", "")[:12]),
                    render.render_failure(str(error)))
            decision.comments_posted = list(journal.posted)
            return decision

        decision.action = ACTION_MERGED
        if self.config.comments.on_merge:
            journal.post_once(render.KEY_MERGED,
                              render.render_merged(pull_request, decision.facts))
        decision.comments_posted = list(journal.posted)
        return decision

    def _head_moved(self, pull_request: Dict) -> Optional[str]:
        """Re-read the pull request and check its head is still what we judged.

        Bitbucket's merge endpoint takes no expected-head parameter, so this is
        a last-moment re-check rather than a guarantee; it closes the window
        from "as long as the whole evaluation took" down to one API call.
        """
        evaluated = ((pull_request.get("source") or {}).get("commit") or {}).get("hash") or ""
        current = self.client.get_pull_request(pull_request["id"])
        head = ((current.get("source") or {}).get("commit") or {}).get("hash") or ""
        if head and evaluated and head != evaluated:
            return ("the source branch moved while the checks were running "
                    "({} -> {}); re-evaluating on the next pass".format(
                        evaluated[:12], head[:12]))
        if current.get("state") != "OPEN":
            return "pull request is no longer open ({})".format(current.get("state"))
        return None

    # -- CODEOWNERS -------------------------------------------------------

    def _evaluator_for(self, branch: str) -> OwnershipEvaluator:
        """Load CODEOWNERS and teams.yaml for a destination branch.

        By default they come from the DESTINATION branch of the merged
        repository, which is where Bitbucket itself reads them. When the bot is
        hosted somewhere else and `owners.repository` names a repository, they
        come from there instead -- the bot never needs a checkout of the code
        it merges.
        """
        owners_config = self.config.owners
        repository = owners_config.repository or None
        ref = owners_config.ref or branch
        cache_key = "{}@{}#{}".format(repository or self.client.repository, ref,
                                      owners_config.read_from)
        if cache_key in self._owners_cache:
            return self._owners_cache[cache_key]

        if owners_config.read_from == "local":
            codeowners_text = _read_local(owners_config.local_root, owners_config.codeowners_path)
            teams_text = _read_local(owners_config.local_root, owners_config.teams_path)
        else:
            codeowners_text = self.client.get_file(ref, owners_config.codeowners_path,
                                                   repository=repository)
            teams_text = self.client.get_file(ref, owners_config.teams_path,
                                              repository=repository)

        try:
            teams = TeamRegistry.parse(teams_text)
        except Exception as error:
            raise BitbucketError(
                "could not parse {} read from {}@{}: {}".format(
                    owners_config.teams_path, repository or self.client.repository, ref, error))

        evaluator = OwnershipEvaluator(
            codeowners=CodeOwners.parse(codeowners_text, source=owners_config.codeowners_path),
            teams=teams,
            resolver=IdentityResolver.from_config(self.config.identity.map,
                                                  self.config.identity.heuristics),
            rules_config=self.config.rules,
            workspace_groups=self.config.workspace_groups,
        )
        self._owners_cache[cache_key] = evaluator
        return evaluator


def merge_message(pull_request: Dict, report: Optional[OwnershipReport]) -> str:
    """The merge commit message this repository already uses.

        Merged in <branch> (pull request #NNN)

        <pull request title>

        Approved-by: <name>
    """
    branch = ((pull_request.get("source") or {}).get("branch") or {}).get("name", "")
    lines = ["Merged in {} (pull request #{})".format(branch, pull_request.get("id")),
             "", pull_request.get("title", "")]

    names: List[str] = []
    for requirement in (report.requirements if report else []):
        for approval in requirement.approvals:
            if approval.display_name not in names:
                names.append(approval.display_name)
    if not names and report:
        # No owner coverage was asked for (a promotion): fall back to whoever
        # approved, so the merge commit still records who signed it off.
        names = [user.get("display_name") for user in report.approvers
                 if user.get("display_name")]
    if names:
        lines.append("")
        lines += ["Approved-by: {}".format(name) for name in names]
    return "\n".join(lines)


def _read_local(root: str, path: str) -> str:
    with open(os.path.join(root, path), "r", encoding="utf-8") as handle:
        return handle.read()
