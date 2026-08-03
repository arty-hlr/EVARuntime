"""
AUT-001 — schéma du plan de bootstrap : versionné, validé, sans secret, lisible.

Pourquoi un schéma explicite
---------------------------
Le plan est le seul artefact que l'opérateur lit AVANT qu'une machine ne soit
modifiée. Il doit donc tenir quatre promesses, et chacune est vérifiable ici :

1. **versionné** — `schema_version` est un entier stable ; tout changement non
   rétro-compatible l'incrémente. Les scripts de déploiement s'appuient dessus,
   exactement comme sur le `SCHEMA_VERSION` de `doctor.py` ;
2. **validé** — `validate_plan_dict()` rejette un document mal formé avant qu'un
   consommateur ne s'en serve. Un plan invalide n'est jamais « appliqué au mieux » ;
3. **sans secret** — `find_secret_leaks()` inspecte le document rendu, noms de
   champs ET valeurs. Un plan est destiné à être copié dans un ticket ou un
   dépôt : un token Hugging Face qui y passe est un token compromis ;
4. **lisible avant application** — `render_human()` produit la même information
   que le JSON, en français, avec l'ordre des étapes et ce qu'elles changent.

Ce que ce module n'est pas
--------------------------
Il n'exécute rien, ne détecte rien et ne parle à aucun service. Il ne connaît
pas non plus les producteurs (`inventory`, `runtime_resolver`, `catalog`…) :
ceux-ci se projettent vers lui, jamais l'inverse. C'est cette absence de
dépendance descendante qui permet à un producteur d'échouer — GPU illisible,
catalogue absent — sans empêcher le plan d'exister et d'expliquer pourquoi.

Contrat pour un producteur
--------------------------
Un module producteur expose :

```python
SECTION_NAME = "hardware"     # unique, présent dans SECTION_NAMES
SECTION_VERSION = 1           # sa propre version, indépendante du plan

def to_plan_section(...) -> PlanSection: ...
```

`PlanSection.data` doit être **sérialisable JSON et sans secret**. Un producteur
qui lit un token le remplace par un booléen (`"token_present": true`) — jamais
par sa valeur, même tronquée.

Statuts et conséquences
-----------------------
| Statut | Sens                          | Effet sur le plan                    |
|--------|-------------------------------|--------------------------------------|
| `ok`   | Section complète et exploitable | aucun                                |
| `warn` | Dégradée mais utilisable       | plan applicable, dette signalée      |
| `fail` | Inexploitable                  | plan **non applicable** (bloqueur)   |
| `skip` | Non applicable ici             | aucun — n'est pas un échec           |

Exit codes — alignés sur `doctor.py`, volontairement
----------------------------------------------------
| Code | Signification                                                    |
|------|------------------------------------------------------------------|
| 0    | Plan complet et applicable.                                      |
| 1    | Au moins un bloqueur — ne rien appliquer.                        |
| 2    | RÉSERVÉ : erreur d'usage de la CLI (convention Typer/Click).      |
| 3    | Avertissements seulement — applicable, dette à traiter.          |
| 4    | Erreur d'exécution du planificateur lui-même.                    |
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal

# Version du document produit. À incrémenter à tout changement non
# rétro-compatible : les scripts de déploiement et les tests s'y adossent.
PLAN_SCHEMA_VERSION = 1

# Nom de l'outil, présent dans le document pour qu'un JSON isolé reste
# identifiable sans son contexte d'appel.
PLAN_TOOL_NAME = "eva-bootstrap-plan"

EXIT_OK = 0
EXIT_BLOCKED = 1
EXIT_USAGE = 2
EXIT_WARNINGS = 3
EXIT_ERROR = 4

SectionStatus = Literal["ok", "warn", "fail", "skip"]
FindingLevel = Literal["info", "warn", "fail"]

_STATUSES: frozenset[str] = frozenset({"ok", "warn", "fail", "skip"})
_LEVELS: frozenset[str] = frozenset({"info", "warn", "fail"})

# Niveaux de preuve partagés par les producteurs du plan et son rapport final.
# Les centraliser dans le contrat évite qu'un nouveau constat devienne une
# hypothèse simplement parce qu'un consommateur maintient sa propre liste.
EVIDENCE_SPEC = "constat-§6"
EVIDENCE_ASSUMPTION = "hypothèse-à-confirmer"
EVIDENCE_OPERATOR = "constat-opérateur"

# Sections attendues d'un plan complet. Une section absente n'est pas une erreur
# de structure — le plan la déclare `skip` avec sa raison — mais le nom, lui,
# doit appartenir à cet ensemble : c'est ce qui empêche deux producteurs de
# publier la même donnée sous deux noms différents.
SECTION_HARDWARE = "hardware"
SECTION_RUNTIME = "runtime"
SECTION_RECOMMENDATION = "recommendation"
SECTION_CATALOG = "catalog"
SECTION_MODELS = "models"

SECTION_NAMES: tuple[str, ...] = (
    SECTION_HARDWARE,
    SECTION_RUNTIME,
    SECTION_RECOMMENDATION,
    SECTION_CATALOG,
    SECTION_MODELS,
)

# Actions qu'une application du plan pourrait exécuter. Fermer cet ensemble est
# délibéré : un plan ne doit pas pouvoir décrire une action que l'applicateur ne
# sait pas exécuter, ni la faire passer pour légitime en la nommant librement.
ACTION_INSTALL_RUNTIME = "install_runtime"
ACTION_DOWNLOAD_MODEL = "download_model"
ACTION_VERIFY_ARTIFACT = "verify_artifact"
ACTION_WRITE_REGISTRY = "write_registry"
ACTION_CALIBRATE_MODEL = "calibrate_model"
ACTION_ENABLE_MODEL = "enable_model"
ACTION_WARMUP_MODEL = "warmup_model"
ACTION_SMOKE_TEST = "smoke_test"
ACTION_ACCEPT_LICENSE = "accept_license"

PLAN_ACTIONS: frozenset[str] = frozenset({
    ACTION_INSTALL_RUNTIME,
    ACTION_DOWNLOAD_MODEL,
    ACTION_VERIFY_ARTIFACT,
    ACTION_WRITE_REGISTRY,
    ACTION_CALIBRATE_MODEL,
    ACTION_ENABLE_MODEL,
    ACTION_WARMUP_MODEL,
    ACTION_SMOKE_TEST,
    ACTION_ACCEPT_LICENSE,
})

# ── Détection de fuite de secret ──────────────────────────────────────────────
#
# Deux filets indépendants, parce qu'aucun des deux ne suffit seul : le premier
# attrape un champ mal nommé dont la valeur est anodine en test, le second
# attrape une valeur sensible rangée sous un nom innocent.

_SECRET_KEY_RE = re.compile(
    r"(SECRET|PASSWORD|PASSWD|CREDENTIAL|API_?KEY|PRIVATE_?KEY|_KEY$|^KEY$)",
    re.IGNORECASE,
)

# `token` a besoin d'un filet à lui, et ancré. Dans une passerelle LLM, un jeton
# est d'abord une unité de facturation : §9 exige de publier
# `prompt_tokens_per_second`, §11 des compteurs de jetons, §10 un temps jusqu'au
# premier jeton. Le motif non ancré d'origine frappait `completion_tokens`,
# `max_tokens` et `tokens_per_second` en sous-chaîne, si bien que le rapport de
# §9 était littéralement impubliable : `render_*()` refusait de le rendre.
# Défaut trouvé indépendamment par AUT-007, AUT-008 et AUT-011 — trois chantiers
# qui ont chacun dû le contourner avant qu'on le corrige ici.
#
# L'ancrage sur les frontières de composant laisse passer le pluriel (un compte)
# et retient le singulier (un porteur d'authentification) : `access_token`,
# `hf_token`, `token_file` restent des fuites.
_TOKEN_KEY_RE = re.compile(r"(^|_)TOKEN(_|$)", re.IGNORECASE)

# Les mesures dont le nom porte « token » au singulier. Ce sont des durées et
# des comptages, pas des porteurs d'authentification. Cette liste est volontaire :
# un nom qu'elle ne prévoit pas bloque la publication au lieu de fuir — le mode
# de dégradation est bruyant, jamais silencieux.
_TOKEN_METRIC_RE = re.compile(
    r"(^|_)(FIRST_TOKEN|TOKEN_COUNT|TOKEN_USAGE|TOKEN_LATENCY)(_|$)",
    re.IGNORECASE,
)


def _is_sensitive_field_name(name: str) -> bool:
    """Le nom de champ annonce-t-il un secret ? Voir les trois motifs ci-dessus."""
    if _SECRET_KEY_RE.search(name):
        return True
    return bool(_TOKEN_KEY_RE.search(name)) and not _TOKEN_METRIC_RE.search(name)

# Champs dont le nom déclenche `_SECRET_KEY_RE` mais qui ne portent, par
# construction, aucune valeur : un booléen de présence est la façon RECOMMANDÉE
# de signaler un secret sans l'exposer.
_SECRET_KEY_ALLOWED_TYPES = (bool, type(None))

_SECRET_VALUE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("hf_token", re.compile(r"\bhf_[A-Za-z0-9]{16,}\b")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("gateway_key", re.compile(r"\bllmgw-[A-Za-z0-9]{16,}\b")),
    ("bearer_header", re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{16,}")),
    ("pem_private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("url_credentials", re.compile(r"://[^/\s:@]+:[^/\s@]+@")),
)


class PlanError(Exception):
    """Plan structurellement invalide ou impossible à assembler."""


@dataclass(frozen=True)
class Finding:
    """
    Un constat attaché à une section : ce que le producteur a vu, et son poids.

    `code` est stable et machine-lisible (`gpu_absent`, `license_gated`…) ;
    `message` est la phrase française destinée à l'opérateur. Les deux sont
    obligatoires : un code sans message n'est pas actionnable, un message sans
    code n'est pas testable.
    """
    code: str
    level: FindingLevel
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "level": self.level, "message": self.message}


@dataclass(frozen=True)
class PlanSection:
    """
    Contribution d'un producteur au plan.

    `notes` porte le texte qui doit être VU par l'opérateur sans être un constat :
    les limites d'un conseiller, ce qu'une estimation ignore, la raison d'une
    hypothèse. Sans ce champ, ces éléments ne vivaient que dans `data`, que le
    rendu humain n'imprime pas — ils n'existaient donc qu'en JSON. Besoin
    remonté par le chantier AUT-004, dont les huit limites de LLMfit sont
    précisément le cas d'usage.
    """
    name: str
    version: int
    status: SectionStatus
    summary: str
    data: dict[str, Any] = field(default_factory=dict)
    findings: tuple[Finding, ...] = ()
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "section_version": self.version,
            "status": self.status,
            "summary": self.summary,
            "data": self.data,
            "findings": [f.to_dict() for f in self.findings],
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class PlanStep:
    """
    Une action que l'application du plan exécuterait, dans l'ordre annoncé.

    `requires_root` et `reversible` ne sont pas décoratifs : ce sont les deux
    questions que se pose l'opérateur avant de valider, et les cacher revient à
    lui demander une signature à l'aveugle.
    """
    order: int
    action: str
    target: str
    detail: str
    requires_root: bool = False
    reversible: bool = True
    estimated_bytes: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "order": self.order,
            "action": self.action,
            "target": self.target,
            "detail": self.detail,
            "requires_root": self.requires_root,
            "reversible": self.reversible,
            "estimated_bytes": self.estimated_bytes,
        }


@dataclass(frozen=True)
class Decision:
    """
    Un choix du planificateur, avec sa raison et ce qu'il a écarté.

    C'est la condition de sortie du jalon M1 : « le système sait expliquer ce
    qu'il installera **et pourquoi** ». Une décision sans `rationale` ne remplit
    pas ce contrat, et la validation la rejette.
    """
    topic: str
    choice: str
    rationale: str
    rejected: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "choice": self.choice,
            "rationale": self.rationale,
            "rejected": list(self.rejected),
        }


@dataclass(frozen=True)
class BootstrapPlan:
    """
    Document complet. Immuable : un plan lu par l'opérateur ne change plus.

    `generated_at` est fourni par l'appelant (ISO-8601). Le module ne lit pas
    l'horloge lui-même, pour que le rendu soit reproductible en test.
    """
    generated_at: str
    mode: str
    sections: tuple[PlanSection, ...] = ()
    steps: tuple[PlanStep, ...] = ()
    decisions: tuple[Decision, ...] = ()
    schema_version: int = PLAN_SCHEMA_VERSION

    # ── Lecture d'état ────────────────────────────────────────────────────────

    def section(self, name: str) -> PlanSection | None:
        for section in self.sections:
            if section.name == name:
                return section
        return None

    @property
    def blockers(self) -> tuple[Finding, ...]:
        """
        Constats qui interdisent l'application, quelle que soit la section.

        Un constat de niveau `fail` bloque **indépendamment du statut de sa
        section**. La première écriture ne les collectait que dans les sections
        déjà `fail` : un producteur qui aurait émis un `fail` dans une section
        `warn` — par distraction ou parce que son propre calcul de statut
        diverge — aurait vu son bloqueur disparaître en silence, et le plan
        serait sorti applicable. Piège signalé par le chantier AUT-003.
        """
        out: list[Finding] = []
        for section in self.sections:
            out.extend(f for f in section.findings if f.level == "fail")
            if section.status == "fail" and not any(f.level == "fail" for f in section.findings):
                # Une section en échec sans constat `fail` explicite reste un
                # bloqueur : on le matérialise plutôt que de le perdre.
                out.append(Finding(
                    code=f"{section.name}_failed",
                    level="fail",
                    message=section.summary or f"Section « {section.name} » en échec.",
                ))
        return tuple(out)

    @property
    def warnings(self) -> tuple[Finding, ...]:
        out: list[Finding] = []
        for section in self.sections:
            out.extend(f for f in section.findings if f.level == "warn")
        return tuple(out)

    def counts(self, *, strict: bool = False) -> dict[str, int]:
        counts = {"ok": 0, "warn": 0, "fail": 0, "skip": 0}
        for section in self.sections:
            counts[section.status] = counts.get(section.status, 0) + 1
        counts["steps"] = len(self.effective_steps(strict=strict))
        counts["decisions"] = len(self.decisions)
        return counts

    def status(self, *, strict: bool = False) -> str:
        if self.blockers:
            return "blocked"
        if self.warnings:
            return "fail" if strict else "warn"
        return "ok"

    def is_blocked(self, *, strict: bool = False) -> bool:
        """
        Le plan interdit-il l'application ? `strict` fait partie de la réponse.

        `--strict` signifie « traitez les avertissements comme bloquants ». Le
        prendre en compte pour le code de sortie mais pas pour `applicable` ni
        pour les étapes produisait un document qui se contredisait lui-même :
        code 1, statut `fail`, et pourtant `applicable: true` avec neuf actions à
        exécuter. Un seul lieu décide désormais, et les trois champs en dérivent.
        """
        return bool(self.blockers) or (strict and bool(self.warnings))

    @property
    def applicable(self) -> bool:
        """Un plan bloqué ne doit être appliqué par personne, jamais partiellement."""
        return not self.is_blocked()

    def effective_steps(self, *, strict: bool = False) -> tuple[PlanStep, ...]:
        """
        Étapes réellement proposées. Vide dès que le plan est bloqué.

        Les étapes restent portées par l'objet — le planificateur les a bien
        calculées — mais elles ne sont ni publiées ni rendues tant que le verdict
        interdit l'application. C'est la même règle que celle du planificateur,
        appliquée une seconde fois et à un autre niveau : ni un producteur, ni un
        assembleur, ni un mode d'affichage ne peut la contourner seul.
        """
        return () if self.is_blocked(strict=strict) else self.steps

    def exit_code(self, *, strict: bool = False) -> int:
        state = self.status(strict=strict)
        if state in ("blocked", "fail"):
            return EXIT_BLOCKED
        if state == "warn":
            return EXIT_WARNINGS
        return EXIT_OK

    def total_download_bytes(self, *, strict: bool = False) -> int:
        return sum(s.estimated_bytes or 0 for s in self.effective_steps(strict=strict))

    # ── Sérialisation ─────────────────────────────────────────────────────────

    def to_dict(self, *, strict: bool = False) -> dict[str, Any]:
        return {
            "tool": PLAN_TOOL_NAME,
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "mode": self.mode,
            "strict": bool(strict),
            "status": self.status(strict=strict),
            "applicable": not self.is_blocked(strict=strict),
            "exit_code": self.exit_code(strict=strict),
            "counts": self.counts(strict=strict),
            "estimated_download_bytes": self.total_download_bytes(strict=strict),
            "sections": [s.to_dict() for s in self.sections],
            "steps": [s.to_dict() for s in self.effective_steps(strict=strict)],
            "decisions": [d.to_dict() for d in self.decisions],
            "blockers": [f.to_dict() for f in self.blockers],
            "warnings": [f.to_dict() for f in self.warnings],
        }


# ── Validation structurelle ───────────────────────────────────────────────────

def validate_plan_dict(document: Any) -> tuple[str, ...]:
    """
    Contrôle la structure d'un plan sérialisé. Retourne les erreurs, vide si sain.

    Volontairement écrit à la main plutôt qu'adossé à pydantic : ce contrat est
    lu par des scripts shell et par un opérateur, il doit rester descriptible en
    une page, et ses messages d'erreur doivent désigner le champ fautif par son
    chemin (`sections[1].status`) et non par une trace de bibliothèque.
    """
    errors: list[str] = []

    if not isinstance(document, dict):
        return (f"le plan doit être un objet JSON, reçu {type(document).__name__}",)

    if document.get("tool") != PLAN_TOOL_NAME:
        errors.append(f"tool doit valoir « {PLAN_TOOL_NAME} », reçu {document.get('tool')!r}")

    version = document.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        errors.append(f"schema_version doit être un entier, reçu {version!r}")
    elif version > PLAN_SCHEMA_VERSION:
        errors.append(
            f"schema_version {version} est plus récent que {PLAN_SCHEMA_VERSION} "
            "— mettez à jour eva-bootstrap avant de lire ce plan"
        )

    for key in ("generated_at", "mode", "status"):
        if not isinstance(document.get(key), str) or not document.get(key):
            errors.append(f"{key} doit être une chaîne non vide")

    errors.extend(_validate_sections(document.get("sections")))
    errors.extend(_validate_steps(document.get("steps")))
    errors.extend(_validate_decisions(document.get("decisions")))

    # Le verdict n'est contrôlé que si la structure tient : recalculer un verdict
    # à partir de sections mal formées produirait du bruit, pas un diagnostic.
    if not errors:
        errors.extend(_validate_verdict(document))

    return tuple(errors)


_ABSENT = object()
_COUNT_KEYS = ("ok", "warn", "fail", "skip", "steps", "decisions")


def _validate_counts(counts: dict) -> list[str]:
    """
    Valide le sous-contrat `counts`, clé par clé et sans coercition numérique.

    Une simple comparaison au dictionnaire recalculé ne suffit pas en Python :
    `True == 1`, `False == 0` et `1.0 == 1`. Un document JSON rempli de
    booléens ou de flottants pouvait donc respecter l'égalité tout en violant le
    schéma. Les compteurs sont des entiers positifs ou nuls, jamais des valeurs
    seulement « numériquement équivalentes ».
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


