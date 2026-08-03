"""
AUT-011 — rapport d'INSTALLATION : ce qui tourne, d'où ça vient, ce qui manque.

Ce que ce module est
--------------------
Le dernier document de la chaîne de bootstrap, et la septième condition de
sortie du jalon M2. Le plan dit ce qui SERAIT fait ; le journal d'exécution dit
ce qui A ÉTÉ fait ; ce rapport-ci est ce qu'un opérateur archive, puis ressort
six mois plus tard quand on lui demande « qu'est-ce qui tourne exactement sur
cette machine, d'où ça vient, et qui l'a autorisé ». C'est aussi la pièce que
réclame le Lot D (SEC-007, SBOM et attestations).

Il agrège **deux documents figés** et n'en produit aucun tiers :

- le **plan** validé par `schema` — matériel, runtime résolu, catalogue,
  licences, décisions avec leur justification ;
- le **journal d'exécution** (`execution.ExecutionReport`) — ce qui a été fait,
  déjà satisfait, simulé, sauté, échoué ou jamais tenté.

Cinq promesses, et chacune est vérifiable ici
---------------------------------------------
1. **il ne prétend jamais plus que ce qui a été fait.** Les sept conditions de
   sortie de M2 sont évaluées une par une contre le journal, et une condition
   qu'aucune étape ne couvre sort `unproven` — jamais `satisfied` par défaut.
   Un rapport de simulation ne peut satisfaire aucune condition d'installation.
   Une étape ne prouve une condition que si elle la CONCERNE, **cible
   comprise** : `verify_artifact` sert deux domaines — l'archive du runtime et
   les GGUF des modèles — et vérifier le GGUF d'un modèle n'a jamais prouvé que
   le runtime était installé (COR-026). Un rapport qui ne peut pas dire non ne
   vaut rien ;
2. **il distingue le constat de l'hypothèse.** Le résolveur de runtime porte un
   champ `evidence` sur chaque variante, et certaines de ses entrées sont
   explicitement des hypothèses non vérifiées (§0.12). Elles sont extraites du
   plan, comptées, et rendues dans une section qui leur est propre. Fail-closed :
   un marqueur de preuve **inconnu** est traité comme une hypothèse, jamais
   comme un constat — c'est le sens sûr de l'erreur ;
3. **aucun secret, jamais.** Le rendu passe par `schema.find_secret_leaks()` et
   **refuse de publier** un document qui fuit, exactement comme `render_json` et
   `render_human` du plan. Le rapport traverse des tickets, des mails et des
   dépôts ;
4. **il est vérifiable après coup.** Le rapport embarque les deux documents
   sources et porte leurs empreintes, calculées par `execution.plan_fingerprint`
   — la même fonction que le journal, pas une seconde implémentation. On peut
   donc affirmer que ce rapport-là parle bien de ce plan-là et de cette
   exécution-là, et le prouver sans avoir gardé les originaux ;
5. **les champs récapitulatifs sont recalculés, jamais crus sur parole.**
   `validate_install_document()` reconstruit conditions, index, compteurs,
   verdict et empreintes **depuis les documents embarqués** et les confronte
   élément par élément à ce que le rapport annonce. La vague 5 a passé trois
   passes de revue à fermer ce trou dans le plan (§0.12) ; il ne se rouvre pas
   dans le document final de la chaîne.

Ce que ce module n'est pas
--------------------------
Il n'exécute rien, ne sonde aucun hôte, n'ouvre aucune socket et ne lance aucun
sous-processus. Il n'importe que la bibliothèque standard, `bootstrap.schema` et
`bootstrap.execution` : il ne connaît aucun producteur, ce qui lui permet d'être
écrit contre des contrats figés pendant que les producteurs de M2 sont encore en
chantier.

Il ne lit pas non plus l'horloge : `build_install_report()` exige une horloge en
argument, sans valeur par défaut, pour la raison qui prive `ExecutionMode` de
défaut — personne ne doit hériter d'un comportement par omission d'argument.

Différence avec `doctor.py`
---------------------------
`doctor` répond à « **cet hôte peut-il démarrer maintenant ?** » : il sonde le
système vivant (fichiers, ports, GPU, nginx, TLS, systemd), son verdict périme
dès qu'on touche à la machine, et il est rejoué avant chaque `systemctl start`.
Ce module répond à « **qu'a produit cette installation-ci, et qu'est-ce qui
reste à faire ?** » : il ne sonde rien, ne relit aucun fichier de l'hôte, et son
verdict est **définitif** parce qu'il porte sur deux documents immuables. Les
deux doivent coexister : un rapport d'installation vert ne dit pas que la
machine va bien aujourd'hui, et un `doctor` vert ne dit pas d'où viennent les
binaires ni sous quelle licence ils ont été installés.

Codes de sortie — alignés sur le plan et le journal, volontairement
-------------------------------------------------------------------
| Code | Signification                                                     |
|------|-------------------------------------------------------------------|
| 0    | Installation complète : les sept conditions de M2 sont satisfaites.|
| 1    | Une étape a échoué — l'installation n'est pas exploitable.         |
| 3    | Installation partielle : simulation, étapes sautées ou conditions  |
|      | non prouvées.                                                      |

Une simulation sort donc en 3, jamais en 0.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from . import execution, schema

# Version du rapport d'installation. Indépendante de celle du plan et de celle
# du journal : les trois documents évoluent séparément.
REPORT_SCHEMA_VERSION = 1

# Nom de l'outil, présent dans le document pour qu'un JSON archivé reste
# identifiable des années plus tard, sans son contexte d'appel.
REPORT_TOOL_NAME = "eva-bootstrap-install-report"

EXIT_OK = schema.EXIT_OK
EXIT_FAILED = schema.EXIT_BLOCKED
EXIT_PARTIAL = schema.EXIT_WARNINGS


class InstallReportError(schema.PlanError):
    """Le rapport ne peut pas être produit, ou son récapitulatif est incohérent."""


class SourcesRefused(InstallReportError):
    """
    Les documents sources ne permettent pas de produire un rapport. Toutes les raisons.

    Même choix que `execution.PlanRefused` : un opérateur qui répare une raison à
    la fois relance l'outil autant de fois qu'il y a de défauts.
    """

    def __init__(self, reasons: Sequence[str]) -> None:
        self.reasons: tuple[str, ...] = tuple(reasons)
        super().__init__("rapport d'installation refusé : " + " ; ".join(self.reasons))


# ── Verdict global ────────────────────────────────────────────────────────────

VERDICT_COMPLETE = "complete"
VERDICT_PARTIAL = "partial"
VERDICT_FAILED = "failed"

INSTALL_VERDICTS: frozenset[str] = frozenset({
    VERDICT_COMPLETE, VERDICT_PARTIAL, VERDICT_FAILED,
})

_VERDICT_EXIT: dict[str, int] = {
    VERDICT_COMPLETE: EXIT_OK,
    VERDICT_PARTIAL: EXIT_PARTIAL,
    VERDICT_FAILED: EXIT_FAILED,
}


# ── Conditions de sortie du jalon M2 ──────────────────────────────────────────

# Trois états, et la distinction entre les deux derniers est le cœur du document.
# `unsatisfied` : le journal prouve que la condition n'est PAS remplie.
# `unproven`    : rien ne la prouve, dans un sens ni dans l'autre. L'absence de
#                 preuve n'est pas une preuve — elle ne devient jamais un succès.
CONDITION_SATISFIED = "satisfied"
CONDITION_UNSATISFIED = "unsatisfied"
CONDITION_UNPROVEN = "unproven"

CONDITION_STATUSES: frozenset[str] = frozenset({
    CONDITION_SATISFIED, CONDITION_UNSATISFIED, CONDITION_UNPROVEN,
})


@dataclass(frozen=True)
class M2Condition:
    """
    Une des sept conditions de sortie de M2 (§13), et les étapes qui la prouvent.

    `actions` est le lien entre une phrase de jalon et des faits vérifiables : la
    condition n'est satisfaite que si TOUTES les étapes du plan portant ces
    actions ont abouti. Ces actions-là ne concernent qu'un domaine ; leur seule
    présence suffit à rattacher l'étape à la condition.

    `shared_actions` porte les actions à DEUX domaines. Il n'y en a qu'une,
    `verify_artifact`, que le planificateur émet une fois pour l'archive de
    `llama-server` et une fois par ensemble de GGUF (`runtime_installer.covers_step`
    documente la même ambiguïté côté applicateur). Une telle étape ne prouve une
    condition que si sa **cible** est aussi celle d'une étape `anchor_actions` —
    c'est-à-dire d'une action qui, elle, ne peut concerner que ce domaine. Sans
    cette borne, la vérification du GGUF d'un modèle prouvait « runtime installé »
    (COR-026, facette a) : la première condition du jalon, affirmée sur une preuve
    qui ne la regardait pas.

    La comparaison est une **égalité de chaînes**, jamais une analyse de forme :
    ce module ne sait pas lire une cible et n'a pas à l'apprendre. Ce qu'il
    suppose est plus faible et vérifiable dans le contrat : les quatre actions
    d'ancrage d'un modèle (`calibrate_model`, `enable_model`, `warmup_model`,
    `smoke_test`) portent l'identifiant du modèle **verbatim**, là où
    `write_registry` et `accept_license` le décorent — d'où leur absence de la
    liste, elles ne pourraient jamais s'apparier. Si un producteur change cette
    grammaire, l'appariement cesse, l'étape partagée cesse de prouver, et la
    condition retombe en `unproven` : l'erreur se voit, elle ne se maquille pas
    en `satisfied`. C'est le même sens sûr que celui d'`EVIDENCE_VERIFIED_MARKERS`.

    `self_evident` n'est vrai que pour la septième, « rapport final produit »,
    que ce document prouve en existant.

    L'identifiant s'appelle `code` et non `key` : `schema._SECRET_KEY_RE` refuse
    un champ nommé `key` porteur d'une valeur, et le rendu du rapport aurait été
    bloqué par son propre détecteur de fuite. La convention `code` est de toute
    façon celle de `schema.Finding`.
    """
    code: str
    label: str
    actions: tuple[str, ...]
    shared_actions: tuple[str, ...] = ()
    anchor_actions: tuple[str, ...] = ()
    self_evident: bool = False

    @property
    def proving_actions(self) -> tuple[str, ...]:
        """Toutes les actions susceptibles de prouver la condition, sans doublon."""
        return self.actions + tuple(
            a for a in self.shared_actions if a not in self.actions
        )


M2_CONDITIONS: tuple[M2Condition, ...] = (
    M2Condition(
        "runtime_installed",
        "Runtime installé et vérifié",
        (schema.ACTION_INSTALL_RUNTIME,),
        shared_actions=(schema.ACTION_VERIFY_ARTIFACT,),
        anchor_actions=(schema.ACTION_INSTALL_RUNTIME,),
    ),
    M2Condition(
        "model_downloaded",
        # Libellé élargi par COR-026 : un hôte dont les GGUF étaient déjà là et
        # attestés n'a rien téléchargé, et le jalon reste tenu. « Téléchargé »
        # aurait fait dire au rapport une chose qui n'a pas eu lieu ; ce que la
        # condition exige réellement, ce sont les octets de la révision figée.
        "Modèle en place à révision figée",
        (schema.ACTION_DOWNLOAD_MODEL,),
        shared_actions=(schema.ACTION_VERIFY_ARTIFACT,),
        anchor_actions=(
            schema.ACTION_CALIBRATE_MODEL,
            schema.ACTION_ENABLE_MODEL,
            schema.ACTION_WARMUP_MODEL,
            schema.ACTION_SMOKE_TEST,
        ),
    ),
    M2Condition(
        "license_accepted",
        "Licence acceptée",
        (schema.ACTION_ACCEPT_LICENSE,),
    ),
    M2Condition(
        "calibrated",
        "Calibration effectuée",
        (schema.ACTION_CALIBRATE_MODEL,),
    ),
    M2Condition(
        "warmed_up",
        "Modèle préchauffé",
        (schema.ACTION_WARMUP_MODEL,),
    ),
    M2Condition(
        "e2e_call",
        "Appel E2E réussi",
        (schema.ACTION_SMOKE_TEST,),
    ),
    M2Condition(
        "report_produced",
        "Rapport final produit",
        (),
        self_evident=True,
    ),
)

# Actions du plan qui ne prouvent AUCUNE condition de M2. Déclarées ici plutôt
# que laissées implicites : sans cette constante, une action nouvelle pourrait
# disparaître de l'évaluation sans que personne s'en aperçoive. Un test vérifie
# que la partition de `schema.PLAN_ACTIONS` est exhaustive.
#
# Écrire le registre et activer un modèle sont des MOYENS d'atteindre l'appel
# E2E, pas des conditions de sortie du jalon ; leur résultat reste visible dans
# le récapitulatif d'exécution, mais il ne fabrique pas de condition remplie.
#
# `enable_model` figure par ailleurs dans les `anchor_actions` de
# `model_downloaded` : servir d'ancre, c'est NOMMER une cible, pas la prouver.
# Une étape `enable_model` en échec ne rend donc aucune condition insatisfaite —
# elle dit seulement de quel modèle parle la vérification voisine.
CONDITION_UNMAPPED_ACTIONS: frozenset[str] = frozenset({
    schema.ACTION_WRITE_REGISTRY,
    schema.ACTION_ENABLE_MODEL,
})


@dataclass(frozen=True)
class ConditionOutcome:
    """État d'une condition de M2, avec la phrase qui dit PAR QUELLE PREUVE."""
    code: str
    label: str
    status: str
    proof: str
    actions: tuple[str, ...] = ()
    step_orders: tuple[int, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "label": self.label,
            "status": self.status,
            "proof": self.proof,
            "actions": list(self.actions),
            "step_orders": list(self.step_orders),
        }


