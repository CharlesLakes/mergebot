"""What Automerge writes on the pull request, and how it avoids repeating itself.

Everything rendered here is read by people on the pull request, so the OUTPUT
of this module is written in Spanish, like the rest of the repository's
documentation. The code, its identifiers and its comments stay in English like
every other module: only the strings that end up on Bitbucket are translated.

That split is also why this module renders from structured state (a
`PipelineStatus`, an `OwnershipReport`, a `GateFacts`) instead of reusing the
English sentences the bot logs: those belong to the logs and the CLI, and are
never quoted here.

Every comment carries an invisible marker at the end:

    <!-- automerge:v1 key=req:8f21ac0b1e -->

The marker is what makes the bot idempotent. Before writing anything it reads
the comments already on the pull request, indexes them by marker, and skips any
message whose key is already there. The status checklist uses the same
mechanism the other way around: its key is found and the comment is EDITED, so
the pull request keeps one live checklist instead of a wall of updates.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from approvals import (OPEN_AUTHOR_IS_OWNER, OPEN_EMPTY_TEAM, OPEN_NO_RULE,
                       OPEN_SINGLE_OWNER, OPEN_UNKNOWN_TEAM, OwnershipReport,
                       Requirement)
from config import DEFAULT_MARKER_NAMESPACE
from pipelines import (STATE_FAILED, STATE_MISSING, STATE_RUNNING,
                       PipelineStatus)

#: Bumped only if the shape of the marker itself ever changes. What comes
#: before it is the namespace, `bot.marker` in the configuration.
MARKER_VERSION = "v1"

KEY_STATUS = "status"
KEY_PIPELINE = "pipeline-green"
KEY_MERGED = "merged"

#: Spanish wording for each reason a requirement is open. The English version
#: in `approvals.py` is the one used by the logs and by `check`; this one is
#: what the pull request shows.
OPEN_REASON_ES = {
    OPEN_NO_RULE: "ninguna regla de CODEOWNERS reclama estos archivos, así que no tienen dueño",
    OPEN_UNKNOWN_TEAM: "el equipo está referenciado en CODEOWNERS pero no declarado en teams.yaml",
    OPEN_EMPTY_TEAM: "el equipo está declarado en teams.yaml pero no tiene integrantes",
    OPEN_SINGLE_OWNER: "el equipo tiene un solo integrante, que no puede revisar su propio trabajo",
    OPEN_AUTHOR_IS_OWNER: "el único dueño elegible es el autor del pull request",
}


def marker(key: str, namespace: str = DEFAULT_MARKER_NAMESPACE) -> str:
    return "<!-- {}:{} key={} -->".format(namespace, MARKER_VERSION, key)


def marker_re(namespace: str = DEFAULT_MARKER_NAMESPACE) -> "re.Pattern[str]":
    return re.compile(r"<!--\s*{}:{}\s+key=([^\s]+)\s*-->".format(
        re.escape(namespace), MARKER_VERSION))


def key_of(text: str, namespace: str = DEFAULT_MARKER_NAMESPACE) -> Optional[str]:
    found = marker_re(namespace).search(text or "")
    return found.group(1) if found else None


@dataclass
class ExistingComment:
    id: int
    key: Optional[str]
    raw: str


class CommentJournal:
    """The bot's own comments on one pull request, read once and reused."""

    def __init__(self, client, pr_id: int, dry_run: bool = False,
                 namespace: str = DEFAULT_MARKER_NAMESPACE) -> None:
        self.client = client
        self.pr_id = pr_id
        self.dry_run = dry_run
        self.namespace = namespace
        # Compiled once and not per comment: a busy pull request is read in
        # full on every pass.
        self._marker_re = marker_re(namespace)
        self._by_key: Dict[str, ExistingComment] = {}
        self.posted: List[str] = []
        self._load()

    def _load(self) -> None:
        for comment in self.client.list_comments(self.pr_id):
            if comment.get("deleted"):
                continue
            raw = ((comment.get("content") or {}).get("raw") or "")
            found = self._marker_re.search(raw)
            key = found.group(1) if found else None
            if key:
                self._by_key[key] = ExistingComment(id=comment["id"], key=key, raw=raw)

    def has(self, key: str) -> bool:
        return key in self._by_key

    def post_once(self, key: str, body: str) -> bool:
        """Write the comment unless one with the same key is already there."""
        if self.has(key):
            return False
        text = "{}\n\n{}".format(body.rstrip(), marker(key, self.namespace))
        if self.dry_run:
            self.posted.append(key)
            return True
        created = self.client.create_comment(self.pr_id, text)
        self._by_key[key] = ExistingComment(
            id=created.get("id", 0), key=key,
            raw=((created.get("content") or {}).get("raw") or text))
        self.posted.append(key)
        return True

    def upsert(self, key: str, body: str) -> bool:
        """Write the comment, or edit it in place when it already exists."""
        text = "{}\n\n{}".format(body.rstrip(), marker(key, self.namespace))
        existing = self._by_key.get(key)
        if existing and existing.raw.strip() == text.strip():
            return False  # nothing changed, do not touch the pull request
        if self.dry_run:
            self.posted.append(key)
            return True
        if existing:
            self.client.update_comment(self.pr_id, existing.id, text)
            existing.raw = text
        else:
            created = self.client.create_comment(self.pr_id, text)
            self._by_key[key] = ExistingComment(id=created.get("id", 0), key=key, raw=text)
        self.posted.append(key)
        return True