def _compare_findings(declares: list, attendus: list, champ: str) -> list[str]:
    """
    Compare INTÉGRALEMENT une liste récapitulative aux constats recalculés.

    Comparer les longueurs ne suffisait pas : une liste `warnings` entièrement
    inventée mais de même taille passait sans un mot. Or ces listes sont
    précisément ce qu'un opérateur lit en diagonale pour décider d'appliquer.
    """
    if len(declares) != len(attendus):
        return [
            f"{champ} en contient {len(declares)}, attendu {len(attendus)} d'après les sections"
        ]
    erreurs: list[str] = []
    for index, (declare, attendu) in enumerate(zip(declares, attendus)):
        if not isinstance(declare, dict):
            erreurs.append(f"{champ}[{index}] doit être un objet, reçu {type(declare).__name__}")
            continue
        for cle in ("code", "level", "message"):
            if declare.get(cle) != attendu.get(cle):
                erreurs.append(
                    f"{champ}[{index}].{cle} annonce {declare.get(cle)!r}, "
                    f"attendu {attendu.get(cle)!r} d'après les sections"
                )
    return erreurs


def _validate_verdict(document: dict) -> list[str]:
    """
    Recalcule le verdict depuis les sections et le confronte à celui du document.

    Sans ce contrôle, un plan enregistré puis retouché — `status: ok`,
    `applicable: true`, `exit_code: 0`, `blockers: []` — passait la validation
    alors qu'une de ses sections était en échec. Ce n'est pas une hypothèse : un
    document falsifié à la main était accepté sans une erreur.

    L'enjeu grandit avec M2, qui appliquera des plans **relus depuis un fichier**
    et non plus seulement calculés en mémoire. Un applicateur ne doit jamais
    pouvoir être convaincu d'agir par un champ dérivé que personne ne recoupe.
    """
    errors: list[str] = []

    # Types d'abord. Sans cela, `warnings: 7` faisait lever un TypeError au lieu
    # de produire un diagnostic — un validateur qui plante n'est pas un validateur.
    for champ, attendu, libelle in (
        ("applicable", bool, "un booléen"),
        ("strict", bool, "un booléen"),
        ("exit_code", int, "un entier"),
        ("estimated_download_bytes", int, "un entier"),
        ("counts", dict, "un objet"),
        ("blockers", list, "une liste"),
        ("warnings", list, "une liste"),
    ):
        valeur = document.get(champ, _ABSENT)
        if valeur is _ABSENT:
            errors.append(f"{champ} est obligatoire")
        elif not isinstance(valeur, attendu) or (attendu is int and isinstance(valeur, bool)):
            # `strict: "false"` était converti en vrai par `bool(...)` : une chaîne
            # non vide est toujours vraie. Le type est donc exigé, pas déduit.
            errors.append(f"{champ} doit être {libelle}, reçu {type(valeur).__name__}")
    if isinstance(document.get("counts"), dict):
        errors.extend(_validate_counts(document["counts"]))
    if errors:
        return errors

    sections = document.get("sections", [])
    steps = document.get("steps", [])
    strict = document["strict"]

    blockers: list[dict] = []
    warnings: list[dict] = []
    counts = {"ok": 0, "warn": 0, "fail": 0, "skip": 0}

    for section in sections:
        status = section.get("status")
        counts[status] = counts.get(status, 0) + 1
        findings = section.get("findings", [])
        fails = [f for f in findings if f.get("level") == "fail"]
        blockers.extend(fails)
        warnings.extend(f for f in findings if f.get("level") == "warn")
        if status == "fail" and not fails:
            blockers.append({
                "code": f"{section.get('name')}_failed",
                "level": "fail",
                "message": section.get("summary") or f"Section « {section.get('name')} » en échec.",
            })

    counts["steps"] = len(steps)
    counts["decisions"] = len(document.get("decisions", []))

    # `strict` fait partie du verdict, pas seulement de l'affichage : il promeut
    # les avertissements en blocage, et ce blocage doit valoir pour `applicable`
    # et pour les étapes comme il vaut pour le code de sortie.
    bloque = bool(blockers) or (strict and bool(warnings))
    if blockers:
        expected_status = "blocked"
    elif warnings:
        expected_status = "fail" if strict else "warn"
    else:
        expected_status = "ok"
    expected_exit = (
        EXIT_BLOCKED if expected_status in ("blocked", "fail")
        else EXIT_WARNINGS if expected_status == "warn"
        else EXIT_OK
    )

    if document.get("status") != expected_status:
        errors.append(
            f"status annonce {document.get('status')!r} alors que les sections donnent "
            f"{expected_status!r}"
        )
    if document["applicable"] is not (not bloque):
        errors.append(
            f"applicable annonce {document['applicable']!r} alors que le verdict "
            f"{'interdit' if bloque else 'autorise'} l'application"
        )
    if document["exit_code"] != expected_exit:
        errors.append(f"exit_code annonce {document['exit_code']!r}, attendu {expected_exit}")

    errors.extend(_compare_findings(document["blockers"], blockers, "blockers"))
    errors.extend(_compare_findings(document["warnings"], warnings, "warnings"))

    if document["counts"] != counts:
        errors.append(f"counts annonce {document['counts']!r}, attendu {counts!r}")

    expected_bytes = sum(s.get("estimated_bytes") or 0 for s in steps)
    if document["estimated_download_bytes"] != expected_bytes:
        errors.append(
            f"estimated_download_bytes annonce {document['estimated_download_bytes']!r}, "
            f"attendu {expected_bytes} d'après les étapes"
        )

    # L'invariant le plus lourd de conséquence : un plan qui sort en code 1 ne
    # doit décrire aucune action. Le critère est le STATUT, pas la seule présence
    # de bloqueurs — sous `--strict`, un plan sans bloqueur mais avec des
    # avertissements sort en 1 lui aussi, et doit donc être vide d'étapes.
    if steps and (expected_status in ("blocked", "fail") or document.get("status") in ("blocked", "fail")):
        errors.append(
            f"le plan sort en statut {expected_status!r} (code {expected_exit}) mais décrit "
            f"{len(steps)} étape(s) : un plan non applicable ne propose aucune action"
        )

    return errors