# ── Niveaux de preuve ─────────────────────────────────────────────────────────

# Marqueurs de preuve reconnus comme des CONSTATS. Recopiés plutôt qu'importés :
# ce module ne connaît aucun producteur, et la copie échoue du bon côté — si un
# producteur renomme son marqueur, ses entrées basculent en « hypothèse » et
# deviennent plus visibles, jamais moins. L'inverse (une hypothèse promue en
# constat par un défaut de couplage) serait exactement le défaut à éviter.
#
# `constat-§6` vient de `runtime_resolver.EVIDENCE_SPEC` ; `🔬` et `📖` sont les
# deux niveaux « établis » de la Convention de suivi (§1). `🧭` — jugement
# d'architecture à confirmer — n'y figure volontairement pas.
EVIDENCE_VERIFIED_MARKERS: frozenset[str] = frozenset({
    "constat-§6",
    "🔬",
    "📖",
})


@dataclass(frozen=True)
class EvidenceClaim:
    """
    Une affirmation du plan et son niveau de preuve, avec le chemin où elle vit.

    Le chemin est ce qui rend le constat actionnable six mois plus tard : sans
    lui, « une hypothèse subsiste » n'aide personne à savoir laquelle.
    """
    path: str
    marker: str
    note: str
    verified: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "marker": self.marker,
            "note": self.note,
            "verified": self.verified,
        }


@dataclass(frozen=True)
class FieldRef:
    """Un champ scalaire relevé dans le plan : son chemin, son nom, sa valeur."""
    path: str
    field: str
    value: Any

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "field": self.field, "value": self.value}


