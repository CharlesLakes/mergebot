# Automerge

Automerge bot for Bitbucket Cloud. It watches the open pull requests of a repository and
merges the ones that satisfy its workflow rules: pipelines green, and every changed file
covered by an approval from whoever owns it according to `.bitbucket/CODEOWNERS`.

Every time a requirement is met the bot says so in a comment on the pull request, so the
reason a pull request was merged — or the reason it was not — is written on the pull request
itself.

> The bot's code, comments and this README are in English. The messages it writes on the pull
> requests are in Spanish, because that is what the team reads there.

---

## 1. Which pull requests it merges

The layout this bot was written for has three long-lived branches, and two of the three hops
between them can be automated:

```
feature/xxx ──PR──> develop ──PR──> stg ──PR──> main
             automerge      automerge        by hand
```

| Destination | Source | Owner coverage | Pipelines |
|---|---|---|---|
| `develop` | any branch | required: every changed file must be covered | whatever ran on the commit must be green |
| `stg` | `develop` only | not required: what it carries was already reviewed on its way into `develop` | plus the manually triggered `e2e` suite |
| `main` | — | **never merged by the bot** | — |

`main` is protected twice: it is not listed under `targets`, and it is listed under
`manual_only_branches`, which makes the configuration refuse to start if somebody ever adds
it as a target. Production is promoted by a person.

## 2. When it merges

The five conditions, in the order the bot checks them:

1. **The pull request is mergeable to begin with**: open, not a draft, going to a configured
   destination from an allowed source, and its title carries no excluding marker (`[WIP]`,
   `[no-automerge]`).
2. **Nobody requested changes** and no task is left unresolved.
3. **The pipeline is green on the commit that would be merged.** Not "on the branch": if
   somebody pushes after a green build, that build belongs to another commit and says nothing
   about the code that would land.
4. **Code owner coverage** — every changed file is covered (§3), when the destination asks
   for it.
5. **At least one approval** from somebody other than the author.

If anything is missing the bot does not merge, and leaves the checklist updated on the pull
request. If everything holds it merges with `merge_commit` and the same message Bitbucket
writes itself (`Merged in <branch> (pull request #NNN)`), closing the source branch.

## 3. How coverage is decided

For every file in the diff the bot finds the `CODEOWNERS` rule that matches it — **the last
matching rule wins**, exactly as in Bitbucket — and groups the files by rule. Each group is a
**requirement**, and all of them must be met. That is what guarantees no file slips through:
a file no rule claims becomes a requirement of its own instead of being ignored.

A requirement is met when **somebody from the owning team approved the pull request**. The
author never covers anything, not even their own team's files.

**The two cases where a requirement opens up** (any approval from someone other than the
author covers it):

| Case | Why |
|---|---|
| The owning team has **a single member** | A team of one cannot review its own owner's work, and there is no second owner to fall back on when that person is away. Today it applies to `@teams/lead`, `@teams/backend`, `@teams/frontend` and `@teams/data`. |
| The file has **no owner** | No `CODEOWNERS` rule matches it, or the team claiming it is not declared in `.bitbucket/teams.yaml`, or that team is empty. Nobody is responsible for it, so any reviewer is as good as any other. |

There is a third case that opens up on its own and cannot be configured away: when **every**
eligible owner is the author of the pull request. Requiring an owner there would make the
pull request impossible to merge.

When a `CODEOWNERS` line lists more than one team
(`/src/modulo_b*/tests/  @teams/testing @teams/backend`), `rules.multi_owner_mode`
decides what is asked for:

- `any` (default, standard CODEOWNERS semantics): one approval from either team is enough;
- `all`: each listed team needs an approval of its own.

## 4. Setup

```bash
cd automerge
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env                    # credentials
cp automerge.example.yaml automerge.yaml  # configuration
```

`automerge.yaml` sits next to the bot's files (it is git-ignored, so each deployment keeps its
own) and holds the **policy**: which branches are merged and from where, what has to be
approved, which checks block, the merge strategy, what gets commented. Credentials never go
in it — those are the `.env`.

