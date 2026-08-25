"""The pipeline gate: nothing is merged on top of a build that is not green.

What gates the merge is **the pull request's own pipeline**: the runs Bitbucket
triggers for it, which carry `target.type = pipeline_pullrequest_target` and
the id of the pull request. Everything else that happens to have run on the
same commit -- the `branches: develop` pipeline, a manually triggered
`custom: e2e` -- belongs to another story and must not block the merge. That
distinction matters in practice: a failed `custom: e2e` of `develop` was
blocking an unrelated pull request until the gate learned to tell them apart.

A specific extra run can still be demanded through `required_pipelines` (the
E2E suite before a promotion to `stg`, for instance). Those are looked up among
every run of the commit, precisely because a custom pipeline is never a pull
request one.

And it is always the HEAD commit of the source branch: if somebody pushes after
a green build, that build belongs to the previous commit and says nothing about
what would be merged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from bitbucket import (PIPELINE_FAILED, PIPELINE_RUNNING, PIPELINE_SUCCESS,
                       BitbucketClient)

LOG = logging.getLogger("automerge.pipelines")

STATE_SUCCESS = "successful"
STATE_RUNNING = "running"
STATE_FAILED = "failed"
STATE_MISSING = "missing"


#: `target.type` of a run triggered by a pull request.
TARGET_PULL_REQUEST = "pipeline_pullrequest_target"


@dataclass
class PipelineRun:
    uuid: str
    build_number: Optional[int]
    result: str
    selector: str
    url: str
    created_on: str = ""
    target_type: str = ""
    pull_request_id: Optional[int] = None

    @property
    def is_pull_request_run(self) -> bool:
        return self.target_type == TARGET_PULL_REQUEST

    @property
    def is_successful(self) -> bool:
        return self.result == PIPELINE_SUCCESS

    @property
    def is_running(self) -> bool:
        return self.result in PIPELINE_RUNNING

    @property
    def is_failed(self) -> bool:
        return self.result in PIPELINE_FAILED

    @property
    def label(self) -> str:
        number = "#{}".format(self.build_number) if self.build_number else self.uuid[:8]
        return "{} ({})".format(number, self.selector)


@dataclass
class PipelineStatus:
    state: str
    commit: str
    runs: List[PipelineRun] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)
    detail: str = ""

    @property
    def satisfied(self) -> bool:
        return self.state == STATE_SUCCESS

    @property
    def failed_runs(self) -> List[PipelineRun]:
        return [run for run in self.runs if run.is_failed]

    @property
    def running_runs(self) -> List[PipelineRun]:
        return [run for run in self.runs if run.is_running]


class PipelineGate:
    def __init__(self, client: BitbucketClient, checks_config) -> None:
        self.client = client
        self.config = checks_config

    def evaluate(self, commit_hash: str,
                 required_pipelines: Optional[Sequence[str]] = None,
                 pull_request_id: Optional[int] = None) -> PipelineStatus:
        """Judge the runs of `commit_hash` that belong to this pull request.

        `required_pipelines` demands extra runs by name -- the manually
        triggered E2E suite before a promotion, for instance. Those are searched
        among every run of the commit, since a custom pipeline is never a pull
        request one.
        """
        if not self.config.require_pipeline:
            return PipelineStatus(state=STATE_SUCCESS, commit=commit_hash,
                                  detail="pipeline check disabled in the configuration")

        raw_runs = self.client.list_pipelines_for_commit(commit_hash)
        all_runs = [self._parse(raw) for raw in raw_runs]

        # What gates the merge: the runs of THIS pull request, and nothing else.
        own = [run for run in all_runs
               if run.is_pull_request_run
               and (pull_request_id is None or run.pull_request_id == pull_request_id)]
        latest = _latest_per_selector(own)

        required = list(required_pipelines if required_pipelines is not None
                        else (self.config.required_pipelines or ()))
        every_latest = _latest_per_selector(all_runs)
        missing = [name for name in required
                   if not any(name.lower() in run.selector.lower() for run in every_latest)]
        # A demanded run has to be green too, wherever it came from.
        latest = latest + [run for run in every_latest
                           if any(name.lower() in run.selector.lower() for name in required)
                           and run not in latest]

        if not latest:
            return PipelineStatus(state=STATE_MISSING, commit=commit_hash, runs=latest,
                                  missing=required,
                                  detail="no pipeline has run for this pull request on commit "
                                         "{}".format(commit_hash[:12]))
        if missing:
            return PipelineStatus(state=STATE_MISSING, commit=commit_hash, runs=latest,
                                  missing=missing,
                                  detail="required pipeline(s) did not run: {}".format(
                                      ", ".join(missing)))

        failed = [run for run in latest if run.is_failed]
        if failed:
            return PipelineStatus(state=STATE_FAILED, commit=commit_hash, runs=latest,
                                  detail="failed: {}".format(
                                      ", ".join(run.label for run in failed)))

        running = [run for run in latest if run.is_running]
        if running:
            return PipelineStatus(state=STATE_RUNNING, commit=commit_hash, runs=latest,
                                  detail="still running: {}".format(
                                      ", ".join(run.label for run in running)))

        return PipelineStatus(state=STATE_SUCCESS, commit=commit_hash, runs=latest,
                              detail="all runs green on {}".format(commit_hash[:12]))

    def _parse(self, raw: Dict) -> PipelineRun:
        target = raw.get("target") or {}
        selector = (target.get("selector") or {})
        state = raw.get("state") or {}
        result = ((state.get("result") or state.get("stage") or {}).get("name")
                  or state.get("name") or "UNKNOWN")
        build_number = raw.get("build_number")
        # The pull request pipeline of this repository is declared as `'**'`,
        # which is unreadable in a comment: fall back to the selector type.
        pattern = selector.get("pattern")
        name = (pattern if pattern and pattern not in ("*", "**") else None) or \
            selector.get("type") or target.get("ref_name") or target.get("type") or "pipeline"
        pull_request = target.get("pullrequest") or {}
        return PipelineRun(
            target_type=str(target.get("type") or ""),
            pull_request_id=pull_request.get("id"),
            uuid=str(raw.get("uuid", "")).strip("{}"),
            build_number=build_number,
            result=str(result).upper(),
            selector=str(name),
            url="https://bitbucket.org/{}/{}/pipelines/results/{}".format(
                self.client.workspace, self.client.repository,
                build_number or str(raw.get("uuid", "")).strip("{}")),
            created_on=raw.get("created_on", ""),
        )


def _latest_per_selector(runs: Sequence[PipelineRun]) -> List[PipelineRun]:
    """Keep the newest run of each selector.

    A commit can be built more than once (a rerun after a flaky failure, a
    manually triggered `custom: e2e`). Only the last attempt of each counts,
    otherwise an old red run would block a branch that was fixed by a rerun.
    """
    newest: Dict[str, PipelineRun] = {}
    for run in runs:
        current = newest.get(run.selector)
        if current is None or (run.created_on or "") > (current.created_on or ""):
            newest[run.selector] = run
    return sorted(newest.values(), key=lambda run: run.selector)