@dataclass(frozen=True)
class StepPerformance:
    """Ce qu'une étape a coûté, et ce qu'elle a mesuré au passage."""
    order: int
    action: str
    target: str
    duration_ms: int
    metrics: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "order": self.order,
            "action": self.action,
            "target": self.target,
            "duration_ms": self.duration_ms,
            "metrics": self.metrics,
        }


# ── Index par convention de nommage ───────────────────────────────────────────
#
# Le rapport n'interprète AUCUNE structure propre à un producteur : il indexe le
# plan par nom de champ. C'est délibéré — un index adossé à des chemins
# (`sections[1].data.variant.artifact_sha256`) se briserait au premier
# producteur qui réorganise ses données, et se briserait EN SILENCE, en rendant
# un index vide qui se lit comme « aucune licence » au lieu de « je n'ai pas su
# regarder ». Un index par nom de champ dégrade autrement : il peut ramener un
# champ de trop, ce qui se voit, plutôt qu'en oublier un, ce qui ne se voit pas.

_LICENSE_KEY_RE = re.compile(r"licen[cs]e", re.IGNORECASE)

_FINGERPRINT_KEY_RE = re.compile(
    r"(sha256|sha512|digest|revision|commit|fingerprint|checksum|etag)",
    re.IGNORECASE,
)

_VERSION_KEY_RE = re.compile(r"(^|_)(version|build)(_|$)", re.IGNORECASE)

_SCALARS = (str, int, float, bool, type(None))


def _index_fields(document: Any, pattern: re.Pattern[str]) -> tuple[FieldRef, ...]:
    """
    Valeurs scalaires vivant sous un champ dont le NOM correspond au motif.

    La correspondance est **héritée** : `license: {"base_model": "apache-2.0"}`
    contribue `apache-2.0`, alors qu'aucun de ses champs terminaux ne contient le
    mot « licence ». Sans cet héritage, un index par nom de champ raterait
    exactement les données structurées — c'est-à-dire toutes celles qui comptent —
    et rendrait une liste vide qui se lit comme « aucune licence » au lieu de
    « je n'ai pas su regarder ».
    """
    relevés: list[FieldRef] = []
    _walk_index(document, "", "", False, pattern, relevés)
    return tuple(relevés)


def _walk_index(
    node: Any,
    path: str,
    field: str,
    inherited: bool,
    pattern: re.Pattern[str],
    out: list[FieldRef],
) -> None:
    retenu = inherited or bool(field and pattern.search(field))
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}" if path else str(key)
            _walk_index(value, child, str(key), retenu, pattern, out)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            # L'indice n'est pas un nom de champ : la liste hérite du sien.
            _walk_index(value, f"{path}[{index}]", field, retenu, pattern, out)
    elif retenu and isinstance(node, _SCALARS) and node is not None and node != "":
        out.append(FieldRef(path=path, field=field, value=node))


def _collect_evidence(node: Any, path: str, out: list[EvidenceClaim]) -> None:
    """
    Relève tout objet portant un champ `evidence`, où qu'il soit dans le plan.

    Structurel et non positionnel : le module ne sait pas quel producteur émet
    ces champs ni à quelle profondeur, et n'a pas à le savoir.
    """
    if isinstance(node, dict):
        marker = node.get("evidence")
        if isinstance(marker, str) and marker:
            note = node.get("evidence_note")
            out.append(EvidenceClaim(
                path=path or "<racine>",
                marker=marker,
                note=note if isinstance(note, str) else "",
                verified=marker in EVIDENCE_VERIFIED_MARKERS,
            ))
        for key, value in node.items():
            _collect_evidence(value, f"{path}.{key}" if path else str(key), out)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _collect_evidence(value, f"{path}[{index}]", out)


def _evidence_claims(plan_document: Any) -> tuple[EvidenceClaim, ...]:
    claims: list[EvidenceClaim] = []
    _collect_evidence(plan_document, "", claims)
    return tuple(claims)


# ── Évaluation des conditions ─────────────────────────────────────────────────

def _evaluate_conditions(results: Sequence[Any], *, applied: bool) -> tuple[ConditionOutcome, ...]:
    """
    Évalue les sept conditions de M2 depuis les résultats d'étapes, fail-closed.

    Travaille sur des dictionnaires — pas sur des `StepResult` — pour qu'une
    seule implémentation serve au calcul et à la revalidation d'un document
    relu. La règle de décision, elle, n'a qu'un sens : une condition n'est
    satisfaite que si toutes ses étapes ont abouti pour de bon, et une
    simulation n'en satisfait aucune.
    """
    outcomes: list[ConditionOutcome] = []
    for condition in M2_CONDITIONS:
        outcomes.append(_evaluate_condition(condition, results, applied=applied))
    return tuple(outcomes)


def _evaluate_condition(
    condition: M2Condition,
    results: Sequence[Any],
    *,
    applied: bool,
) -> ConditionOutcome:
    if condition.self_evident:
        return ConditionOutcome(
            code=condition.code,
            label=condition.label,
            status=CONDITION_SATISFIED,
            proof=(
                "ce document : il existe, il a été validé, et il porte les empreintes "
                "du plan et du journal auxquels il se rapporte"
            ),
        )

    concernés, directe, hors_domaine = _proving_steps(condition, results)
    orders = tuple(
        r["order"] for r in concernés
        if isinstance(r.get("order"), int) and not isinstance(r.get("order"), bool)
    )
    actions = condition.proving_actions

    if not concernés:
        return ConditionOutcome(
            code=condition.code,
            label=condition.label,
            status=CONDITION_UNPROVEN,
            proof=(
                "aucune étape du journal ne porte "
                + _liste(condition.actions)
                + " : rien n'atteste cette condition, dans un sens ni dans l'autre"
                + _clause_hors_domaine(condition, hors_domaine)
            ),
            actions=actions,
        )

    if not applied:
        # Le mode prime sur les statuts : un journal de simulation ne peut rien
        # attester d'une machine, même si toutes ses étapes se disent prêtes.
        return ConditionOutcome(
            code=condition.code,
            label=condition.label,
            status=CONDITION_UNPROVEN,
            proof=(
                f"le journal est une SIMULATION : ses {len(concernés)} étape(s) n'ont "
                "rien appliqué sur cet hôte"
            ),
            actions=actions,
            step_orders=orders,
        )

    statuses = [r.get("status") for r in concernés]
    for statut, phrase in (
        (execution.STEP_FAILED, "en échec"),
        (execution.STEP_NOT_ATTEMPTED, "non tentée(s) — l'état de l'hôte est inconnu"),
        (execution.STEP_SKIPPED, "sautée(s) délibérément"),
        (execution.STEP_WOULD_APPLY, "seulement simulée(s)"),
    ):
        fautives = tuple(
            r.get("order") for r in concernés if r.get("status") == statut
        )
        if fautives:
            return ConditionOutcome(
                code=condition.code,
                label=condition.label,
                status=CONDITION_UNSATISFIED,
                proof=(
                    "étape(s) "
                    + ", ".join(str(o) for o in fautives)
                    + f" : {phrase}"
                ),
                actions=actions,
                step_orders=orders,
            )

    inconnus = sorted({str(s) for s in statuses if s not in execution.STEP_STATUSES})
    if inconnus:
        # Fail-closed : un statut hors contrat ne devient pas un succès.
        return ConditionOutcome(
            code=condition.code,
            label=condition.label,
            status=CONDITION_UNSATISFIED,
            proof="statut d'étape hors contrat : " + ", ".join(inconnus),
            actions=actions,
            step_orders=orders,
        )

    if directe:
        preuve = (
            f"{len(concernés)} étape(s) appliquée(s) ou déjà satisfaite(s) : "
            + ", ".join(str(o) for o in orders)
        )
    else:
        # Le plan ne portait aucune étape propre à la condition — typiquement,
        # les GGUF étaient déjà présents et attestés, donc AUT-014 n'a émis
        # aucun `download_model`. Ce n'est pas la même chose que « rien ne
        # l'atteste » : la vérification d'intégrité a bien eu lieu, sur la
        # cible de cette condition, et le rapport le dit mot pour mot.
        preuve = (
            "aucune étape " + _liste(condition.actions) + " n'était au programme ; "
            "la condition est attestée autrement : étape(s) "
            + ", ".join(str(o) for o in orders) + " " + _liste(condition.shared_actions)
            + ", sur la cible de cette condition, appliquée(s) ou déjà satisfaite(s)"
        )

    return ConditionOutcome(
        code=condition.code,
        label=condition.label,
        status=CONDITION_SATISFIED,
        proof=preuve,
        actions=actions,
        step_orders=orders,
    )


