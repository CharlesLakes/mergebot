"""Command line entry point, driven by `automerge.py`.

    python automerge.py check --pr 407      # evaluate and report, no side effects
    python automerge.py run                 # comment and merge what is ready
    python automerge.py run --dry-run       # decide out loud, touch nothing
    python automerge.py watch --interval 300

`check` is `run --dry-run` with a prettier printout, and it is the command to
reach for when the question is "why is this pull request not merging?".
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from typing import List, Optional, Sequence

from approvals import OPEN_REASON_TEXT
from bitbucket import BitbucketClient, BitbucketError
from bot import ACTION_FAILED, ACTION_MERGED, Decision, Automerge
from config import DEFAULT_CONFIG_NAME, Config, ConfigError
from env import load_env_file

LOG = logging.getLogger("automerge")

#: Where the bot's own files live. Cron and Bitbucket Pipelines start the
#: process in whatever directory they happen to be in, so a bare relative
#: default that only resolves against the working directory is a trap.
BOT_DIR = os.path.dirname(os.path.abspath(__file__))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="automerge",
        description="Automerge bot for Bitbucket Cloud, driven by CODEOWNERS and pipelines.")
    parser.add_argument("command", choices=("run", "check", "watch"),
                        help="run: comment and merge. check: evaluate only. watch: run in a loop.")
    parser.add_argument("--config", default=None,
                        help="path to the YAML configuration (default: automerge.yaml, looked "
                             "up in the working directory and next to the bot)")
    parser.add_argument("--env-file", default=None,
                        help="file with the credentials (default: .env, same lookup)")
    parser.add_argument("--pr", type=int, action="append", dest="pull_requests",
                        help="restrict to this pull request id; repeatable")
    parser.add_argument("--dry-run", action="store_true",
                        help="evaluate and print, without commenting or merging")
    parser.add_argument("--interval", type=int, default=300,
                        help="seconds between passes in watch mode (default: %(default)s)")
    parser.add_argument("--verbose", "-v", action="store_true", help="debug logging")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    env_path, _ = _locate(args.env_file, ".env")
    if env_path:
        load_env_file(env_path)

    try:
        config = _load_config(args.config)
        if args.dry_run or args.command == "check":
            config.dry_run = True
        token, basic = config.credentials()
    except ConfigError as error:
        LOG.error("%s", error)
        return 2

    client = BitbucketClient(
        workspace=config.bitbucket.workspace,
        repository=config.bitbucket.repository,
        base_url=config.bitbucket.base_url,
        token=token, basic_auth=basic,
        timeout=config.bitbucket.timeout,
        max_retries=config.bitbucket.max_retries)
    bot = Automerge(config, client)

    if args.command == "watch":
        return _watch(bot, args)

    try:
        decisions = bot.run(args.pull_requests)
    except BitbucketError as error:
        # A failure on the very first call is almost always the credentials or
        # the repository name, and a stack trace helps nobody diagnose that.
        LOG.error("%s", error)
        LOG.error("%s", _hint_for(error, config))
        return 2

    _report(decisions, verbose=args.command == "check")
    return 0 if all(decision.action != ACTION_FAILED for decision in decisions) else 1


def _hint_for(error: BitbucketError, config: Config) -> str:
    """Turn the usual HTTP failures into the thing to go and check."""
    if error.status == 401:
        return ("The credentials were rejected. Check AUTOMERGE_BITBUCKET_TOKEN (or the "
                "username/app password pair) in the .env, and that the token has not expired.")
    if error.status == 403:
        return ("The credentials are valid but lack permissions. The bot needs "
                "Pull requests: Write, Pipelines: Read and Repositories: Read on {}/{}.".format(
                    config.bitbucket.workspace, config.bitbucket.repository))
    if error.status == 404:
        return ("Repository {}/{} not found. Check bitbucket.workspace and "
                "bitbucket.repository in the configuration -- the slug is the one in the "
                "repository URL, not its display name.".format(
                    config.bitbucket.workspace, config.bitbucket.repository))
    return "Unexpected API failure; run with --verbose for the full exchange."


def _locate(explicit: Optional[str], name: str):
    """`(path, was_explicit)`: the file as given, in the CWD, or next to the bot."""
    if explicit:
        return explicit, True
    for candidate in (os.path.join(os.getcwd(), name), os.path.join(BOT_DIR, name)):
        if os.path.exists(candidate):
            return candidate, False
    return None, False


def _load_config(explicit: Optional[str]) -> Config:
    """Load the configuration, or run on defaults when there is none.

    The YAML is the policy -- which branches are merged, what has to be
    approved, which checks block. It is optional only because a deployment can
    be described entirely by the environment; when it is missing, the built-in
    defaults apply (merge into `develop` only, full owner coverage) and that is
    said out loud rather than assumed silently.
    """
    path, was_explicit = _locate(explicit, DEFAULT_CONFIG_NAME)
    if path:
        LOG.info("configuration: %s", path)
        return Config.load(path)
    if was_explicit:
        raise ConfigError("configuration file not found: {}".format(explicit))
    LOG.warning("no %s found in %s or in %s: running on the built-in defaults plus the "
                "environment (merges into `develop` only). Copy %s to automerge.yaml to "
                "change the policy.", DEFAULT_CONFIG_NAME, os.getcwd(), BOT_DIR,
                os.path.join(BOT_DIR, "automerge.example.yaml"))
    return Config.from_dict({})


def _watch(bot: Automerge, args) -> int:
    branches = ", ".join(target.branch for target in bot.config.targets)
    LOG.info("watching %s every %ss (Ctrl-C to stop)", branches, args.interval)
    while True:
        try:
            _report(bot.run(args.pull_requests), verbose=False)
        except KeyboardInterrupt:
            return 0
        except BitbucketError as error:
            LOG.error("pass failed: %s", error)
            LOG.error("%s", _hint_for(error, bot.config))
        except Exception as error:  # keep the loop alive: a bad pass is not fatal
            LOG.exception("pass failed: %s", error)
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            return 0


def _report(decisions: List[Decision], verbose: bool) -> None:
    if not decisions:
        print("No open pull request to look at.")
        return
    for decision in decisions:
        print(decision.summary())
        if not verbose or decision.report is None:
            continue
        if decision.facts:
            print("  target: {} (codeowners: {})".format(
                decision.facts.target.branch,
                "required" if decision.facts.target.require_codeowners else "not required"))
        print("  files -> requirements:")
        for requirement in decision.report.requirements:
            owners = ", ".join(unit.token for unit in requirement.units)
            state = "MET " if requirement.satisfied else "OPEN"
            print("    [{}] {} ({} file(s)) -> {}".format(
                state, requirement.pattern, len(requirement.paths), owners))
            for unit in requirement.units:
                if unit.is_open:
                    print("           open: {}".format(
                        OPEN_REASON_TEXT.get(unit.open_reason, unit.open_reason)))
            for approval in requirement.approvals:
                print("           approved by {}{}".format(
                    approval.display_name,
                    " (owner: {})".format(approval.member) if approval.member else ""))
        if decision.pipeline:
            print("  pipeline: {} — {}".format(decision.pipeline.state, decision.pipeline.detail))
    merged = [decision for decision in decisions if decision.action == ACTION_MERGED]
    print("\n{} pull request(s) evaluated, {} merged.".format(len(decisions), len(merged)))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