It is looked up in the working directory first and next to the bot's files second, so cron
and pipeline runs find it wherever they start from; `--config /path/to/automerge.yaml` names it
explicitly. Without it the bot runs on the built-in defaults (merge into `develop` only, full
owner coverage) and says so in a warning — enough to try it out, not what you want in
production.

### Credentials

They go in `.env` (git-ignored), never in the repository. The `.env` holds
**secrets and nothing else**: which repository is merged, the policy and the merge strategy
all live in `automerge.yaml`, so they stay versioned and reviewable. Either option works, and
the example file says where each one is created:

- **access token**, repository or workspace scoped → `AUTOMERGE_BITBUCKET_TOKEN`;
- **user + app password** → `AUTOMERGE_BITBUCKET_USERNAME` and `AUTOMERGE_BITBUCKET_APP_PASSWORD`.

Required permissions: *Pull requests: Write*, *Pipelines: Read*, *Repositories: Read*.

Paste the raw token value, with no `Bearer` prefix and no quotes: the bot builds the
`Authorization: Bearer <token>` header itself.

> The bot's account must **not** be one whose approval is expected to count. The bot merges
> and comments as that account; having it approve as well would pollute the approval count.

### The bot's identity

Every comment and every merge is attributed to whoever owns the credentials, and that decides
what people see next to the messages:

| Credentials | Shown as | Avatar |
|---|---|---|
| Access token | The token's name (call it `Automerge`) | Auto-generated, cannot be replaced |
| Dedicated Atlassian account | The account's display name | Its profile picture — upload whatever you like |

So a real face needs a real account: create an Atlassian account (`automerge@example.com`), set its
display name and profile picture, invite it to the workspace with write access, and use its
app password. It takes a Bitbucket user seat, which is the only cost of the difference.

**This is also why the bot has no name of its own to configure.** Bitbucket already prints the
account that wrote each comment, so nothing the bot writes repeats it: the checklist is headed
*Estado del automerge*, and a pull request is kept out of its way with `[no-automerge]`. Both
say what is happening rather than who is doing it, and stay true whatever the account is
called. The two places a name would otherwise be pinned down **are** configurable —
`checks.skip_title_markers`, because a person has to type it in a title, and `bot.marker`
(§6), because two bots on the same pull request must not overwrite each other's comments.

### Merge strategy

`merge.strategy` in `automerge.yaml`. Bitbucket Cloud offers three:

| Strategy | What it does |
|---|---|
| `merge_commit` | A merge commit that keeps the branch history. What this repository uses today (`Merged in <branch> (pull request #NNN)`), and the default. |
| `squash` | Collapses every commit of the branch into one. |
| `fast_forward` | Moves the destination pointer, with no merge commit. Only possible when the destination has nothing the branch does not already have. |

`close_source_branch` is separate and stays in the YAML.

### Running from another repository

The bot never needs a checkout of the code it merges: it reads `CODEOWNERS` and `teams.yaml`
through the API, from the destination branch of each pull request. Living inside the
repository it merges is convenient, not a requirement.

To host it elsewhere, name the repository to merge and nothing else:

```yaml
bitbucket:
  workspace: mi-workspace
  repository: mi-repositorio
```

The rules keep being read from that repository, so `owners.repository` stays empty. It exists
for a different case: when `CODEOWNERS` and `teams.yaml` live in a **different** repository
from the one being merged — a repository centralising the rules for several others. Then name
it, and pin the ref as well, because otherwise the bot looks there for the pull request's
destination branch, which that repository may not have:

```yaml
owners:
  repository: mi-workspace/reglas-compartidas   # workspace/repo-slug
  ref: develop               # branch or commit
```

## 5. Usage

```bash
python automerge.py check --pr 407     # evaluate and print the detail, touch nothing
python automerge.py run                # comment and merge whatever is ready
python automerge.py run --pr 407       # that pull request only
python automerge.py run --dry-run      # decide out loud, without commenting or merging
python automerge.py watch --interval 300
```

`check` is the command that answers *"why is this pull request not merging?"*: it prints
every requirement, which files it groups, who can cover it and who did.