def _liste(actions: Sequence[str]) -> str:
    """« a » ni « b » — la forme employée par toutes les phrases de preuve."""
    return " ni ".join(f"« {a} »" for a in actions)


def _domain_targets(condition: M2Condition, results: Sequence[Any]) -> frozenset[str]:
    """
    Cibles que des étapes NON ambiguës rattachent au domaine de cette condition.

    Vide si la condition n'a pas d'action partagée : rien à discriminer, donc
    aucune raison de parcourir le journal. Les cibles absentes, vides ou non
    textuelles ne sont jamais retenues — une cible qu'on ne sait pas lire ne
    rattache rien.
    """
    if not condition.shared_actions:
        return frozenset()
    return frozenset(
        r["target"]
        for r in results
        if isinstance(r, dict)
        and r.get("action") in condition.anchor_actions
        and isinstance(r.get("target"), str)
        and r["target"]
    )


def _proving_steps(
    condition: M2Condition, results: Sequence[Any]
) -> tuple[list[dict], bool, list[dict]]:
    """
    Trie le journal pour une condition : ce qui la prouve, et ce qui n'en est pas.

    Rend trois choses, dans l'ordre du journal :

    1. les étapes qui la concernent — actions propres, plus actions partagées
       dont la CIBLE est rattachée au domaine par une action d'ancrage ;
    2. si au moins une de ces étapes porte une action propre. C'est ce qui
       distingue « la condition a été remplie par son étape dédiée » de « son
       étape dédiée n'existait pas, une autre l'atteste » ;
    3. les étapes à action partagée qui ont été ÉCARTÉES faute de cible
       rattachable. Elles ne prouvent rien ici, mais leur existence change la
       phrase rendue : « aucune preuve » et « une preuve qui regarde ailleurs »
       ne se réparent pas de la même façon.
    """
    domaine = _domain_targets(condition, results)
    retenues: list[dict] = []
    ecartees: list[dict] = []
    directe = False
    for result in results:
        if not isinstance(result, dict):
            continue
        action = result.get("action")
        if action in condition.actions:
            retenues.append(result)
            directe = True
        elif action in condition.shared_actions:
            if result.get("target") in domaine:
                retenues.append(result)
            else:
                ecartees.append(result)
    return retenues, directe, ecartees


def _clause_hors_domaine(condition: M2Condition, ecartees: Sequence[dict]) -> str:
    """Dit pourquoi une étape à action partagée n'a rien prouvé ici. Vide s'il n'y en a pas."""
    if not ecartees:
        return ""
    orders = ", ".join(
        str(r.get("order")) for r in ecartees
        if isinstance(r.get("order"), int) and not isinstance(r.get("order"), bool)
    )
    return (
        f" — l'étape ou les étapes {orders} portent bien "
        + _liste(condition.shared_actions)
        + ", mais leur cible n'est celle d'aucune étape "
        + _liste(condition.anchor_actions)
        + " : elles attestent un autre domaine"
    )


# ── Compteurs ─────────────────────────────────────────────────────────────────

_COUNT_KEYS: tuple[str, ...] = (
    "conditions_total",
    "conditions_satisfied",
    "conditions_unsatisfied",
    "conditions_unproven",
    "evidence_claims",
    "hypotheses",
    "licenses",
    "versions",
    "fingerprints",
    "steps_total",
    "steps_applied",
    "failures",
)

_ABSENT = object()

# Clés du document rendu. Le jeu est FERMÉ dans les deux sens : une clé absente
# et une clé en trop sont toutes deux des erreurs.
#
# Fermer le jeu ferme une falsification que la comparaison champ par champ ne
# voit pas : ajouter à la racine un `"conclusion": "installation validée"` que
# rien ne recoupe. Le même trou existe dans `execution.validate_execution_document`,
# qui accepte une clé racine inconnue et ne contrôle pas `plan_schema_version`.
_DOCUMENT_KEYS: frozenset[str] = frozenset({
    "tool",
    "schema_version",
    "plan_schema_version",
    "execution_schema_version",
    "generated_at",
    "mode",
    "applied",
    "verdict",
    "exit_code",
    "plan_generated_at",
    "plan_fingerprint",
    "execution_fingerprint",
    "counts",
    "conditions",
    "hypotheses",
    "licenses",
    "versions",
    "fingerprints",
    "performance",
    "plan",
    "execution",
})