def _validate_sections(sections: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(sections, list):
        return ["sections doit être une liste"]

    seen: set[str] = set()
    for index, section in enumerate(sections):
        path = f"sections[{index}]"
        if not isinstance(section, dict):
            errors.append(f"{path} doit être un objet")
            continue

        name = section.get("name")
        if name not in SECTION_NAMES:
            errors.append(f"{path}.name inconnu : {name!r} (attendu parmi {list(SECTION_NAMES)})")
        elif name in seen:
            errors.append(f"{path}.name en double : {name!r}")
        else:
            seen.add(name)

        if section.get("status") not in _STATUSES:
            errors.append(f"{path}.status invalide : {section.get('status')!r}")

        version = section.get("section_version")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            errors.append(f"{path}.section_version doit être un entier >= 1")

        if not isinstance(section.get("summary"), str) or not section.get("summary"):
            errors.append(f"{path}.summary doit être une chaîne non vide")

        if not isinstance(section.get("data"), dict):
            errors.append(f"{path}.data doit être un objet")

        notes = section.get("notes")
        if not isinstance(notes, list) or any(not isinstance(n, str) or not n for n in notes):
            errors.append(f"{path}.notes doit être une liste de chaînes non vides")

        errors.extend(_validate_findings(section.get("findings"), path))

    return errors


def _validate_findings(findings: Any, path: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(findings, list):
        return [f"{path}.findings doit être une liste"]
    for index, finding in enumerate(findings):
        sub = f"{path}.findings[{index}]"
        if not isinstance(finding, dict):
            errors.append(f"{sub} doit être un objet")
            continue
        if not isinstance(finding.get("code"), str) or not finding.get("code"):
            errors.append(f"{sub}.code doit être une chaîne non vide")
        if finding.get("level") not in _LEVELS:
            errors.append(f"{sub}.level invalide : {finding.get('level')!r}")
        if not isinstance(finding.get("message"), str) or not finding.get("message"):
            errors.append(f"{sub}.message doit être une chaîne non vide")
    return errors


def _validate_steps(steps: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(steps, list):
        return ["steps doit être une liste"]

    for index, step in enumerate(steps):
        path = f"steps[{index}]"
        if not isinstance(step, dict):
            errors.append(f"{path} doit être un objet")
            continue
        if step.get("action") not in PLAN_ACTIONS:
            errors.append(f"{path}.action inconnue : {step.get('action')!r}")
        for key in ("target", "detail"):
            if not isinstance(step.get(key), str) or not step.get(key):
                errors.append(f"{path}.{key} doit être une chaîne non vide")
        for key in ("requires_root", "reversible"):
            if not isinstance(step.get(key), bool):
                errors.append(f"{path}.{key} doit être un booléen")
        size = step.get("estimated_bytes")
        if size is not None and (not isinstance(size, int) or isinstance(size, bool) or size < 0):
            errors.append(f"{path}.estimated_bytes doit être un entier >= 0 ou null")
        order = step.get("order")
        if not isinstance(order, int) or isinstance(order, bool):
            errors.append(f"{path}.order doit être un entier")
        elif order != index + 1:
            # L'ordre est le seul contenu opérationnel d'un plan : un trou ou un
            # doublon signifie qu'une étape a été perdue à l'assemblage.
            errors.append(f"{path}.order vaut {order}, attendu {index + 1} (numérotation continue depuis 1)")

    return errors


def _validate_decisions(decisions: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(decisions, list):
        return ["decisions doit être une liste"]
    for index, decision in enumerate(decisions):
        path = f"decisions[{index}]"
        if not isinstance(decision, dict):
            errors.append(f"{path} doit être un objet")
            continue
        for key in ("topic", "choice", "rationale"):
            if not isinstance(decision.get(key), str) or not decision.get(key):
                errors.append(f"{path}.{key} doit être une chaîne non vide")
        if not isinstance(decision.get("rejected"), list):
            errors.append(f"{path}.rejected doit être une liste")
    return errors


# ── Non-divulgation ───────────────────────────────────────────────────────────

def find_secret_leaks(document: Any) -> tuple[str, ...]:
    """
    Cherche un secret dans un plan sérialisé. Retourne les chemins fautifs.

    Deux filets indépendants, chacun rattrapant ce que l'autre laisse passer :

    - **par nom de champ** : `hf_token`, `admin_secret`, `api_key`… Un tel champ
      n'a le droit de porter qu'un booléen ou `null` — la façon correcte de dire
      « un token est présent » sans le dire ;
    - **par forme de valeur** : un `hf_…`, un `sk-…`, un en-tête `Bearer`, un bloc
      PEM ou une URL `user:password@` rangés sous un nom anodin (`note`, `cmd`).

    Le message retourné cite le CHEMIN et le motif, jamais la valeur : un
    rapport de fuite qui recopie le secret est lui-même une fuite.
    """
    leaks: list[str] = []
    _walk_for_secrets(document, "", leaks)
    return tuple(leaks)


def _walk_for_secrets(node: Any, path: str, leaks: list[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}" if path else str(key)
            if _is_sensitive_field_name(str(key)) and not isinstance(value, _SECRET_KEY_ALLOWED_TYPES):
                leaks.append(
                    f"{child} : champ au nom sensible portant une valeur "
                    f"({type(value).__name__}) — n'exposer qu'un booléen de présence"
                )
            _walk_for_secrets(value, child, leaks)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _walk_for_secrets(value, f"{path}[{index}]", leaks)
    elif isinstance(node, str):
        for label, pattern in _SECRET_VALUE_PATTERNS:
            if pattern.search(node):
                leaks.append(f"{path or '<racine>'} : valeur ressemblant à un secret ({label})")


def assert_no_secrets(document: Any) -> None:
    """Lève `PlanError` si un secret transparaît. Fail-closed, par construction."""
    leaks = find_secret_leaks(document)
    if leaks:
        raise PlanError("le plan expose des valeurs sensibles : " + " ; ".join(leaks))


# ── Rendu ─────────────────────────────────────────────────────────────────────

def render_json(plan: BootstrapPlan, *, strict: bool = False) -> str:
    """Rend le plan en JSON, après contrôle de non-divulgation."""
    document = plan.to_dict(strict=strict)
    assert_no_secrets(document)
    return json.dumps(document, indent=2, ensure_ascii=False, sort_keys=False)


_STATUS_LABEL: dict[str, str] = {
    "ok": "APPLICABLE",
    "warn": "APPLICABLE AVEC RÉSERVES",
    "fail": "BLOQUÉ (mode strict)",
    "blocked": "BLOQUÉ",
}

_SECTION_LABEL: dict[str, str] = {
    SECTION_HARDWARE: "Inventaire matériel",
    SECTION_RUNTIME: "Runtime llama-server",
    SECTION_RECOMMENDATION: "Recommandation",
    SECTION_CATALOG: "Catalogue approuvé",
    SECTION_MODELS: "Modèles retenus",
}

_MARK: dict[str, str] = {"ok": "[ok]", "warn": "[!!]", "fail": "[XX]", "skip": "[--]"}


def render_human(plan: BootstrapPlan, *, strict: bool = False) -> str:
    """
    Rend le plan en français, pour relecture avant application.

    Même information que le JSON, même contrôle de non-divulgation. Pas de
    balisage `rich` : ce texte finit dans un journal systemd ou un ticket, où
    les séquences de couleur ne sont que du bruit.
    """
    document = plan.to_dict(strict=strict)
    assert_no_secrets(document)

    state = plan.status(strict=strict)
    lines: list[str] = [
        "PLAN DE BOOTSTRAP EVARUNTIME",
        f"  Généré le    : {plan.generated_at}",
        f"  Mode         : {plan.mode}",
        f"  Schéma       : v{plan.schema_version}",
        f"  Verdict      : {_STATUS_LABEL.get(state, state.upper())}",
        "",
        "Ce document décrit ce qui SERAIT fait. Rien n'a été installé, téléchargé",
        "ni modifié pour le produire.",
        "",
        "SECTIONS",
    ]

    if not plan.sections:
        lines.append("  (aucune)")
    for section in plan.sections:
        label = _SECTION_LABEL.get(section.name, section.name)
        lines.append(f"  {_MARK.get(section.status, '[??]')} {label} — {section.summary}")
        for finding in section.findings:
            if finding.level != "info":
                lines.append(f"       · {finding.level} [{finding.code}] {finding.message}")
        for note in section.notes:
            for note_line in note.splitlines():
                lines.append(f"       {note_line}" if note_line else "")

    lines.extend(["", "DÉCISIONS"])
    if not plan.decisions:
        lines.append("  (aucune)")
    for decision in plan.decisions:
        lines.append(f"  · {decision.topic} → {decision.choice}")
        lines.append(f"      parce que {decision.rationale}")
        if decision.rejected:
            lines.append(f"      écarté : {', '.join(decision.rejected)}")

    steps = plan.effective_steps(strict=strict)
    lines.extend(["", "ÉTAPES QUE L'APPLICATION EXÉCUTERAIT"])
    if not steps:
        lines.append("  (aucune)")
        if plan.steps:
            # Le planificateur en avait calculé : dire pourquoi elles sont retenues,
            # sinon l'opérateur croit à un plan vide et cherche au mauvais endroit.
            lines.append(
                f"  Le plan est bloqué : les {len(plan.steps)} étape(s) calculées ne sont pas"
            )
            lines.append(
                "  proposées. Levez les bloqueurs ci-dessous, puis relancez la commande."
            )
    for step in steps:
        flags = []
        if step.requires_root:
            flags.append("root")
        if not step.reversible:
            flags.append("IRRÉVERSIBLE")
        if step.estimated_bytes:
            flags.append(_human_bytes(step.estimated_bytes))
        suffix = f"  ({', '.join(flags)})" if flags else ""
        lines.append(f"  {step.order:>2}. [{step.action}] {step.target}{suffix}")
        lines.append(f"      {step.detail}")

    total = plan.total_download_bytes(strict=strict)
    if total:
        lines.extend(["", f"Volume à télécharger : {_human_bytes(total)}"])

    if plan.blockers:
        lines.extend(["", "BLOQUEURS — ne rien appliquer tant qu'ils tiennent"])
        for finding in plan.blockers:
            lines.append(f"  · [{finding.code}] {finding.message}")

    if plan.warnings:
        lines.extend(["", "AVERTISSEMENTS"])
        for finding in plan.warnings:
            lines.append(f"  · [{finding.code}] {finding.message}")

    lines.extend(["", f"Sortie : {plan.exit_code(strict=strict)}"])
    return "\n".join(lines)


def _human_bytes(size: int) -> str:
    value = float(size)
    for unit in ("o", "Kio", "Mio", "Gio"):
        if value < 1024 or unit == "Gio":
            return f"{value:.1f} {unit}" if unit != "o" else f"{int(value)} o"
        value /= 1024
    return f"{value:.1f} Gio"


def merge_findings(*groups: Iterable[Finding]) -> tuple[Finding, ...]:
    """Concatène des constats en préservant l'ordre et en dédoublonnant par code."""
    seen: set[str] = set()
    out: list[Finding] = []
    for group in groups:
        for finding in group:
            if finding.code in seen:
                continue
            seen.add(finding.code)
            out.append(finding)
    return tuple(out)


def status_from_findings(findings: Iterable[Finding], *, default: SectionStatus = "ok") -> SectionStatus:
    """
    Dérive le statut d'une section de ses constats. `fail` l'emporte sur `warn`.

    Sans cette fonction, chaque producteur redupliquait la table
    `{"info": "ok", "warn": "warn", "fail": "fail"}` dans son coin — trois
    l'avaient déjà fait. Or `worst_status()` combine des *statuts* de section,
    pas des *niveaux* de constat : les deux notions se ressemblent assez pour
    qu'on les confonde, et diffèrent assez pour que la confusion coûte cher.

    `default` permet à un producteur de partir de `skip` quand la section n'est
    pas applicable : un constat `info` ne doit pas transformer un `skip` en `ok`.
    """
    status: SectionStatus = default
    for finding in findings:
        if finding.level == "fail":
            return "fail"
        if finding.level == "warn":
            status = "warn"
    return status


def worst_status(*statuses: str) -> SectionStatus:
    """Statut le plus sévère d'un ensemble. `skip` ne dégrade jamais un `ok`."""
    order = {"skip": 0, "ok": 1, "warn": 2, "fail": 3}
    best = "skip"
    for status in statuses:
        if order.get(status, 0) > order[best]:
            best = status
    return best  # type: ignore[return-value]