```
PR #407 [waiting] feat(modulo_a): sincronizar el estado de los envíos
  - 1 file(s) matched by `/e2e/` are not covered: @teams/testing
  target: develop (codeowners: required)
  files -> requirements:
    [MET ] /src/modulo_a/ (3 file(s)) -> @teams/frontend
           open: the team has a single member, who cannot be its own reviewer
           approved by Martín Rondón
    [OPEN] /e2e/ (1 file(s)) -> @teams/testing
  pipeline: successful — all runs green on 4f1c9a2b0e11
```

### Running it unattended

Any scheduler will do; the bot keeps no state of its own (§8). By cron, every ten minutes:

```cron
*/10 * * * * cd /path/to/automerge && .venv/bin/python automerge.py run >> /var/log/automerge.log 2>&1
```

As a Bitbucket scheduled pipeline, by adding a `custom:` entry to the merged repository's
`bitbucket-pipelines.yml` and scheduling it from
*Pipelines → Schedules* (the token goes in *Repository variables*, marked *secured*):

```yaml
  custom:
    automerge:
      - step:
          name: Automerge
          image: python:3.11
          script:
            - pip install --quiet -r automerge/requirements.txt
            - cd automerge && python automerge.py run
```

## 6. What it writes on the pull request

| Comment | When |
|---|---|
| **Requisito de code owners cumplido** | The first time a requirement is met. It names who approved, which `CODEOWNERS` rule applies and which files it covers; if the requirement was open, it explains why. |
| **Requisito de pipeline cumplido** | The first time every run on the commit is green. |
| **Estado del automerge** | A single checklist, **edited in place** on every pass. It is the current state, not a log. |
| **Mergeado en `<rama>`** | On merge, with a summary of what was satisfied. |
| **El automerge falló** | When everything was satisfied but Bitbucket refused the merge (conflict, branch restriction). |

Every comment carries an invisible marker at the end (`<!-- automerge:v1 key=... -->`, where
`automerge` is `bot.marker` in the configuration) and that is what keeps the bot from repeating
itself: before writing it reads what it already said and
skips whatever is there. Requirement comments are written **once**; the live state — including
a requirement that stopped being met because somebody withdrew an approval — is always in the
checklist.

## 7. Configuration

Everything the bot decides comes from `automerge.yaml`, so that changing the policy is a YAML
edit and not a code change. `automerge.example.yaml` documents every option; the ones most
likely to be touched:

| Option | What it does |
|---|---|
| `checks.skip_title_markers` | The title markers that keep the bot off a pull request (`[no-automerge]` by default). The one place a name would otherwise be hardcoded where a person has to type it. |
| `bot.marker` | Namespace of the invisible marker of §6. Changing it makes the bot forget what it already said and post everything a second time, so it is only for running two bots on the same pull requests. |
| `targets` | The branches the bot may merge into, with their per-branch policy (`from`, `require_codeowners`, `min_approvals`, `required_pipelines`). |
| `owners.repository` / `owners.ref` | Read the rules from a repository other than the one being merged (rules centralised elsewhere). Not needed just to host the bot outside. |
| `merge.strategy` | `merge_commit` / `squash` / `fast_forward`. |
| `manual_only_branches` | Branches the bot must never merge into. `main` by default. |
| `rules.multi_owner_mode` | `any` / `all` when a rule lists several teams. |
| `rules.single_owner_fallback` | Turns off the exception for single-member teams. |
| `rules.unowned_fallback` | Turns off the exception for files with no owner. |
| `checks.required_pipelines` | Demand that certain runs exist, not only that whatever ran is green. |
| `merge.enabled` | `false` leaves the bot in "comment only" mode. |
| `identity.map` | The exact email → Bitbucket account mapping (§8). |

A misspelled option is a startup error, not a policy that silently does not apply: the loader
rejects keys it does not know.

## 8. Known limits

- **The Bitbucket API never exposes the email of an approver.** `teams.yaml` identifies people
  by email, so the two have to be matched: the bot uses `identity.map` from the configuration
  (exact, and always preferred) and, when there is no entry, heuristics — `nickname` equal to
  the local part of the email, or initial + surname, which is this workspace's habit
  (`jperez@example.com` is *Juana Pérez*). Whatever cannot be matched **does not count as an
  approval**: the bot fails closed, and the checklist lists the approvals it could not
  attribute so they can be added to the map.