# ── Le rapport ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class InstallReport:
    """
    Rapport d'installation. Tout ce qui récapitule est DÉRIVÉ, rien n'est stocké.

    `verdict()`, `counts()`, `conditions()` et les index se recalculent à chaque
    appel depuis `plan` et `execution`, et `validate_install_document()` refait
    le même calcul sur le document rendu. Aucun champ ne peut donc être retouché
    à la main sans être détecté : c'est la leçon des trois passes de revue de la
    vague 5, appliquée d'emblée au dernier document de la chaîne.

    `plan` est le document de plan **verbatim**, embarqué et non résumé. C'est ce
    qui rend le rapport auto-suffisant : on peut recalculer son empreinte, donc
    prouver de quel plan il parle, sans avoir gardé le fichier d'origine.
    """
    generated_at: str
    plan: dict[str, Any]
    execution: execution.ExecutionReport
    schema_version: int = REPORT_SCHEMA_VERSION

    # ── Empreintes ────────────────────────────────────────────────────────────

    @property
    def plan_fingerprint(self) -> str:
        """Empreinte du plan, recalculée. `execution` fournit la fonction."""
        return execution.plan_fingerprint(self.plan)

    @property
    def execution_fingerprint(self) -> str:
        """Empreinte du journal d'exécution, par la même fonction que le plan."""
        return execution.plan_fingerprint(self.execution.to_dict())

    # ── Lecture d'état ────────────────────────────────────────────────────────

    def conditions(self) -> tuple[ConditionOutcome, ...]:
        return _evaluate_conditions(
            [r.to_dict() for r in self.execution.results],
            applied=self.execution.applied,
        )

    def evidence_claims(self) -> tuple[EvidenceClaim, ...]:
        return _evidence_claims(self.plan)

    def hypotheses(self) -> tuple[EvidenceClaim, ...]:
        """Affirmations du plan qui ne sont PAS des constats. Jamais fondues dans le reste."""
        return tuple(c for c in self.evidence_claims() if not c.verified)

    def licenses(self) -> tuple[FieldRef, ...]:
        return _index_fields(self.plan, _LICENSE_KEY_RE)

    def versions(self) -> tuple[FieldRef, ...]:
        return _index_fields(self.plan, _VERSION_KEY_RE)

    def fingerprints(self) -> tuple[FieldRef, ...]:
        return _index_fields(self.plan, _FINGERPRINT_KEY_RE)

    def performance(self) -> tuple[StepPerformance, ...]:
        return _performance_from_results([r.to_dict() for r in self.execution.results])

    def total_duration_ms(self) -> int:
        return sum(r.duration_ms for r in self.execution.results)

    def counts(self) -> dict[str, int]:
        outcomes = self.conditions()
        claims = self.evidence_claims()
        return {
            "conditions_total": len(outcomes),
            "conditions_satisfied": sum(1 for o in outcomes if o.status == CONDITION_SATISFIED),
            "conditions_unsatisfied": sum(1 for o in outcomes if o.status == CONDITION_UNSATISFIED),
            "conditions_unproven": sum(1 for o in outcomes if o.status == CONDITION_UNPROVEN),
            "evidence_claims": len(claims),
            "hypotheses": sum(1 for c in claims if not c.verified),
            "licenses": len(self.licenses()),
            "versions": len(self.versions()),
            "fingerprints": len(self.fingerprints()),
            "steps_total": len(self.execution.results),
            "steps_applied": sum(
                1 for r in self.execution.results if r.status in execution.APPLIED_STATUSES
            ),
            "failures": len(self.execution.failures()),
        }

    def unmet_conditions(self) -> tuple[ConditionOutcome, ...]:
        return tuple(o for o in self.conditions() if o.status != CONDITION_SATISFIED)

    def verdict(self) -> str:
        """
        Verdict d'installation, recalculé. L'échec l'emporte, puis toute condition non tenue.

        Trois barrières indépendantes empêchent un « complet » de sortir à tort :
        un échec au journal, une condition non satisfaite, et un journal qui n'a
        rien appliqué. La troisième fait doublon avec la deuxième — une
        simulation rend déjà toutes les conditions `unproven` — et c'est
        délibéré : c'est le seul champ de ce document qu'un script d'exploitation
        lira sans lire le reste.
        """
        if self.execution.verdict() == execution.VERDICT_FAILED:
            return VERDICT_FAILED
        if self.unmet_conditions():
            return VERDICT_PARTIAL
        if not self.execution.applied:
            return VERDICT_PARTIAL
        if self.execution.verdict() != execution.VERDICT_OK:
            return VERDICT_PARTIAL
        return VERDICT_COMPLETE

    def exit_code(self) -> int:
        return _VERDICT_EXIT[self.verdict()]

    # ── Sérialisation ─────────────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": REPORT_TOOL_NAME,
            "schema_version": self.schema_version,
            "plan_schema_version": schema.PLAN_SCHEMA_VERSION,
            "execution_schema_version": execution.EXECUTION_SCHEMA_VERSION,
            "generated_at": self.generated_at,
            "mode": self.execution.mode.value,
            "applied": self.execution.applied,
            "verdict": self.verdict(),
            "exit_code": self.exit_code(),
            "plan_generated_at": self.execution.plan_generated_at,
            "plan_fingerprint": self.plan_fingerprint,
            "execution_fingerprint": self.execution_fingerprint,
            "counts": self.counts(),
            "conditions": [c.to_dict() for c in self.conditions()],
            "hypotheses": [h.to_dict() for h in self.hypotheses()],
            "licenses": [f.to_dict() for f in self.licenses()],
            "versions": [f.to_dict() for f in self.versions()],
            "fingerprints": [f.to_dict() for f in self.fingerprints()],
            "performance": {
                "total_duration_ms": self.total_duration_ms(),
                "steps": [p.to_dict() for p in self.performance()],
            },
            "plan": self.plan,
            "execution": self.execution.to_dict(),
        }


def _performance_from_results(results: Sequence[Any]) -> tuple[StepPerformance, ...]:
    """
    Ce que chaque étape a coûté, et les mesures qu'elle a rapportées.

    Les métriques sont les valeurs NUMÉRIQUES de premier niveau de `evidence` —
    TTFT, octets écrits, tokens par seconde. Les booléens sont exclus
    explicitement : en Python `True` est un entier, et une preuve booléenne
    rangée parmi des mesures se lirait comme la mesure « 1 ».
    """
    out: list[StepPerformance] = []
    for result in results:
        if not isinstance(result, dict):
            continue
        evidence = result.get("evidence")
        evidence = evidence if isinstance(evidence, dict) else {}
        metrics = {
            str(k): v
            for k, v in evidence.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
        }
        duration = result.get("duration_ms")
        duration = duration if isinstance(duration, int) and not isinstance(duration, bool) else 0
        if not metrics and not duration:
            continue
        out.append(StepPerformance(
            order=result.get("order"),
            action=result.get("action"),
            target=result.get("target"),
            duration_ms=duration,
            metrics=metrics,
        ))
    return tuple(out)


# ── Assemblage ────────────────────────────────────────────────────────────────

def build_install_report(
    *,
    plan_document: Any,
    execution_report: execution.ExecutionReport,
    now: Callable[[], str],
) -> InstallReport:
    """
    Assemble le rapport, ou refuse. Aucune production « au mieux ».

    `now` n'a **pas de valeur par défaut** : ce module ne lit jamais l'horloge, et
    personne n'hérite d'une horloge par omission d'argument — même raison que
    l'absence de défaut sur `execution.ExecutionMode`.

    Six raisons de refuser, accumulées puis rendues ensemble :

    1. le plan n'est pas un document de plan valide (`schema.validate_plan_dict`) ;
    2. le plan expose un secret ;
    3. le plan n'était pas applicable — rien n'aurait dû être exécuté ;
    4. le journal d'exécution est incohérent (`validate_execution_document`) ;
    5. le journal expose un secret ;
    6. **le journal ne parle pas de ce plan** : empreinte ou date de génération
       divergentes. C'est le refus le plus important du module — sans lui, un
       rapport pourrait présenter le journal d'une installation réussie à côté
       du plan d'une autre, et rien ne le contredirait.
    """
    reasons: list[str] = []

    if not isinstance(plan_document, dict):
        raise SourcesRefused((
            f"le plan doit être un objet JSON, reçu {type(plan_document).__name__}",
        ))
    if not isinstance(execution_report, execution.ExecutionReport):
        raise SourcesRefused((
            "execution_report doit être un ExecutionReport, reçu "
            f"{type(execution_report).__name__}",
        ))

    reasons.extend(
        f"plan invalide : {error}" for error in schema.validate_plan_dict(plan_document)
    )
    reasons.extend(
        f"secret exposé par le plan : {leak}"
        for leak in schema.find_secret_leaks(plan_document)
    )
    if plan_document.get("applicable") is not True:
        reasons.append(
            f"le plan annonce applicable={plan_document.get('applicable')!r} : aucune "
            "installation n'aurait dû être exécutée à partir de lui, et aucun rapport "
            "d'installation ne peut donc s'y adosser"
        )

    execution_document = execution_report.to_dict()
    reasons.extend(
        f"journal invalide : {error}"
        for error in execution.validate_execution_document(execution_document)
    )
    reasons.extend(
        f"secret exposé par le journal : {leak}"
        for leak in schema.find_secret_leaks(execution_document)
    )

    attendue = execution.plan_fingerprint(plan_document)
    if execution_report.plan_fingerprint != attendue:
        reasons.append(
            f"le journal se rapporte au plan {execution_report.plan_fingerprint}, alors "
            f"que le plan fourni a pour empreinte {attendue} — ces deux documents ne "
            "décrivent pas la même installation"
        )
    if execution_report.plan_generated_at != plan_document.get("generated_at"):
        reasons.append(
            f"le journal annonce un plan généré le {execution_report.plan_generated_at!r}, "
            f"le plan fourni annonce {plan_document.get('generated_at')!r}"
        )

    if reasons:
        raise SourcesRefused(reasons)

    horodatage = now()
    if not isinstance(horodatage, str) or not horodatage:
        raise SourcesRefused((
            f"l'horloge fournie a rendu {horodatage!r} : un rapport archivé sans date "
            "n'est pas rattachable à une installation",
        ))

    return InstallReport(
        generated_at=horodatage,
        plan=plan_document,
        execution=execution_report,
    )