# -- rendering (Spanish: this is what the team reads on the pull request) ---

def requirement_key(requirement: Requirement) -> str:
    return "req:{}".format(requirement.key)


def _plural(count: int, singular: str, plural: str) -> str:
    return singular if count == 1 else plural


def _file_list(paths: Sequence[str], limit: int = 10) -> str:
    shown = ["- `{}`".format(path) for path in list(paths)[:limit]]
    remaining = len(paths) - len(shown)
    if remaining > 0:
        shown.append("- ...y {} {} más".format(remaining, _plural(remaining, "archivo", "archivos")))
    return "\n".join(shown)


def _approver_names(requirement: Requirement) -> str:
    names = []
    for approval in requirement.approvals:
        if approval.member:
            names.append("**{}** (dueño, `{}` — coincidencia por `{}`)".format(
                approval.display_name, approval.member, approval.method))
        else:
            names.append("**{}**".format(approval.display_name))
    return ", ".join(names) or "nadie"


def render_requirement_met(requirement: Requirement) -> str:
    """The message the bot is here for: this requirement is met, and why."""
    owners = ", ".join("`{}`".format(unit.token) for unit in requirement.units)
    lines = ["**Requisito de code owners cumplido** — {}".format(owners),
             "",
             "Aprobado por {}.".format(_approver_names(requirement))]

    open_units = [unit for unit in requirement.units if unit.is_open and unit.satisfied]
    if open_units and not any(approval.is_owner for approval in requirement.approvals):
        reasons = "; ".join(
            "`{}`: {}".format(unit.token, OPEN_REASON_ES.get(unit.open_reason, unit.open_reason))
            for unit in open_units)
        lines += ["",
                  "Este requisito estaba abierto porque {}. "
                  "Basta la aprobación de cualquier persona distinta del autor.".format(reasons)]

    count = len(requirement.paths)
    lines += ["",
              "Regla `{}` de `.bitbucket/CODEOWNERS` — {} {} cubierto{}:".format(
                  requirement.pattern, count, _plural(count, "archivo", "archivos"),
                  "" if count == 1 else "s"),
              _file_list(requirement.paths)]
    return "\n".join(lines)


def render_pipeline_green(status: PipelineStatus) -> str:
    runs = "\n".join("- [{}]({}) — {}".format(run.label, run.url, run.result)
                     for run in status.runs) or "- (sin corridas)"
    return ("**Requisito de pipeline cumplido** — todas las corridas del commit `{}` "
            "están en verde.\n\n{}".format(status.commit[:12], runs))


def render_status(pull_request: Dict, facts, will_merge: bool) -> str:
    """The live checklist, edited in place on every pass.

    The heading names the job and not the bot: Bitbucket already prints the
    account that wrote the comment right above it, and that name comes from the
    credentials, so anything written here could only contradict it.
    """
    report = facts.report
    head = ((pull_request.get("source") or {}).get("commit") or {}).get("hash", "")
    lines = ["## Estado del automerge",
             "",
             "| Requisito | Estado | Detalle |",
             "| --- | --- | --- |",
             "| Pipeline sobre `{}` | {} | {} |".format(
                 head[:12], _tick(facts.pipeline.satisfied), _cell(pipeline_detail_es(facts.pipeline)))]

    lines.append("| Aprobaciones ({} mínimo) | {} | {} |".format(
        facts.min_approvals, _tick(facts.approvals_counted >= facts.min_approvals),
        _cell("{} {} de alguien distinto del autor".format(
            facts.approvals_counted,
            _plural(facts.approvals_counted, "aprobación", "aprobaciones")))))

    if facts.target.require_codeowners and facts.diffstat_incomplete:
        lines.append("| Archivos del cambio | {} | {} |".format(
            _tick(False),
            _cell("la lista de archivos modificados llegó incompleta, así que no se puede "
                  "evaluar la cobertura por dueños. Se reintenta en la próxima pasada")))
    elif facts.target.require_codeowners and not facts.changed_paths:
        lines.append("| Archivos del cambio | {} | {} |".format(
            _tick(True),
            _cell("el pull request no modifica ningún archivo, así que no hay cobertura por "
                  "dueños que exigir")))

    if facts.changes_requested:
        names = ", ".join(user.get("display_name", "?") for user in facts.changes_requested)
        lines.append("| Cambios solicitados | {} | {} |".format(
            _tick(False), _cell("pendientes de resolver con {}".format(names))))
    if facts.open_tasks:
        lines.append("| Tasks del pull request | {} | {} |".format(
            _tick(False), _cell("{} sin resolver".format(facts.open_tasks))))

    for requirement in report.requirements:
        owners = ", ".join("`{}`".format(unit.token) for unit in requirement.units)
        detail = (_approver_names(requirement) if requirement.satisfied
                  else _pending_detail(requirement))
        count = len(requirement.paths)
        lines.append("| {} ({} {}) | {} | {} |".format(
            owners, count, _plural(count, "archivo", "archivos"),
            _tick(requirement.satisfied), _cell(detail)))

    # Only worth mentioning while something is still pending: with everything
    # covered, an approval that matched no owner is not a problem to report.
    if report.unmatched_approvers and report.pending:
        names = ", ".join(user.get("display_name", "?") for user in report.unmatched_approvers)
        lines += ["", "> Aprobaciones que no se pudieron atribuir a ningún dueño: {}. "
                      "Si alguna es de un dueño, agrégalo a `identity.map` en la configuración "
                      "del bot.".format(names)]

    lines += ["", "**Mergeando ahora.**" if will_merge
              else "Todavía no mergeo — falta lo que aparece como pendiente arriba."]
    return "\n".join(lines)