- **Bitbucket does not reset approvals on a new push** unless the repository is configured to.
  If a push should invalidate what was approved, that is a merge check of the repository, not
  something the bot can decide.
- **Workspace groups** (`@workspace/group`) cannot be expanded through the API. If
  `CODEOWNERS` ever uses one, its members are declared under `workspace_groups` in the YAML;
  until then such a requirement is treated as having no owner.
- **The bot does not review the change.** It is not a reviewer: it only verifies that the
  right people already reviewed.
- **It keeps no state.** Everything it needs is re-read from the pull request on every pass,
  so it can be killed and relaunched with no consequences.

## 9. The files

| File | What is in it |
|---|---|
| `automerge.py` | Entry point: `python automerge.py <command>`. |
| `cli.py` | Arguments, logging, and the `check` printout. |
| `bot.py` | The orchestrator: the five conditions, in order, and the merge. |
| `approvals.py` | The policy: groups files by rule, resolves owners, decides what is covered. |
| `codeowners.py` | `CODEOWNERS` parser and matcher (`.gitignore`-style patterns, last match wins). |
| `teams.py` | Reader for `.bitbucket/teams.yaml`. |
| `identity.py` | The bridge between `teams.yaml` emails and Bitbucket accounts. |
| `pipelines.py` | The pipeline gate: the latest run of each selector on the commit. |
| `comments.py` | What it writes on the pull request (in Spanish) and how it avoids repeating itself. |
| `bitbucket.py` | Minimal REST v2 client, with retries and pagination. |
| `config.py` | Loading and validation of `automerge.yaml` and of the credentials. |
| `env.py` | Dependency-free `.env` reader. |

## 10. TODO — running on Forgejo, GitLab or GitHub

**None of this is implemented.** The bot talks to Bitbucket Cloud and only to Bitbucket Cloud.
This section is the note left while the assumptions were still fresh: what a port would take, and
which of them are traps.

The code already comes in two halves, and only one is about Bitbucket:

| Half | Files | State |
|---|---|---|
| **The policy** | `approvals.py`, `codeowners.py`, `teams.py`, `identity.py`, `config.py`, and the rendering half of `comments.py` | Already forge-agnostic. It reasons about paths, owner tokens, people and approvals — none of which belongs to Bitbucket. |
| **The forge** | `bitbucket.py`, `pipelines.py`, the journal half of `comments.py`, and the payload digging spread through `bot.py` | Bitbucket REST v2, top to bottom. |

So a port is not a rewrite: it is drawing the line that is nearly there already, and giving it a name.

### 10.1 What would have to be built

**A `Forge` interface**, with `bitbucket.py` as its first implementation. The surface is small — eleven
calls, which is the whole of what the bot asks of a forge today:

```
get_pull_request(id)                 list_comments(pr)
list_open_pull_requests(branches)    create_comment(pr, text)
get_diffstat(pr) -> (paths, size)    update_comment(pr, comment_id, text)
list_participants(pr)                merge_pull_request(pr, message, strategy, close_source)
list_tasks(pr)                       get_file(ref, path, repository=None)
list_pipelines_for_commit(hash)
```

(`get_current_user` and `list_changed_paths` exist on the client and nothing calls them; they can go.)

**Normalised payloads**, which is the larger job. The eleven calls are easy to reimplement; what leaks
is the *shape of the answer*. `bot.py` reaches into raw Bitbucket dicts —
`pull_request["source"]["commit"]["hash"]`, `destination.branch.name`, `draft`, `state == "OPEN"` —
and `approvals.py` identifies people by `uuid` / `account_id` / `nickname` / `display_name`, a
quartet that exists nowhere else: GitHub, GitLab and Forgejo each give one numeric id and one login.
A `PullRequest` / `User` dataclass built by each implementation would keep that out of the policy half.

**A per-forge run collector.** `PipelineGate` should keep its shape — "the latest result per name on
this commit, restricted to the ones this pull request triggered" is the idea, and it survives
everywhere — but the collecting is forge-specific (§10.3).

### 10.2 What each forge changes