# ── Validation du rapport rendu ───────────────────────────────────────────────

def validate_install_document(document: Any) -> tuple[str, ...]:
    """
    Contrôle un rapport d'installation sérialisé. Retourne les erreurs, vide si sain.

    Ce rapport est ce qu'un auditeur, un ticket ou une revue de sécurité liront
    des mois plus tard pour conclure qu'une machine a été installée correctement.
    **Aucun de ses champs récapitulatifs n'est cru sur parole** : conditions,
    index, compteurs, verdict, code de sortie et empreintes sont tous
    reconstruits depuis les deux documents embarqués, puis confrontés élément par
    élément. Un rapport retouché à la main pour transformer six conditions non
    prouvées en six conditions satisfaites est rejeté.
    """
    errors: list[str] = []

    if not isinstance(document, dict):
        return (f"le rapport doit être un objet JSON, reçu {type(document).__name__}",)

    if document.get("tool") != REPORT_TOOL_NAME:
        errors.append(
            f"tool doit valoir « {REPORT_TOOL_NAME} », reçu {document.get('tool')!r}"
        )

    version = document.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        errors.append(f"schema_version doit être un entier, reçu {version!r}")
    elif version != REPORT_SCHEMA_VERSION:
        errors.append(
            f"schema_version {version} n'est pas celle de cet outil ({REPORT_SCHEMA_VERSION})"
        )

    # Les versions des DEUX contrats amont sont contrôlées ici, et pas seulement
    # celle du rapport : un rapport archivé doit dire contre quels contrats il a
    # été produit, sinon le champ n'est qu'un ornement.
    for champ, attendue in (
        ("plan_schema_version", schema.PLAN_SCHEMA_VERSION),
        ("execution_schema_version", execution.EXECUTION_SCHEMA_VERSION),
    ):
        valeur = document.get(champ)
        if valeur != attendue or isinstance(valeur, bool):
            errors.append(f"{champ} doit valoir {attendue}, reçu {valeur!r}")

    for key in ("generated_at", "plan_generated_at"):
        if not isinstance(document.get(key), str) or not document.get(key):
            errors.append(f"{key} doit être une chaîne non vide")

    inconnues = sorted(set(document) - _DOCUMENT_KEYS)
    if inconnues:
        errors.append(
            "le rapport porte des clés hors contrat : " + ", ".join(inconnues)
            + " — rien ne les recoupe, elles ne peuvent donc rien attester"
        )
    manquantes = sorted(_DOCUMENT_KEYS - set(document))
    if manquantes:
        errors.append("clés obligatoires absentes : " + ", ".join(manquantes))

    plan = document.get("plan")
    if not isinstance(plan, dict):
        errors.append(f"plan doit être un objet, reçu {type(plan).__name__}")
    execution_document = document.get("execution")
    if not isinstance(execution_document, dict):
        errors.append(f"execution doit être un objet, reçu {type(execution_document).__name__}")

    if errors:
        return tuple(errors)

    # Les deux documents sources doivent tenir SEULS avant qu'on en dérive quoi
    # que ce soit : recalculer un verdict depuis un journal incohérent
    # produirait du bruit, pas un diagnostic.
    errors.extend(f"plan embarqué invalide : {e}" for e in schema.validate_plan_dict(plan))
    errors.extend(
        f"journal embarqué invalide : {e}"
        for e in execution.validate_execution_document(execution_document)
    )
    if errors:
        return tuple(errors)

    errors.extend(_validate_install_summary(document, plan, execution_document))
    return tuple(errors)


def _validate_counts(counts: dict) -> list[str]:
    """
    Valide `counts` clé par clé, sans coercition numérique.

    Repris de `schema._validate_counts` et de `execution._validate_execution_counts`
    pour la même raison qu'eux : `True == 1`, `False == 0` et `1.0 == 1`. Un
    rapport entièrement booléen passerait l'égalité de dictionnaires tout en
    violant le contrat.
    """
    errors: list[str] = []
    for key in _COUNT_KEYS:
        value = counts.get(key, _ABSENT)
        if value is _ABSENT:
            errors.append(f"counts.{key} est obligatoire")
        elif not isinstance(value, int) or isinstance(value, bool) or value < 0:
            errors.append(
                f"counts.{key} doit être un entier >= 0, reçu {type(value).__name__}"
            )
    for key in counts:
        if key not in _COUNT_KEYS:
            errors.append(f"counts contient une clé inconnue : {key!r}")
    return errors