def _pending_detail(requirement: Requirement) -> str:
    for unit in requirement.units:
        if unit.is_open:
            return "falta una aprobación de cualquier persona distinta del autor ({})".format(
                OPEN_REASON_ES.get(unit.open_reason, unit.open_reason))
    people = sorted({member for unit in requirement.units for member in unit.eligible})
    if not people:
        return "sin dueño elegible"
    return "esperando la aprobación de: {}".format(", ".join(people))


def pipeline_detail_es(status: PipelineStatus) -> str:
    """The pipeline state as a sentence, for the pull request.

    Built from the structured status on purpose: `PipelineStatus.detail` is the
    English version and belongs to the logs.
    """
    commit = status.commit[:12]
    if status.state == STATE_MISSING:
        if status.missing:
            return "no corrió: {}".format(", ".join(status.missing))
        return "todavía no hay ninguna corrida para el commit `{}`".format(commit)
    if status.state == STATE_FAILED:
        return "falló: {}".format(", ".join(run.label for run in status.failed_runs))
    if status.state == STATE_RUNNING:
        return "todavía no está en verde: {}".format(
            ", ".join("{} [{}]".format(run.label, run.result)
                      for run in status.pending_runs))
    if not status.runs:
        return "verificación de pipeline desactivada en la configuración"
    return "todas las corridas del commit `{}` en verde".format(commit)


def render_merged(pull_request: Dict, facts) -> str:
    report, pipeline = facts.report, facts.pipeline
    covered = sum(len(requirement.paths) for requirement in report.requirements)
    branch = ((pull_request.get("destination") or {}).get("branch") or {}).get("name", "")
    lines = ["**Mergeado en `{}`.** Se cumplieron todos los requisitos:".format(branch),
             "",
             "- Pipeline: {}".format(pipeline_detail_es(pipeline))]

    if report.requirements:
        lines.append("- Code owners: {} {} cubierto{} por {} {}".format(
            covered, _plural(covered, "archivo", "archivos"), "" if covered == 1 else "s",
            len(report.requirements),
            _plural(len(report.requirements), "requisito", "requisitos")))
        for requirement in report.requirements:
            lines.append("  - `{}` → {}".format(requirement.pattern, _approver_names(requirement)))
    else:
        names = ", ".join("**{}**".format(user.get("display_name", "?"))
                          for user in report.approvers) or "nadie"
        if facts.target.require_codeowners:
            # Nothing to cover: the branch carries no change over its destination.
            lines.append("- Code owners: el pull request no modifica archivos, "
                         "así que no había cobertura que exigir")
        else:
            # A promotion between long-lived branches.
            lines.append("- Code owners: no se exige cobertura por dueños en esta rama de "
                         "destino (lo que trae ya se revisó al entrar a `develop`)")
        lines.append("- Aprobado por: {}".format(names))
    return "\n".join(lines)


def render_failure(reason: str) -> str:
    return ("**El automerge falló.** Todos los requisitos estaban cumplidos, pero Bitbucket "
            "rechazó el merge:\n\n```\n{}\n```\n\nLo habitual: un conflicto con la rama de "
            "destino, o una branch restriction que la cuenta del bot no puede "
            "satisfacer.".format(reason))


def _tick(value: bool) -> str:
    return "✅ cumplido" if value else "⏳ pendiente"


def _cell(text: str) -> str:
    return (text or "").replace("|", "\\|").replace("\n", " ")