| What the bot needs | Bitbucket (today) | Elsewhere |
|---|---|---|
| The object | pull request | *merge request* on GitLab, and the API path with it. Forgejo indexes by per-repository number, not by global id. |
| Approvals | `participants[].approved` | Reviews with a state (`APPROVED` / `CHANGES_REQUESTED`) on GitHub and Forgejo, where a newer review from the same person **supersedes** the older one — something `approvers_of` does not model, because a Bitbucket participant has only a current state. |
| Owner teams | `.bitbucket/teams.yaml`, a versioned list of emails, because Bitbucket has no team API | Real teams, readable from the API (`/orgs/{org}/teams/{slug}/members` and its equivalents). `teams.yaml` becomes optional, and **most of `identity.py` stops being necessary**: CODEOWNERS names `@org/team` and `@login` directly, so there is no email to guess an account from. |
| Owner rules | CODEOWNERS, last match wins | Same on GitHub and Forgejo, so `codeowners.py` carries over. **GitLab's format is a different one**: it has sections (`[Backend]`), optional sections and per-section approval counts. `CodeOwners.parse` would have to learn it, or refuse it out loud. |
| CI | Pipeline runs with a selector | Check runs *and* commit statuses on GitHub (two APIs, both have to be read); one pipeline per commit with jobs inside it on GitLab; Actions reported as commit statuses on Forgejo. |
| Merge | `merge_commit` / `squash` / `fast_forward` | Not a shared vocabulary: GitHub is merge / squash / **rebase**, and has no fast-forward; Bitbucket has no rebase. `MERGE_STRATEGIES` in `config.py` must become per-forge validation instead of one global tuple. |
| Tasks | `/pullrequests/{id}/tasks` | Bitbucket-only as a first-class thing. The analogue is an unresolved review thread on GitHub (**GraphQL only** — `isResolved` is not in REST), an unresolved discussion on GitLab (`blocking_discussions_resolved`, one clean boolean on the merge request), and nothing at all on Forgejo. `block_on_open_tasks` needs a per-forge answer, or an "unsupported" that degrades the way the missing endpoint already does. |
| Pagination | a `next` URL in the body | A `Link: rel=next` header on GitHub, `X-Next-Page` on GitLab, `page`/`limit` on Forgejo. `BitbucketClient.paginate` follows `next` and nothing else. |
| Config | `bitbucket:` | `forge: {type, base_url, ...}`. `workspace` is `owner` on GitHub and Forgejo and `namespace` on GitLab — worth keeping as aliases rather than renaming. `base_url` already exists, which is what a self-hosted Forgejo or GitLab needs. |

### 10.3 The traps

- **The pipeline gate is the least portable part of the bot**, and the one whose bugs are the most
  expensive — §8 already records that a failed `custom: e2e` of another branch was blocking unrelated
  pull requests. The concept that has to be reproduced is *"the runs this pull request triggered"*,
  not *"the runs on this commit"*. Bitbucket marks them `target.type = pipeline_pullrequest_target`;
  GitLab's equivalent is a detached pipeline (`source == merge_request_event`); GitHub's is a check
  run for the pull request's head, which on a `pull_request` trigger runs against a merge commit
  that **does not exist in either branch** — so `same_commit` against the source head silently
  matches nothing. That one needs a test before it needs code.
- **GitHub already enforces CODEOWNERS itself**, through branch protection, so the reason to run this
  bot there is not the coverage check but the two escape hatches of §3 — single-member teams and
  unowned files — which GitHub has no way to express. Worth being explicit about, otherwise the port
  duplicates a feature that is already there and adds nothing.
- **Verify what GitLab's tier actually gives you** before promising anything: approval *rules* are a
  paid feature, and how much of the approval API answers on Free decides whether the bot can read an
  approval at all there. If it can, Automerge replaces the paid feature rather than complementing it.
- **`_head_moved` stops being a compromise on GitHub and GitLab**: both accept an expected head SHA on
  the merge call, so what is a narrowed race here becomes an actual guarantee. Take it.
- The marker mechanism of §6 and the Spanish comments carry over untouched — every one of these
  forges takes Markdown comments with an edit endpoint, and an HTML comment is invisible in all of them.