def _validate_install_summary(
    document: dict,
    plan: dict,
    execution_document: dict,
) -> list[str]:
    """Recalcule TOUT le récapitulatif depuis les documents embarqués."""
    errors: list[str] = []

    for champ, attendu, libelle in (
        ("verdict", str, "une chaîne"),
        ("exit_code", int, "un entier"),
        ("applied", bool, "un booléen"),
        ("mode", str, "une chaîne"),
        ("counts", dict, "un objet"),
        ("conditions", list, "une liste"),
        ("hypotheses", list, "une liste"),
        ("licenses", list, "une liste"),
        ("versions", list, "une liste"),
        ("fingerprints", list, "une liste"),
        ("performance", dict, "un objet"),
    ):
        valeur = document.get(champ, _ABSENT)
        if valeur is _ABSENT:
            errors.append(f"{champ} est obligatoire")
        elif not isinstance(valeur, attendu) or (attendu is int and isinstance(valeur, bool)):
            errors.append(f"{champ} doit être {libelle}, reçu {type(valeur).__name__}")
    if isinstance(document.get("counts"), dict):
        errors.extend(_validate_counts(document["counts"]))
    if errors:
        return errors

    # ── Empreintes : ce rapport parle-t-il bien de ces documents-là ? ─────────
    empreinte_plan = execution.plan_fingerprint(plan)
    empreinte_journal = execution.plan_fingerprint(execution_document)
    if document.get("plan_fingerprint") != empreinte_plan:
        errors.append(
            f"plan_fingerprint annonce {document.get('plan_fingerprint')!r}, alors que le "
            f"plan embarqué a pour empreinte {empreinte_plan}"
        )
    if document.get("execution_fingerprint") != empreinte_journal:
        errors.append(
            f"execution_fingerprint annonce {document.get('execution_fingerprint')!r}, alors "
            f"que le journal embarqué a pour empreinte {empreinte_journal}"
        )
    if execution_document.get("plan_fingerprint") != empreinte_plan:
        errors.append(
            "le journal embarqué se rapporte au plan "
            f"{execution_document.get('plan_fingerprint')!r}, pas au plan embarqué "
            f"({empreinte_plan})"
        )
    if document.get("plan_generated_at") != plan.get("generated_at"):
        errors.append(
            f"plan_generated_at annonce {document.get('plan_generated_at')!r}, le plan "
            f"embarqué annonce {plan.get('generated_at')!r}"
        )
    if plan.get("applicable") is not True:
        errors.append(
            "le plan embarqué n'est pas applicable : aucun rapport d'installation ne "
            "peut s'y adosser"
        )

    # ── Mode et application ──────────────────────────────────────────────────
    mode = execution_document.get("mode")
    applied = mode == execution.ExecutionMode.APPLY.value
    if document.get("mode") != mode:
        errors.append(
            f"mode annonce {document.get('mode')!r}, le journal embarqué annonce {mode!r}"
        )
    if document.get("applied") is not applied:
        errors.append(
            f"applied annonce {document.get('applied')!r} alors que le journal est en "
            f"mode {mode!r}"
        )

    # ── Conditions, index et compteurs, tous reconstruits ────────────────────
    results = execution_document.get("results") or []
    attendues = _evaluate_conditions(results, applied=applied)
    errors.extend(_compare_dicts(
        document["conditions"],
        [c.to_dict() for c in attendues],
        "conditions",
    ))

    claims = _evidence_claims(plan)
    errors.extend(_compare_dicts(
        document["hypotheses"],
        [c.to_dict() for c in claims if not c.verified],
        "hypotheses",
    ))
    for champ, motif in (
        ("licenses", _LICENSE_KEY_RE),
        ("versions", _VERSION_KEY_RE),
        ("fingerprints", _FINGERPRINT_KEY_RE),
    ):
        errors.extend(_compare_dicts(
            document[champ],
            [f.to_dict() for f in _index_fields(plan, motif)],
            champ,
        ))

    attendue_perf = _performance_from_results(results)
    performance = document["performance"]
    errors.extend(_compare_dicts(
        performance.get("steps") if isinstance(performance.get("steps"), list) else [],
        [p.to_dict() for p in attendue_perf],
        "performance.steps",
    ))
    duree = sum(
        r.get("duration_ms", 0) for r in results
        if isinstance(r, dict) and isinstance(r.get("duration_ms"), int)
        and not isinstance(r.get("duration_ms"), bool)
    )
    if performance.get("total_duration_ms") != duree:
        errors.append(
            f"performance.total_duration_ms annonce "
            f"{performance.get('total_duration_ms')!r}, attendu {duree} d'après le journal"
        )

    statuses = [r.get("status") for r in results if isinstance(r, dict)]
    counts = {
        "conditions_total": len(attendues),
        "conditions_satisfied": sum(1 for o in attendues if o.status == CONDITION_SATISFIED),
        "conditions_unsatisfied": sum(1 for o in attendues if o.status == CONDITION_UNSATISFIED),
        "conditions_unproven": sum(1 for o in attendues if o.status == CONDITION_UNPROVEN),
        "evidence_claims": len(claims),
        "hypotheses": sum(1 for c in claims if not c.verified),
        "licenses": len(_index_fields(plan, _LICENSE_KEY_RE)),
        "versions": len(_index_fields(plan, _VERSION_KEY_RE)),
        "fingerprints": len(_index_fields(plan, _FINGERPRINT_KEY_RE)),
        "steps_total": len(results),
        "steps_applied": sum(1 for s in statuses if s in execution.APPLIED_STATUSES),
        "failures": sum(1 for s in statuses if s == execution.STEP_FAILED),
    }
    if document["counts"] != counts:
        errors.append(f"counts annonce {document['counts']!r}, attendu {counts!r}")

    # ── Verdict : recalculé indépendamment, comme le fait `execution` ────────
    if execution.STEP_FAILED in statuses:
        attendu_verdict = VERDICT_FAILED
    elif any(o.status != CONDITION_SATISFIED for o in attendues):
        attendu_verdict = VERDICT_PARTIAL
    elif not applied:
        attendu_verdict = VERDICT_PARTIAL
    elif set(statuses) & {
        execution.STEP_NOT_ATTEMPTED, execution.STEP_WOULD_APPLY, execution.STEP_SKIPPED,
    }:
        attendu_verdict = VERDICT_PARTIAL
    else:
        attendu_verdict = VERDICT_COMPLETE

    if document["verdict"] != attendu_verdict:
        errors.append(
            f"verdict annonce {document['verdict']!r} alors que le plan et le journal "
            f"donnent {attendu_verdict!r}"
        )
    if document["exit_code"] != _VERDICT_EXIT[attendu_verdict]:
        errors.append(
            f"exit_code annonce {document['exit_code']!r}, attendu "
            f"{_VERDICT_EXIT[attendu_verdict]} pour le verdict {attendu_verdict!r}"
        )

    return errors


def _compare_dicts(declares: Any, attendus: list[dict], champ: str) -> list[str]:
    """
    Compare INTÉGRALEMENT une liste récapitulative à sa reconstruction.

    Même leçon que `schema._compare_findings` et `execution._compare_failures` :
    comparer les longueurs laisse passer une liste entièrement inventée de la
    bonne taille — et c'est précisément la liste qu'un lecteur pressé parcourt en
    diagonale six mois plus tard.
    """
    if not isinstance(declares, list):
        return [f"{champ} doit être une liste, reçu {type(declares).__name__}"]
    if len(declares) != len(attendus):
        return [
            f"{champ} en contient {len(declares)}, attendu {len(attendus)} d'après les "
            "documents embarqués"
        ]
    erreurs: list[str] = []
    for index, (declare, attendu) in enumerate(zip(declares, attendus)):
        if not isinstance(declare, dict):
            erreurs.append(f"{champ}[{index}] doit être un objet, reçu {type(declare).__name__}")
            continue
        for cle in attendu:
            if declare.get(cle, _ABSENT) != attendu[cle]:
                erreurs.append(
                    f"{champ}[{index}].{cle} annonce {declare.get(cle, None)!r}, "
                    f"attendu {attendu[cle]!r} d'après les documents embarqués"
                )
        for cle in declare:
            if cle not in attendu:
                erreurs.append(f"{champ}[{index}] porte une clé inconnue : {cle!r}")
    return erreurs


def assert_valid_install_document(document: Any) -> None:
    """Lève `InstallReportError` si le rapport est incohérent. Fail-closed."""
    errors = validate_install_document(document)
    if errors:
        raise InstallReportError(
            "rapport d'installation incohérent : " + " ; ".join(errors)
        )


# ── Rendu ─────────────────────────────────────────────────────────────────────

def render_install_json(report: InstallReport) -> str:
    """
    Rend le rapport en JSON, après contrôle de non-divulgation ET de cohérence.

    Les deux contrôles sont faits AU RENDU, et pas seulement à l'assemblage :
    c'est le rendu qui publie. Un rapport qui expose un token ou dont les
    conditions ne correspondent pas à son journal n'est pas publié du tout — il
    ne sort pas « avec un avertissement ».
    """
    document = report.to_dict()
    schema.assert_no_secrets(document)
    assert_valid_install_document(document)
    return json.dumps(document, indent=2, ensure_ascii=False, sort_keys=False)


_VERDICT_LABEL: dict[str, str] = {
    VERDICT_COMPLETE: "INSTALLATION COMPLÈTE",
    VERDICT_PARTIAL: "INSTALLATION PARTIELLE — ne pas la présenter comme terminée",
    VERDICT_FAILED: "INSTALLATION EN ÉCHEC",
}

_MODE_LABEL: dict[str, str] = {
    execution.ExecutionMode.DRY_RUN.value: "SIMULATION — rien n'a été appliqué",
    execution.ExecutionMode.APPLY.value: "APPLICATION RÉELLE",
}

_CONDITION_MARK: dict[str, str] = {
    CONDITION_SATISFIED: "[ok]",
    CONDITION_UNSATISFIED: "[XX]",
    CONDITION_UNPROVEN: "[??]",
}

_SECTION_LABEL: dict[str, str] = {
    schema.SECTION_HARDWARE: "Inventaire matériel",
    schema.SECTION_RUNTIME: "Runtime llama-server",
    schema.SECTION_RECOMMENDATION: "Recommandation",
    schema.SECTION_CATALOG: "Catalogue approuvé",
    schema.SECTION_MODELS: "Modèles retenus",
}

_SECTION_MARK: dict[str, str] = {"ok": "[ok]", "warn": "[!!]", "fail": "[XX]", "skip": "[--]"}


