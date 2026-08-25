"""Thin Bitbucket Cloud REST v2 client -- only what the bot needs.

Authentication is either a repository/workspace access token (Bearer) or a
user plus app password (Basic). Both are read from the environment by
`config.Config.credentials()`.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple
from urllib.parse import quote

import requests

LOG = logging.getLogger("automerge.bitbucket")

#: Statuses a pipeline run can end in, grouped by what they mean for the merge.
PIPELINE_SUCCESS = "SUCCESSFUL"
PIPELINE_RUNNING = ("PENDING", "BUILDING", "IN_PROGRESS", "PAUSED", "HALTED")
PIPELINE_FAILED = ("FAILED", "ERROR", "STOPPED", "EXPIRED", "SKIPPED")


class BitbucketError(Exception):
    """An API call that did not go through."""

    def __init__(self, message: str, status: Optional[int] = None, payload: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.payload = payload


class BitbucketClient:
    def __init__(self, workspace: str, repository: str, base_url: str,
                 token: Optional[str] = None,
                 basic_auth: Optional[Tuple[str, str]] = None,
                 timeout: int = 30, max_retries: int = 3,
                 session: Optional[requests.Session] = None) -> None:
        self.workspace = workspace
        self.repository = repository
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = session or requests.Session()
        if token:
            self.session.headers["Authorization"] = "Bearer {}".format(token)
        elif basic_auth:
            self.session.auth = basic_auth
        self.session.headers.setdefault("Accept", "application/json")

    # -- plumbing ---------------------------------------------------------

    @property
    def repo_path(self) -> str:
        return "/repositories/{}/{}".format(self.workspace, self.repository)

    def _url(self, path: str) -> str:
        if path.startswith("http"):
            return path
        return "{}{}".format(self.base_url, path)

    def request(self, method: str, path: str, **kwargs) -> requests.Response:
        """One call, retried on the failures that are worth retrying.

        429 and 5xx are transient (Bitbucket throttles hard on busy
        workspaces); everything else is the bot's own fault and is raised.
        """
        url = self._url(path)
        last_error: Optional[BitbucketError] = None
        for attempt in range(1, self.max_retries + 1):
            response = self.session.request(method, url, timeout=self.timeout, **kwargs)
            if response.status_code < 400:
                return response
            retriable = response.status_code == 429 or response.status_code >= 500
            error = BitbucketError(
                "{} {} -> HTTP {}: {}".format(method, url, response.status_code,
                                              response.text[:500]),
                status=response.status_code,
                payload=_json_or_none(response),
            )
            if not retriable or attempt == self.max_retries:
                raise error
            last_error = error
            delay = min(2 ** attempt, 30)
            LOG.warning("retrying in %ss after %s", delay, error)
            time.sleep(delay)
        raise last_error  # pragma: no cover - the loop always returns or raises

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict:
        return self.request("GET", path, params=params).json()

    def paginate(self, path: str, params: Optional[Dict[str, Any]] = None,
                 limit: Optional[int] = None) -> Iterator[Dict]:
        """Walk a paginated collection, following `next` until it runs out."""
        page: Optional[str] = path
        page_params = dict(params or {})
        count = 0
        while page:
            payload = self.get(page, params=page_params)
            page_params = {}  # `next` already carries the query string
            for item in payload.get("values", []):
                yield item
                count += 1
                if limit is not None and count >= limit:
                    return
            page = payload.get("next")

    # -- pull requests ----------------------------------------------------

    def get_pull_request(self, pr_id: int) -> Dict:
        return self.get("{}/pullrequests/{}".format(self.repo_path, pr_id))

    def list_open_pull_requests(self, target_branches: Optional[Sequence[str]] = None) -> List[Dict]:
        params: Dict[str, Any] = {"state": "OPEN", "pagelen": 50}
        if target_branches:
            destinations = " OR ".join('destination.branch.name="{}"'.format(branch)
                                       for branch in target_branches)
            params["q"] = 'state="OPEN" AND ({})'.format(destinations)
        return list(self.paginate("{}/pullrequests".format(self.repo_path), params=params))

    def get_diffstat(self, pr_id: int) -> Tuple[List[str], Optional[int]]:
        """`(paths, entries_reported)` for a pull request.

        The second value is the `size` the API claims for the collection. It is
        returned so the caller can tell the two very different situations
        apart: a pull request that genuinely changes nothing (`size` 0), and a
        listing that came back short of what it announced -- which must never be
        read as "there is nothing to review here".
        """
        url = "{}/pullrequests/{}/diffstat".format(self.repo_path, pr_id)
        first = self.get(url, params={"pagelen": 100})
        reported = first.get("size")

        entries = list(first.get("values") or [])
        page = first.get("next")
        while page:
            payload = self.get(page)
            entries.extend(payload.get("values") or [])
            page = payload.get("next")

        paths: List[str] = []
        for entry in entries:
            for side in ("new", "old"):
                node = entry.get(side) or {}
                path = node.get("path")
                if path and path not in paths:
                    paths.append(path)
        if reported is not None and len(entries) != reported:
            LOG.warning("diffstat of PR #%s announced %s entries and returned %s",
                        pr_id, reported, len(entries))
        return paths, reported

    def list_changed_paths(self, pr_id: int) -> List[str]:
        """Every path the pull request touches, renames counted on both sides."""
        return self.get_diffstat(pr_id)[0]

    def list_participants(self, pull_request: Dict) -> List[Dict]:
        """Participants of the PR, refetched only if the payload lacks them."""
        participants = pull_request.get("participants")
        if participants is None:
            pr_id = pull_request["id"]
            participants = list(self.paginate(
                "{}/pullrequests/{}/participants".format(self.repo_path, pr_id)))
        return participants

    def list_comments(self, pr_id: int) -> List[Dict]:
        return list(self.paginate("{}/pullrequests/{}/comments".format(self.repo_path, pr_id),
                                  params={"pagelen": 100}))

    def create_comment(self, pr_id: int, text: str) -> Dict:
        body = {"content": {"raw": text}}
        return self.request("POST", "{}/pullrequests/{}/comments".format(self.repo_path, pr_id),
                            json=body).json()

    def update_comment(self, pr_id: int, comment_id: int, text: str) -> Dict:
        body = {"content": {"raw": text}}
        return self.request("PUT", "{}/pullrequests/{}/comments/{}".format(
            self.repo_path, pr_id, comment_id), json=body).json()

    def list_tasks(self, pr_id: int) -> List[Dict]:
        try:
            return list(self.paginate("{}/pullrequests/{}/tasks".format(self.repo_path, pr_id),
                                      params={"pagelen": 100}))
        except BitbucketError as error:
            # Tasks are not available on every plan; a missing endpoint must
            # not be read as "there are no open tasks".
            if error.status in (403, 404):
                LOG.warning("tasks endpoint unavailable (HTTP %s); skipping that check",
                            error.status)
                return []
            raise

    def merge_pull_request(self, pr_id: int, message: str, strategy: str,
                           close_source_branch: bool) -> Dict:
        body = {
            "type": "pullrequest",
            "message": message,
            "merge_strategy": strategy,
            "close_source_branch": close_source_branch,
        }
        return self.request("POST", "{}/pullrequests/{}/merge".format(self.repo_path, pr_id),
                            json=body).json()

    # -- pipelines --------------------------------------------------------

    def list_pipelines_for_commit(self, commit_hash: str, scan_limit: int = 200) -> List[Dict]:
        """Runs whose target commit is `commit_hash`, newest first.

        The filtering is done HERE and not by the server on purpose. Bitbucket
        accepts `q=target.commit.hash="..."` on this endpoint and then ignores
        it: it answers with the whole pipeline history, runs of other branches
        included. Trusting that answer is how a failed `custom: e2e` of another
        branch ends up blocking an unrelated pull request.

        `scan_limit` bounds how far back the history is walked. A pull request
        whose runs are older than that window reads as "no runs", which blocks
        the merge instead of waving it through.
        """
        path = "{}/pipelines/".format(self.repo_path)
        recent = self.paginate(path, params={"sort": "-created_on", "pagelen": 50},
                               limit=scan_limit)
        return [run for run in recent
                if same_commit(((run.get("target") or {}).get("commit") or {}).get("hash"),
                               commit_hash)]

    # -- files ------------------------------------------------------------

    def get_file(self, ref: str, path: str, repository: Optional[str] = None) -> str:
        """Raw contents of `path` at `ref` (branch name or commit hash).

        `repository` ("workspace/repo-slug") reads from another repository, so
        the bot can live anywhere and still read the rules of the repository it
        merges -- or read them from a third one that centralises them.
        """
        base = "/repositories/{}".format(repository) if repository else self.repo_path
        url = "{}/src/{}/{}".format(base, quote(ref, safe=""), quote(path.lstrip("/")))
        response = self.request("GET", url)
        # Decoded by hand on purpose. The `src` endpoint answers `text/plain`
        # with no charset, and `requests` then falls back to ISO-8859-1 as the
        # HTTP spec tells it to: every accent and every dash of a UTF-8 file
        # comes back as mojibake, and a `—` turns into a C1 control character
        # that the YAML parser refuses outright.
        try:
            return response.content.decode("utf-8")
        except UnicodeDecodeError:
            # Not UTF-8 after all: fall back to whatever the response claims,
            # rather than failing on a file that a human can still read.
            LOG.warning("%s is not valid UTF-8; decoding as %s", path, response.encoding)
            return response.content.decode(response.encoding or "latin-1", errors="replace")

    def get_current_user(self) -> Dict:
        return self.get("/user")


def same_commit(left: Optional[str], right: Optional[str]) -> bool:
    """Compare two commit hashes of possibly different lengths.

    A pull request payload carries the short hash (12 characters) while a
    pipeline carries the full 40, so a plain `==` between them is always false
    -- which silently turned "the runs of this commit" into "no runs at all".
    """
    if not left or not right:
        return False
    left, right = left.strip().lower(), right.strip().lower()
    shorter, longer = sorted((left, right), key=len)
    return len(shorter) >= 7 and longer.startswith(shorter)


def _json_or_none(response: requests.Response) -> Any:
    try:
        return response.json()
    except (ValueError, json.JSONDecodeError):
        return None