def render_install_human(report: InstallReport) -> str:
    """
    Rend le rapport en français, dans l'ordre où un lecteur en a besoin.

    Le verdict figure en tête ET en pied, et ce qui manque est énoncé
    immédiatement après l'en-tête : un rapport d'installation partielle doit se
    lire comme tel au premier coup d'œil, pas se terminer par un vert rassurant
    après trois écrans de détail.

    Pas de balisage `rich` : ce texte finit dans un ticket ou un journal systemd,
    où les séquences de couleur ne sont que du bruit — et où elles
    fragmenteraient un secret au point de le rendre indétectable par une
    recherche de sous-chaîne (leçon de la vague 5).
    """
    document = report.to_dict()
    schema.assert_no_secrets(document)
    assert_valid_install_document(document)

    counts = report.counts()
    verdict = report.verdict()
    lines: list[str] = [
        "RAPPORT D'INSTALLATION EVARUNTIME",
        f"  Généré le     : {report.generated_at}",
        f"  Verdict       : {_VERDICT_LABEL.get(verdict, verdict.upper())}",
        f"  Mode          : {_MODE_LABEL.get(report.execution.mode.value, report.execution.mode.value)}",
        f"  Schéma        : v{report.schema_version}",
        f"  Plan          : {report.execution.plan_generated_at}",
        f"                  {report.plan_fingerprint}",
        f"  Exécution     : {report.execution.started_at} → {report.execution.finished_at}",
        f"                  {report.execution_fingerprint}",
        "",
        "Ce document décrit ce qui a été fait sur cet hôte, et ce qui ne l'a pas été.",
        "Il n'a sondé aucun service : il n'agrège que le plan et le journal ci-dessous.",
    ]

    tenues = f"{counts['conditions_satisfied']}/{counts['conditions_total']}"
    lines.extend(["", f"CONDITIONS DE SORTIE DU JALON M2 ({tenues})"])
    for outcome in report.conditions():
        lines.append(f"  {_CONDITION_MARK.get(outcome.status, '[??]')} {outcome.label}")
        lines.append(f"      {outcome.proof}")

    manquantes = report.unmet_conditions()
    if manquantes:
        lines.extend([
            "",
            "CE QUI RESTE À FAIRE — l'installation n'est PAS terminée",
        ])
        for outcome in manquantes:
            lines.append(f"  · {outcome.label} : {outcome.proof}")

    lines.extend(["", "ÉTAT DU SYSTÈME D'APRÈS LE PLAN"])
    sections = report.plan.get("sections") or []
    if not sections:
        lines.append("  (aucune)")
    for section in sections:
        if not isinstance(section, dict):
            continue
        name = str(section.get("name"))
        label = _SECTION_LABEL.get(name, name)
        mark = _SECTION_MARK.get(str(section.get("status")), "[??]")
        lines.append(f"  {mark} {label} — {section.get('summary')}")
        for note in section.get("notes") or []:
            for note_line in str(note).splitlines():
                lines.append(f"       {note_line}" if note_line else "")

    lines.extend(["", "DÉCISIONS ET LEUR JUSTIFICATION"])
    decisions = report.plan.get("decisions") or []
    if not decisions:
        lines.append("  (aucune)")
    for decision in decisions:
        if not isinstance(decision, dict):
            continue
        lines.append(f"  · {decision.get('topic')} → {decision.get('choice')}")
        lines.append(f"      parce que {decision.get('rationale')}")
        rejected = decision.get("rejected") or []
        if rejected:
            lines.append(f"      écarté : {', '.join(str(r) for r in rejected)}")

    lines.extend(_render_index("LICENCES", report.licenses()))
    lines.extend(_render_index("VERSIONS", report.versions()))
    lines.extend(_render_index("EMPREINTES ET RÉVISIONS", report.fingerprints()))

    hypotheses = report.hypotheses()
    lines.extend([
        "",
        "HYPOTHÈSES NON VÉRIFIÉES — à ne PAS citer comme des faits établis",
    ])
    if not hypotheses:
        lines.append("  (aucune) — toute affirmation du plan porte un niveau de preuve reconnu.")
    for claim in hypotheses:
        lines.append(f"  · [{claim.marker}] {claim.path}")
        if claim.note:
            lines.append(f"      {claim.note}")

    lines.extend(["", "PERFORMANCES MESURÉES"])
    performances = report.performance()
    if not performances:
        lines.append("  (aucune) — aucune étape n'a rapporté de durée ni de mesure.")
    for perf in performances:
        mesures = ", ".join(f"{k} = {v}" for k, v in sorted(perf.metrics.items()))
        suffixe = f"  ({mesures})" if mesures else ""
        lines.append(
            f"  {perf.order:>2}. [{perf.action}] {perf.target} — {perf.duration_ms} ms{suffixe}"
        )
    lines.append(f"  Durée totale des étapes : {report.total_duration_ms()} ms")

    failures = report.execution.failures()
    if failures:
        lines.extend(["", "ÉCHECS DE L'EXÉCUTION"])
        for failure in failures:
            lines.append(f"  {failure.order:>2}. [{failure.action}] {failure.target}")
            lines.append(f"      {failure.error}")

    lines.extend([
        "",
        "COMPTEURS",
        "  " + ", ".join(f"{key} : {counts[key]}" for key in _COUNT_KEYS),
        "",
        f"Verdict : {_VERDICT_LABEL.get(verdict, verdict.upper())}",
        f"Sortie : {report.exit_code()}",
    ])
    return "\n".join(lines)


def _render_index(title: str, refs: Sequence[FieldRef]) -> list[str]:
    lines = ["", title]
    if not refs:
        lines.append("  (aucune) — le plan ne porte aucun champ de ce type.")
    for ref in refs:
        lines.append(f"  · {ref.path} = {ref.value}")
    return lines


# ── Persistance ───────────────────────────────────────────────────────────────

FORMAT_JSON = "json"
FORMAT_TEXT = "text"

_RENDERERS: dict[str, Callable[[InstallReport], str]] = {
    FORMAT_JSON: render_install_json,
    FORMAT_TEXT: render_install_human,
}

REPORT_FORMATS: tuple[str, ...] = (FORMAT_JSON, FORMAT_TEXT)


def write_install_report(
    report: InstallReport,
    path: Path | str,
    *,
    fmt: str = FORMAT_JSON,
) -> Path:
    """
    Écrit le rapport à l'emplacement FOURNI par l'appelant, de façon atomique.

    Produire le document et l'écrire sont deux choses distinctes : rien n'oblige
    à écrire, et cette fonction n'est jamais appelée par l'assemblage. Trois
    règles :

    - **l'emplacement n'est jamais deviné** : ni répertoire par défaut, ni
      création de l'arborescence. Un répertoire parent absent est une erreur, pas
      une invitation à choisir à la place de l'opérateur ;
    - **écriture atomique** : fichier temporaire dans le MÊME répertoire, `fsync`,
      puis `os.replace`. Un rapport à moitié écrit — parce que le disque est
      plein ou la machine coupée — ne doit jamais remplacer un rapport antérieur
      complet ;
    - **le contenu passe par le rendu**, donc par le contrôle de non-divulgation
      et par la validation. Aucune voie d'écriture ne les contourne.
    """
    if fmt not in _RENDERERS:
        raise InstallReportError(
            f"format inconnu : {fmt!r} (attendu parmi {list(REPORT_FORMATS)})"
        )

    target = Path(path)
    parent = target.parent
    if not parent.is_dir():
        raise InstallReportError(
            f"{parent} n'est pas un répertoire existant : le rapport n'est pas écrit. "
            "L'emplacement d'archivage est une décision d'exploitation, il n'est pas "
            "créé à la volée."
        )

    content = _RENDERERS[fmt](report)

    temporaire = parent / f".{target.name}.{os.getpid()}.tmp"
    try:
        with open(temporaire, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporaire, target)
    except OSError:
        temporaire.unlink(missing_ok=True)
        raise
    return target
