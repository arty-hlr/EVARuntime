"""
AUT-001 — assemblage du plan de bootstrap : le seul module qui connaît tous les producteurs.

Ce que fait ce module, et ce qu'il ne fait pas
---------------------------------------------
Il exécute le pipeline de §5 — inventaire → runtime → recommandation → catalogue
→ sélection → étapes — et rend un `schema.BootstrapPlan`. **Il n'applique rien.**
Aucun téléchargement, aucune compilation, aucune écriture de registre, aucun
`systemctl`. C'est la condition de sortie du jalon M1 : « le système sait
expliquer ce qu'il installera et pourquoi », pas « le système installe ».

Le seul sous-processus que la chaîne peut lancer est `llama-server --version` sur
un binaire déjà présent, et seulement si l'appelant en fournit le chemin.

Pourquoi le planificateur est le seul à tout importer
----------------------------------------------------
Les producteurs ne se connaissent pas (règle posée dans `bootstrap/__init__.py`).
C'est ce qui permet à l'un d'échouer sans entraîner les autres : un `nvidia-smi`
cassé dégrade la section `hardware`, il n'empêche pas le catalogue d'être lu ni
le plan d'exister pour expliquer où est le trou. Le prix de cette indépendance
est que quelqu'un doit faire les raccords — c'est ici, et nulle part ailleurs.

La règle d'activation, littéralement
------------------------------------
§7 la formule ainsi, et le code la respecte dans cet ordre :

    recommandation LLMfit          ← simple ordonnancement, jamais un droit
      + modèle approuvé par le catalogue EVARuntime   ← filtre dur
      + estimation conservatrice                      ← filtre dur
      + chargement réel de calibration                ← ÉTAPE du plan, pas ici
      = modèle activable

Les deux premiers termes sont des **filtres** : LLMfit ne peut qu'ordonner des
candidats déjà approuvés, jamais en ajouter un. Le quatrième n'est pas évaluable
au moment du plan — il devient une étape `calibrate_model` que l'application
devra franchir, et c'est pour cela que `write_registry` écrit une entrée
**désactivée** (AUT-007).

Ordre des étapes, et pourquoi celui-là
--------------------------------------
    accept_license → download_model → verify_artifact → write_registry (désactivé)
    → calibrate_model → enable_model (provisoire) → smoke_test
    → warmup_model

Trois inversions seraient des défauts, pas des goûts : vérifier après avoir posé
l'artefact ne protège de rien ; activer avant d'avoir calibré publie une capacité
supposée ; préchauffer avant la recette conserverait en mémoire un modèle dont
aucun token n'a encore été prouvé. La recette est donc exécutée par modèle,
pendant une activation provisoire explicitement réversible (DEC-010).

Ce que le plan peut prouver d'un artefact déjà présent — AUT-014
----------------------------------------------------------------
Sur le second déploiement réel, les deux GGUF du catalogue étaient DÉJÀ sur
l'hôte, leur en-tête avait été lu par `gguf_meta`, et le plan annonçait quand
même 837,1 Mio de téléchargement. L'information manquait au raisonnement, pas à
la collecte.

L'arbitrage tient dans une asymétrie : au moment du plan, la **divergence** d'un
artefact se prouve pour rien, sa **conformité** ne se prouve pas sans le relire
en entier.

- Prouver qu'un fichier est FAUX coûte un `stat()` : une taille différente de
  celle épinglée interdit mathématiquement au SHA-256 de correspondre. Le plan
  le fait donc lui-même, et en fait un **bloqueur** — jamais une réutilisation
  silencieuse, jamais un écrasement.
- Prouver qu'un fichier est BON coûte la lecture intégrale de ses octets.
  Hacher 40 Gio à chaque `bootstrap-plan` — commande qui n'écrit rien, qu'on
  relance à volonté et que `doctor` appelle — échangerait un défaut d'affichage
  contre un défaut de conception. Le plan ne hache donc **rien** : il exige une
  ATTESTATION, le manifeste de provenance écrit par AUT-006 après vérification
  octet à octet de l'ensemble complet contre les empreintes du catalogue.

Le SHA-256 lui-même reste confronté au catalogue à l'application, par l'étape
`verify_artifact` — qui, elle, lit bien tous les octets. Rien n'est troqué :
le hachage est déplacé du plan (lecture seule, rapide, rejouable) vers
l'application (une fois, au moment où l'artefact est mis en service).

Quand les trois conditions sont réunies — ensemble complet, tailles conformes,
manifeste cohérent —, l'étape `download_model` **disparaît** du plan, son volume
est décompté du total, et le motif est écrit dans le détail de la
`verify_artifact` qui reste seule. Dans tous les autres cas le téléchargement
reste proposé : le téléchargeur revérifiera et ne transférera rien s'il n'y a
rien à transférer, ce qui est le comportement conservateur.

Pourquoi ce module importe `downloader`, alors que M1 n'exécute rien
--------------------------------------------------------------------
`provenance_path()` et `manifest_problems()` sont des fonctions pures, sans
effet de bord, qui DÉFINISSENT ce qu'est un manifeste cohérent. Les réécrire ici
créerait une seconde définition, et c'est toujours celle qui n'a pas été mise à
jour qui laisse passer un artefact périmé : le plan crédirait une attestation que
`verify_artifact` refuserait ensuite, produisant un plan applicable et voué à
l'échec. C'est le raisonnement que `bootstrap/__init__.py` tient déjà pour
`runtime_installer` → `runtime_resolver`, ici dans l'autre sens. Le jour où la
séparation M1/M2 doit être rétablie strictement, ces deux fonctions ont leur
place dans un module de contrat partagé, pas recopiées des deux côtés.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Sequence

from bootstrap import catalog as catalog_mod
from bootstrap import downloader as downloader_mod
from bootstrap import gguf_meta
from bootstrap import inventory as inventory_mod
from bootstrap import llmfit as llmfit_mod
from bootstrap import runtime_resolver as runtime_mod
from bootstrap import schema

# Marge appliquée au budget disque avant de retenir un modèle : télécharger
# jusqu'au dernier octet libre d'un volume rend l'hôte inexploitable bien avant
# la fin du téléchargement (journaux, WAL SQLite, fichiers temporaires).
DISK_HEADROOM_FACTOR = 1.25

# Fraction de la RAM hôte qu'un modèle servi sur CPU a le droit de viser. Au
# delà, `MemoryMax` de l'unité systemd tue le processus en cours de chargement —
# le défaut exact que COR-007 a traité pour `minimax-m2.7`.
CPU_RAM_BUDGET_FRACTION = 0.6

_GIB = 1024 ** 3


class PlannerError(schema.PlanError):
    """Le plan ne peut pas être assemblé du tout (entrée invalide, pas un échec métier)."""


class PlannerUsageError(PlannerError):
    """
    L'appelant a mal formé sa demande : identifiant inconnu, fichier introuvable.

    Distincte de `PlannerError` parce que la CONSÉQUENCE diffère, et qu'un script
    la lit : « votre commande désigne quelque chose qui n'existe pas » sort en
    code 2, alors que « le planificateur lui-même a échoué » sort en 4. Les
    confondre ferait conclure à une panne de l'outil sur une simple faute de
    frappe dans un `--model` — et inversement, ferait passer une vraie panne
    pour une erreur de l'opérateur.
    """


@dataclass(frozen=True)
class PlannerOptions:
    """
    Entrées du planificateur. Aucune ne contient de secret.

    `generated_at` est fourni par l'appelant plutôt que lu à l'horloge, comme
    dans `schema` et `runtime_resolver` : c'est ce qui rend un plan reproductible
    en test, et c'est aussi ce qui permet de rejouer un plan daté d'un incident.
    """
    generated_at: str
    mode: str = "local"
    catalog_path: Path | None = None
    hardware_profile_path: Path | None = None
    models_dir: Path = inventory_mod.DEFAULT_MODELS_DIR
    selected_ids: tuple[str, ...] | None = None
    max_models: int = 1
    existing_binary: Path | None = None
    release_policy: runtime_mod.ReleasePolicy | None = None
    resolver_policy: runtime_mod.ResolverPolicy | None = None
    llmfit_config: llmfit_mod.LLMfitConfig = field(default_factory=llmfit_mod.LLMfitConfig)


# ── Point d'entrée ────────────────────────────────────────────────────────────

async def build_plan(options: PlannerOptions) -> schema.BootstrapPlan:
    """
    Assemble le plan complet. Ne lève que sur une entrée structurellement invalide.

    Une sonde en panne, un catalogue non épinglé, un runtime introuvable : rien
    de tout cela ne fait disparaître le plan. Ces situations produisent des
    sections dégradées, des constats et, s'il le faut, un plan **bloqué** — qui
    reste lisible et dit pourquoi. Un plan absent n'explique rien à personne.
    """
    _reject_unsupported_mode(options.mode)

    sections: list[schema.PlanSection] = []
    decisions: list[schema.Decision] = []

    hardware = _collect_hardware(options)
    sections.append(inventory_mod.to_plan_section(hardware))

    resolution = await _resolve_runtime(hardware, options)
    if resolution is None:
        sections.append(_no_release_policy_section(options))
        decisions.append(schema.Decision(
            topic="runtime llama-server",
            choice="aucun",
            rationale="aucune politique de release n'a été fournie au planificateur",
        ))
    else:
        sections.append(runtime_mod.to_plan_section(resolution))
        decisions.append(runtime_mod.to_decision(resolution))

    advice = llmfit_mod.run_llmfit(options.llmfit_config)
    sections.append(_recommendation_section(advice))

    catalog, catalog_section = _load_catalog(options)
    sections.append(catalog_section)

    selection = _select_models(catalog, hardware, advice, options)
    sections.append(_models_section(selection, options))
    decisions.append(_selection_decision(selection))

    plan = schema.BootstrapPlan(
        generated_at=options.generated_at,
        mode=options.mode,
        sections=tuple(sections),
        decisions=tuple(decisions),
    )

    # L'invariant « un plan bloqué ne propose aucune étape » est posé ICI, sur le
    # verdict complet, et non au cas par cas dans l'assemblage. La première
    # écriture ne le tenait que pour l'absence de politique de release : un
    # runtime déclaré NON RÉSOLU laissait passer téléchargement, écriture de
    # registre, activation et recette — sans la moindre étape d'installation.
    # Adossé aux bloqueurs, l'invariant vaut désormais pour toutes leurs causes,
    # y compris celles qui n'existent pas encore.
    if plan.blockers:
        return plan
    return replace(plan, steps=_assemble_steps(resolution, selection, options))


def _reject_unsupported_mode(mode: str) -> None:
    """
    Refuse le mode cluster tant qu'il n'est pas planifié pour de vrai.

    L'accepter en le recopiant dans le document serait pire que de le refuser :
    le plan produit inventorierait l'hôte gateway, choisirait un runtime LOCAL et
    prévoirait des GGUF sous son propre `/models`, alors qu'en cluster le binaire
    et les modèles vivent sur les nœuds. Un opérateur lirait un plan cohérent et
    entièrement faux — la pire des sorties, bien avant l'absence de sortie.

    Planifier un cluster suppose d'interroger chaque node-agent : c'est un
    chantier à part entière, hors du jalon M1. En attendant, la conduite à tenir
    est dans le message.
    """
    if mode == "cluster":
        raise PlannerUsageError(
            "La planification du mode cluster n'est pas implémentée (jalon M1). "
            "Accepter --mode cluster produirait un plan cohérent et faux : il "
            "inventorierait l'hôte gateway, choisirait un runtime local et "
            "prévoirait des modèles sous son propre volume, alors qu'en cluster le "
            "binaire et les GGUF vivent sur les nœuds. En attendant, planifiez "
            "chaque nœud séparément avec --hardware-profile et --models-dir, ou "
            "restez en --mode local."
        )
    if mode != "local":
        raise PlannerUsageError(f"--mode doit valoir « local » ou « cluster », reçu {mode!r}.")


# ── Étape 1 : inventaire ──────────────────────────────────────────────────────

def _collect_hardware(options: PlannerOptions) -> inventory_mod.HardwareProfile:
    """
    Profil sondé, ou profil déclaré si l'opérateur en fournit un (§5, cas VM).

    `load_hardware_profile()` prend le TEXTE du document, pas un chemin : la
    lecture du fichier appartient à l'appelant, ce qui laisse le producteur
    testable sans toucher au disque. C'est donc ici qu'on lit, et ici que
    l'illisibilité devient une erreur d'entrée — un profil explicitement demandé
    par l'opérateur et introuvable n'est pas une situation à dégrader en
    silence : c'est une consigne qu'on ne peut pas honorer.
    """
    if options.hardware_profile_path is None:
        return inventory_mod.collect_hardware(
            options=inventory_mod.InventoryOptions(models_dir=options.models_dir)
        )

    path = options.hardware_profile_path
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PlannerUsageError(f"--hardware-profile {path} : lecture impossible ({exc}).") from exc
    try:
        # `env` transmis explicitement : sans lui, le chargeur retombe sur `{}` et
        # CUDA_VISIBLE_DEVICES est ignoré pour un profil DÉCLARÉ. Deux GPU
        # déclarés avec `CUDA_VISIBLE_DEVICES=1` produisaient alors un budget VRAM
        # doublé — la variable gouverne ce que CUDA expose au runtime, quelle que
        # soit l'origine de la liste de GPU.
        return inventory_mod.load_hardware_profile(
            text, origin=f"--hardware-profile {path}", env=os.environ
        )
    except inventory_mod.InventoryError as exc:
        raise PlannerUsageError(str(exc)) from exc


# ── Étape 2 : runtime ─────────────────────────────────────────────────────────

async def _resolve_runtime(
    hardware: inventory_mod.HardwareProfile,
    options: PlannerOptions,
) -> runtime_mod.RuntimeResolution | None:
    """
    Raccord inventaire → résolveur : le document §5 est le contrat commun.

    `hardware_profile_from_mapping()` consomme directement le dictionnaire de
    l'inventaire. Aucune structure intermédiaire n'est inventée ici : le jour où
    un champ de §5 change, il change à un seul endroit.

    Retourne `None` quand aucune politique de release n'est fournie. Le
    planificateur **ne fabrique pas** de politique par défaut : `ReleasePolicy`
    exige une version et un commit épinglés, et en inventer ferait passer un
    numéro imaginaire pour un fait vérifié dans tous les manifestes produits.
    L'absence de politique est donc un bloqueur explicite, pas un défaut à combler.
    """
    if options.release_policy is None and options.resolver_policy is None:
        return None
    policy = options.resolver_policy or runtime_mod.ResolverPolicy(
        release=options.release_policy  # type: ignore[arg-type]
    )
    profile = runtime_mod.hardware_profile_from_mapping(hardware.to_dict())
    return await runtime_mod.resolve_runtime(
        profile,
        policy,
        installed_at=options.generated_at,
        existing_binary=options.existing_binary,
    )


def _no_release_policy_section(options: PlannerOptions) -> schema.PlanSection:
    """Section `runtime` quand rien n'épingle le runtime. Bloquante, et elle dit quoi fournir."""
    return schema.PlanSection(
        name=schema.SECTION_RUNTIME,
        version=runtime_mod.SECTION_VERSION,
        status="fail",
        summary="aucune politique de release fournie",
        data={"release_policy": None, "mode": options.mode},
        findings=(schema.Finding(
            code="politique_de_release_absente",
            level="fail",
            message=(
                "Aucune politique de release n'a été fournie : le planificateur ne peut "
                "pas décider quel llama-server installer, et il refuse d'en inventer une. "
                "Fournissez une version épinglée (« bNNNNN »), le commit correspondant et "
                "le premier build patché connu (plancher de sécurité, "
                "cf. GHSA-8947-pfff-2f3c) — c'est de là que LLAMA_SERVER_MIN_BUILD est "
                "généré (§6)."
            ),
        ),),
        notes=(
            "Un numéro de build inventé se propagerait dans tous les manifestes de\n"
            "provenance produits, où il aurait l'apparence d'un fait vérifié.\n"
            "Tant que le runtime n'est pas résolu, AUCUNE étape n'est proposée : ce qui\n"
            "serait retenu reste visible dans la section « Modèles retenus », mais une\n"
            "séquence de téléchargements sans binaire capable de servir les modèles\n"
            "inviterait à n'exécuter que la moitié du plan.",
        ),
    )


# ── Étape 3 : recommandation ──────────────────────────────────────────────────

def _recommendation_section(result: llmfit_mod.LLMfitResult) -> schema.PlanSection:
    """
    Section LLMfit, augmentée de ses limites en clair.

    Le producteur ne peut pas les rendre visibles lui-même : elles vivent dans
    `data`, que le rendu humain n'imprime pas. Le planificateur les remonte dans
    `notes`, qui est imprimé. Sans ce raccord, l'opérateur lirait une
    recommandation sans jamais lire ce qu'elle ignore.
    """
    section = llmfit_mod.to_plan_section(result)
    return replace(section, notes=(llmfit_mod.render_advisory_notice(),))


# ── Étape 4 : catalogue ───────────────────────────────────────────────────────

def _load_catalog(options: PlannerOptions) -> tuple[catalog_mod.Catalog | None, schema.PlanSection]:
    """
    Charge le catalogue. Un catalogue illisible bloque le plan — il ne le tue pas.

    Distinction volontaire : sans catalogue, aucun modèle ne peut être proposé,
    donc le plan n'est pas applicable ; mais l'inventaire et la résolution du
    runtime restent utiles à l'opérateur qui diagnostique.
    """
    try:
        catalog = catalog_mod.load_catalog(options.catalog_path)
    except (catalog_mod.CatalogError, OSError) as exc:
        return None, schema.PlanSection(
            name=schema.SECTION_CATALOG,
            version=catalog_mod.SECTION_VERSION,
            status="fail",
            summary="catalogue illisible",
            data={"path": str(options.catalog_path or catalog_mod.DEFAULT_CATALOG_PATH)},
            findings=(schema.Finding(
                code="catalogue_illisible",
                level="fail",
                message=(
                    f"Le catalogue de modèles approuvés n'a pas pu être chargé : {exc}. "
                    "Aucun modèle ne peut être proposé tant qu'il n'est pas réparé."
                ),
            ),),
        )
    try:
        section = catalog_mod.to_plan_section(catalog, selected_ids=options.selected_ids)
    except catalog_mod.CatalogError as exc:
        # `--model` désigne un identifiant absent du catalogue : faute de saisie,
        # pas panne de l'outil. Sans ce raccord, l'erreur remontait jusqu'au
        # `except Exception` de la CLI et sortait en 4.
        raise PlannerUsageError(str(exc)) from exc
    return catalog, section


# ── Étape 5 : sélection ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class ArtifactState:
    """
    Ce que le plan CONSTATE de l'ensemble déjà posé sur le disque (AUT-014).

    Aucun champ n'est le résultat d'un hachage : tout vient de `stat()` et de la
    relecture d'un manifeste JSON. Voir l'arbitrage en tête de module.
    """
    entry_id: str
    expected: tuple[str, ...] = ()
    present: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    divergent: tuple[str, ...] = ()
    unsized: tuple[str, ...] = ()
    attestation_path: str = ""
    attestation_problems: tuple[str, ...] = ()
    attested: bool = False
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "expected": list(self.expected),
            "present": list(self.present),
            "missing": list(self.missing),
            "divergent": list(self.divergent),
            "unsized": list(self.unsized),
            "attestation_path": self.attestation_path,
            "attestation_problems": list(self.attestation_problems),
            "attested": self.attested,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ModelChoice:
    """Un modèle retenu, avec ce qui a permis de le retenir."""
    entry: catalog_mod.CatalogEntry
    reason: str
    inspection: dict[str, Any] | None = None
    artifact: ArtifactState | None = None


@dataclass(frozen=True)
class Selection:
    """Résultat de la sélection : ce qui est retenu, ce qui ne l'est pas, et pourquoi."""
    retained: tuple[ModelChoice, ...] = ()
    rejected: tuple[tuple[str, str], ...] = ()
    findings: tuple[schema.Finding, ...] = ()
    ordered_by_llmfit: bool = False
    budget: dict[str, Any] = field(default_factory=dict)
    inspection: gguf_meta.GgufInspection | None = None


def _select_models(
    catalog: catalog_mod.Catalog | None,
    hardware: inventory_mod.HardwareProfile,
    advice: llmfit_mod.LLMfitResult,
    options: PlannerOptions,
) -> Selection:
    """
    Applique la règle de §7, dans l'ordre : catalogue d'abord, LLMfit en dernier.

    LLMfit n'intervient qu'une fois les candidats établis, et **uniquement pour
    les ordonner**. Il ne peut ni ajouter un modèle absent du catalogue, ni
    ressusciter une entrée non épinglée, ni relever un budget. C'est la
    traduction en code de « conseiller, pas autorité » : le seul pouvoir laissé
    à la recommandation est celui de faire passer un candidat approuvé devant un
    autre candidat approuvé.
    """
    if catalog is None:
        return Selection(findings=(schema.Finding(
            code="selection_impossible",
            level="fail",
            message="Aucun catalogue chargé : la sélection de modèles ne peut pas avoir lieu.",
        ),))

    candidates = list(catalog.plannable_entries())
    if options.selected_ids is not None:
        wanted = set(options.selected_ids)
        candidates = [e for e in candidates if e.id in wanted]

    rejected: list[tuple[str, str]] = []
    findings: list[schema.Finding] = []

    for entry in catalog.entries:
        if not entry.plannable and (options.selected_ids is None or entry.id in set(options.selected_ids)):
            rejected.append((entry.id, "non planifiable — voir la section catalogue"))

    budget = _budget(hardware)
    fitting: list[tuple[catalog_mod.CatalogEntry, str]] = []
    for entry in candidates:
        verdict = _fits(entry, budget)
        if verdict is None:
            fitting.append((entry, _fit_reason(entry, budget)))
        else:
            rejected.append((entry.id, verdict))

    ordered, ordered_by_llmfit = _order_by_advice(fitting, advice)
    retained = tuple(ModelChoice(entry=e, reason=r) for e, r in ordered[: max(options.max_models, 0)])
    for entry, _ in ordered[max(options.max_models, 0):]:
        rejected.append((entry.id, f"au-delà de --max-models={options.max_models}"))

    if not retained:
        findings.append(schema.Finding(
            code="aucun_modele_retenu",
            level="fail",
            message=(
                "Aucun modèle du catalogue ne peut être retenu sur cet hôte : "
                + ("; ".join(f"{i} → {r}" for i, r in rejected) if rejected
                   else "le catalogue ne propose aucune entrée planifiable")
                + ". Sans modèle, le parcours jusqu'au premier token n'a pas d'objet."
            ),
        ))

    inspection = _inspect_local(retained, options)
    if inspection is not None:
        findings.extend(inspection.findings)
        retained = tuple(
            replace(choice, inspection=_inspection_for(choice, inspection))
            for choice in retained
        )

    # AUT-014 — le rapprochement avec le disque vient APRÈS la sélection : il ne
    # sert pas à retenir ou écarter un modèle, seulement à savoir ce qu'il reste
    # réellement à faire pour celui qui est retenu.
    retained = tuple(
        replace(choice, artifact=_reconcile_artifact(choice.entry, options.models_dir))
        for choice in retained
    )
    findings.extend(
        _divergence_finding(choice.artifact)
        for choice in retained
        if choice.artifact is not None and choice.artifact.divergent
    )

    return Selection(
        retained=retained,
        rejected=tuple(rejected),
        findings=tuple(findings),
        ordered_by_llmfit=ordered_by_llmfit,
        budget=budget,
        inspection=inspection,
    )


def _budget(hardware: inventory_mod.HardwareProfile) -> dict[str, Any]:
    """
    Ce dont l'hôte dispose réellement, et sur quelle grandeur juger un modèle.

    Sur un hôte à GPU visible, c'est la VRAM exposée qui décide. Sans GPU, un
    modèle est servi par le CPU et c'est la RAM hôte — bornée, parce qu'un
    `MemoryMax` systemd la borne aussi.
    """
    vram = hardware.visible_vram_total_bytes
    ram = hardware.ram_total_bytes
    return {
        "target": "vram" if vram > 0 else "ram",
        "vram_bytes": vram,
        "ram_bytes": ram,
        "ram_budget_bytes": int(ram * CPU_RAM_BUDGET_FRACTION),
        "disk_available_bytes": hardware.disk_available_bytes,
        "disk_headroom_factor": DISK_HEADROOM_FACTOR,
    }


def _fits(entry: catalog_mod.CatalogEntry, budget: dict[str, Any]) -> str | None:
    """Retourne la raison du refus, ou `None` si l'entrée tient. Conservateur par défaut."""
    needed_disk = int(entry.resources.disk_gb * _GIB * DISK_HEADROOM_FACTOR)
    if budget["disk_available_bytes"] < needed_disk:
        return (
            f"disque insuffisant sur le volume des modèles : "
            f"{budget['disk_available_bytes'] / _GIB:.1f} Go libres pour "
            f"{needed_disk / _GIB:.1f} Go requis (marge ×{DISK_HEADROOM_FACTOR})"
        )

    if budget["target"] == "vram":
        needed = int(entry.resources.initial_vram_gb * _GIB)
        if needed > budget["vram_bytes"]:
            return (
                f"VRAM insuffisante : {entry.resources.initial_vram_gb:.1f} Go estimés "
                f"pour {budget['vram_bytes'] / _GIB:.1f} Go exposés"
            )
        return None

    needed = int(entry.resources.initial_ram_gb * _GIB)
    if needed > budget["ram_budget_bytes"]:
        return (
            f"RAM insuffisante pour un service CPU : {entry.resources.initial_ram_gb:.1f} Go "
            f"estimés pour un budget de {budget['ram_budget_bytes'] / _GIB:.1f} Go "
            f"({CPU_RAM_BUDGET_FRACTION:.0%} de la RAM hôte)"
        )
    return None


def _fit_reason(entry: catalog_mod.CatalogEntry, budget: dict[str, Any]) -> str:
    if budget["target"] == "vram":
        return (
            f"tient dans la VRAM exposée ({entry.resources.initial_vram_gb:.1f} Go estimés "
            f"sur {budget['vram_bytes'] / _GIB:.1f} Go)"
        )
    return (
        f"tient dans le budget RAM CPU ({entry.resources.initial_ram_gb:.1f} Go estimés "
        f"sur {budget['ram_budget_bytes'] / _GIB:.1f} Go)"
    )


def _order_by_advice(
    fitting: Sequence[tuple[catalog_mod.CatalogEntry, str]],
    advice: llmfit_mod.LLMfitResult,
) -> tuple[list[tuple[catalog_mod.CatalogEntry, str]], bool]:
    """
    Ordonne les candidats approuvés. LLMfit ne fait que remonter les siens.

    Rapprochement volontairement conservateur : un candidat LLMfit ne compte que
    si son libellé désigne sans ambiguïté une entrée du catalogue. Un
    rapprochement flou ferait entrer par la petite porte le pouvoir qu'on refuse
    à LLMfit par la grande.
    """
    ordre_naturel = sorted(fitting, key=lambda pair: pair[0].resources.initial_vram_gb)
    recommendation = getattr(advice, "recommendation", None)
    candidates = list(getattr(recommendation, "candidates", ()) or ())
    if not candidates:
        return ordre_naturel, False

    # Le champ est `candidate` et pas `model_id` : c'est délibéré côté AUT-004,
    # pour qu'une chaîne venue d'un outil tiers ne serve jamais de clé de registre.
    labels = [str(getattr(c, "candidate", "") or "").strip().lower() for c in candidates]
    rank: dict[str, int] = {}
    for entry, _ in ordre_naturel:
        for position, label in enumerate(labels):
            if label and (label == entry.id or label == entry.repo_id.lower()):
                rank[entry.id] = position
                break

    if not rank:
        return ordre_naturel, False

    ordered = sorted(
        ordre_naturel,
        key=lambda pair: (rank.get(pair[0].id, len(labels)), pair[0].resources.initial_vram_gb),
    )
    return ordered, True


def _inspect_local(
    retained: Sequence[ModelChoice],
    options: PlannerOptions,
) -> gguf_meta.GgufInspection | None:
    """
    Inspecte les GGUF déjà présents localement, s'il y en a (AUT-013).

    Sur une machine vierge il n'y en a aucun — c'est le cas normal, et
    `inspect_paths` rend alors `skip`. Quand un fichier est déjà là (réinstallation,
    hôte de test), son header vaut mieux que l'estimation déclarée du catalogue :
    il vient du fichier réel.
    """
    if not retained:
        return None
    paths = [
        options.models_dir / file.name
        for choice in retained
        for file in choice.entry.files
    ]
    defaults = retained[0].entry.runtime.defaults
    return gguf_meta.inspect_paths(paths, gguf_meta.EstimationInputs(
        ctx_size=defaults.ctx_size,
        parallel=defaults.parallel,
        cache_type_k=defaults.cache_type_k,
        cache_type_v=defaults.cache_type_v,
    ))


def _inspection_for(
    choice: ModelChoice,
    inspection: gguf_meta.GgufInspection,
) -> dict[str, Any] | None:
    names = {file.name for file in choice.entry.files}
    for entry in inspection.entries:
        if Path(str(entry.get("path", ""))).name in names:
            return entry
    return None


# ── Artefacts déjà présents (AUT-014) ─────────────────────────────────────────

def _reconcile_artifact(entry: catalog_mod.CatalogEntry, models_dir: Path) -> ArtifactState:
    """
    Confronte l'ensemble du catalogue à ce qui est réellement au chemin cible.

    Ordre des verdicts, du plus grave au plus favorable — et il n'est pas
    interchangeable :

    1. **taille divergente** : le fichier porte le nom définitif mais pas la
       taille épinglée. Son SHA-256 ne PEUT pas correspondre ; c'est une preuve,
       pas une présomption. Bloquant, et ni réutilisé ni écrasé ;
    2. **ensemble incomplet** : un GGUF splitté ou un couple poids + `mmproj`
       est indivisible (§8). Créditer la moitié présente reviendrait à annoncer
       un volume que rien n'atteste — un ensemble à moitié posé n'a d'ailleurs
       jamais de manifeste, celui-ci n'étant écrit qu'en dernier. Le
       téléchargement reste donc proposé **en entier** ;
    3. **taille non épinglée** : sans `size_bytes` au catalogue, on ne peut rien
       conclure d'un `stat()`. Conservateur ;
    4. **pas d'attestation exploitable** : fichiers en place, tailles conformes,
       mais aucun manifeste cohérent. Le plan ne présume pas de la conformité
       qu'il n'a pas vérifiée ; le téléchargeur revérifiera, et ne transférera
       rien s'il n'y a rien à transférer ;
    5. **attesté** : tout est là, aux tailles du catalogue, et un manifeste
       cohérent dit que ces octets ont été confrontés aux empreintes épinglées.
       Le téléchargement disparaît du plan.
    """
    expected = tuple(f.name for f in entry.files)
    present: list[str] = []
    missing: list[str] = []
    divergent: list[str] = []
    unsized: list[str] = []

    for cfile in entry.files:
        target = models_dir / cfile.name
        try:
            size = target.stat().st_size if target.is_file() else None
        except OSError:
            # Un fichier présent mais illisible n'est pas une conformité : il est
            # traité comme absent, et le téléchargement restera proposé.
            size = None
        if size is None:
            missing.append(cfile.name)
            continue
        present.append(cfile.name)
        if cfile.size_bytes is None:
            unsized.append(cfile.name)
        elif size != cfile.size_bytes:
            divergent.append(cfile.name)

    attestation = downloader_mod.provenance_path(models_dir, entry.id)
    base = ArtifactState(
        entry_id=entry.id,
        expected=expected,
        present=tuple(present),
        missing=tuple(missing),
        divergent=tuple(divergent),
        unsized=tuple(unsized),
        attestation_path=str(attestation),
    )

    if divergent:
        return replace(base, reason=(
            f"{', '.join(divergent)} : présent(s) au chemin cible mais de taille "
            "différente de celle épinglée par le catalogue — le SHA-256 ne peut pas "
            "correspondre"
        ))

    if missing:
        if present:
            return replace(base, reason=(
                f"ensemble incomplet : {len(present)}/{len(expected)} fichier(s) présent(s), "
                f"manque {', '.join(missing)}. L'ensemble est indivisible (§8) : rien n'est "
                "décompté et le téléchargement reste proposé en entier"
            ))
        return replace(base, reason="aucun fichier de l'ensemble n'est présent au chemin cible")

    if unsized:
        return replace(base, reason=(
            f"{', '.join(unsized)} : présent(s), mais le catalogue n'épingle pas leur taille — "
            "rien ne permet de conclure sans relire tous leurs octets"
        ))

    problems = downloader_mod.manifest_problems(_read_attestation(attestation), entry)
    if problems:
        return replace(base, attestation_problems=problems, reason=(
            "fichiers présents et de taille conforme, mais aucune attestation de "
            f"vérification exploitable ({' ; '.join(problems)}) — le plan ne présume pas "
            "d'une empreinte qu'il n'a pas vérifiée, le téléchargement reste proposé et "
            "revérifiera sans rien transférer s'il n'y a rien à transférer"
        ))

    return replace(base, attested=True, reason=(
        f"les {len(expected)} fichier(s) de l'ensemble sont déjà au chemin cible, à la "
        "taille épinglée, et le manifeste de provenance atteste qu'ils ont été confrontés "
        "octet à octet aux empreintes du catalogue — aucun octet à retélécharger"
    ))


def _read_attestation(path: Path) -> dict[str, Any] | None:
    """Relit le manifeste de provenance. Une absence est normale, pas une erreur."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def _divergence_finding(state: ArtifactState) -> schema.Finding:
    """Un artefact provablement faux bloque le plan. Il n'est jamais réparé pour l'opérateur."""
    return schema.Finding(
        code="artefact_local_divergent",
        level="fail",
        message=(
            f"« {state.entry_id} » : {', '.join(state.divergent)} — un fichier porte déjà "
            "le nom définitif au chemin cible, mais sa taille diffère de celle épinglée par "
            "le catalogue : son empreinte SHA-256 ne peut pas correspondre. Le plan ne le "
            "réutilise pas et ne propose pas de l'écraser — déplacez-le ou supprimez-le "
            "vous-même après avoir compris d'où il vient, puis régénérez le plan."
        ),
    )


# ── Section « modèles retenus » ───────────────────────────────────────────────

def _models_section(selection: Selection, options: PlannerOptions) -> schema.PlanSection:
    """
    Ce que le plan retient, ce qu'il écarte, et sur quelle grandeur il a jugé.

    Le statut vient des constats (`status_from_findings`) et non d'un calcul
    parallèle : deux façons de décider du même statut finissent toujours par
    diverger, et c'est le divergent qui devient le bug.
    """
    data: dict[str, Any] = {
        "budget": selection.budget,
        "ordered_by_llmfit": selection.ordered_by_llmfit,
        "retained": [
            {
                "id": choice.entry.id,
                "display_name": choice.entry.display_name,
                "reason": choice.reason,
                "license": choice.entry.license.to_dict(),
                "resources": choice.entry.resources.to_dict(),
                "runtime_defaults": choice.entry.runtime.defaults.to_dict(),
                "local_inspection": choice.inspection,
                "local_artifact": choice.artifact.to_dict() if choice.artifact else None,
            }
            for choice in selection.retained
        ],
        "rejected": [{"id": i, "reason": r} for i, r in selection.rejected],
        "max_models": options.max_models,
    }
    if selection.inspection is not None:
        data["gguf_inspection"] = selection.inspection.to_plan_data()

    if selection.retained:
        summary = (
            f"{len(selection.retained)} modèle(s) retenu(s) : "
            + ", ".join(c.entry.id for c in selection.retained)
            + (" — ordonné par LLMfit" if selection.ordered_by_llmfit else "")
        )
    else:
        summary = "aucun modèle retenu"

    return schema.PlanSection(
        name=schema.SECTION_MODELS,
        version=1,
        status=schema.status_from_findings(selection.findings),
        summary=summary,
        data=data,
        findings=selection.findings,
        notes=(
            "Les valeurs de ressources sont des ESTIMATIONS conservatrices, jamais des\n"
            "mesures. L'étape `calibrate_model` du plan est ce qui les remplace par des\n"
            "pics observés ; tant qu'elle n'a pas eu lieu, l'entrée de registre reste\n"
            "désactivée (AUT-007).",
        ),
    )


def _selection_decision(selection: Selection) -> schema.Decision:
    if selection.retained:
        choice = selection.retained[0]
        return schema.Decision(
            topic="modèle par défaut",
            choice=choice.entry.id,
            rationale=(
                choice.reason
                + (", remonté par la recommandation LLMfit" if selection.ordered_by_llmfit
                   else ", plus petite empreinte estimée parmi les entrées approuvées qui tiennent")
            ),
            rejected=tuple(f"{i} ({r})" for i, r in selection.rejected),
        )
    return schema.Decision(
        topic="modèle par défaut",
        choice="aucun",
        rationale="aucune entrée approuvée du catalogue ne tient dans les ressources de cet hôte",
        rejected=tuple(f"{i} ({r})" for i, r in selection.rejected),
    )


# ── Étapes ────────────────────────────────────────────────────────────────────

def _assemble_steps(
    resolution: runtime_mod.RuntimeResolution | None,
    selection: Selection,
    options: PlannerOptions,
) -> tuple[schema.PlanStep, ...]:
    """
    Construit la séquence complète, numérotée en continu depuis 1.

    La renumérotation est centralisée ici parce que `schema` l'exige continue :
    chaque producteur numérote depuis 1 dans son coin, et le planificateur est le
    seul à connaître l'ordre global.
    """
    if resolution is None:
        # Aucune séquence n'est proposée tant que le runtime n'est pas résolu.
        # Décrire des téléchargements sans binaire capable de servir les modèles
        # inviterait à n'exécuter que la moitié du plan — et cette moitié-là est
        # justement celle qui consomme du disque et du réseau. Ce qui SERAIT
        # retenu reste visible dans la section « modèles », rien n'est caché.
        return ()

    steps: list[schema.PlanStep] = list(runtime_mod.to_plan_steps(resolution))

    for choice in selection.retained:
        steps.extend(_model_steps(choice, options))

    return tuple(replace(step, order=index + 1) for index, step in enumerate(steps))


def _model_steps(choice: ModelChoice, options: PlannerOptions) -> list[schema.PlanStep]:
    entry = choice.entry
    target_dir = options.models_dir
    steps: list[schema.PlanStep] = []

    if entry.license.operator_acceptance_required:
        steps.append(schema.PlanStep(
            order=0,
            action=schema.ACTION_ACCEPT_LICENSE,
            target=f"{entry.id} — {entry.license.base_model.id}",
            detail=(
                f"Acceptation explicite par l'opérateur avant tout téléchargement. "
                f"Modèle de base : {entry.license.base_model.id} ; fine-tune : "
                f"{entry.license.fine_tune.id} ; redistribution du GGUF "
                f"{'autorisée' if entry.license.redistribution_allowed else 'NON autorisée'}."
            ),
            requires_root=False,
            reversible=True,
        ))

    # AUT-014 — un ensemble déjà en place ET attesté ne se retéléchargue pas :
    # l'étape `download_model` disparaît, son volume avec elle, et la
    # `verify_artifact` qui reste seule porte le motif. Voir l'arbitrage en tête
    # de module : le plan ne hache rien, il exige une attestation.
    state = choice.artifact
    attested = state is not None and state.attested
    files = ", ".join(f.name for f in entry.files)

    if not attested:
        etat_local = (
            f" État local : {state.reason}."
            if state is not None and state.present and state.reason
            else ""
        )
        steps.append(schema.PlanStep(
            order=0,
            action=schema.ACTION_DOWNLOAD_MODEL,
            target=f"{entry.repo_id}@{entry.revision}",
            detail=(
                f"Télécharger l'ensemble indivisible ({files}) vers {target_dir}, à révision "
                f"figée, par fichier temporaire puis renommage atomique.{etat_local}"
            ),
            requires_root=True,
            reversible=True,
            estimated_bytes=entry.files.total_bytes,
        ))

    steps.append(schema.PlanStep(
        order=0,
        action=schema.ACTION_VERIFY_ARTIFACT,
        target=entry.id,
        detail=(
            (
                f"Aucun téléchargement proposé : {state.reason}. "  # type: ignore[union-attr]
                "Cette étape reste la seule preuve d'intégrité : elle relit les octets et "
                "confronte le SHA-256 de chaque fichier de l'ensemble au catalogue, avant "
                "toute mise en service. Un écart annule l'installation du modèle."
            )
            if attested else
            (
                "Vérifier le SHA-256 de chaque fichier de l'ensemble contre le catalogue, "
                "avant toute mise en service. Un écart annule l'installation du modèle."
            )
        ),
        requires_root=False,
        reversible=True,
    ))

    steps.append(schema.PlanStep(
        order=0,
        action=schema.ACTION_WRITE_REGISTRY,
        target=f"models.yaml → {entry.id}",
        detail=(
            "Écrire l'entrée de registre avec `enabled: false` et les paramètres du "
            "catalogue. Elle reste désactivée tant que la calibration et la recette "
            "n'ont pas réussi (AUT-007)."
        ),
        requires_root=True,
        reversible=True,
    ))

    steps.append(schema.PlanStep(
        order=0,
        action=schema.ACTION_CALIBRATE_MODEL,
        target=entry.id,
        detail=(
            "Chargement réel de calibration : relever les pics RAM/VRAM, la durée de "
            "chargement et le TTFT, puis PROPOSER un `vram_gb` — sans l'appliquer "
            "silencieusement (§9)."
        ),
        requires_root=False,
        reversible=True,
    ))

    steps.append(schema.PlanStep(
        order=0,
        action=schema.ACTION_ENABLE_MODEL,
        target=entry.id,
        detail=(
            "Publier `enabled: true` uniquement dans le registre vivant, avec la "
            "capacité issue de la calibration, à titre PROVISOIRE. Le fichier reste "
            "`enabled: false` jusqu'à la recette publique ; un échec ferme l'admission "
            "live et décharge le modèle (DEC-010, COR-022)."
        ),
        requires_root=True,
        reversible=True,
    ))

    steps.append(schema.PlanStep(
        order=0,
        action=schema.ACTION_SMOKE_TEST,
        target=entry.id,
        detail=(
            "Recette du premier token pour CE modèle à travers le chemin public réel "
            "nginx → gateway → llama-server (§10). Elle ferme l'activation provisoire : "
            "succès = activation conservée ; échec = retour immédiat à `enabled: false`."
        ),
        requires_root=False,
        reversible=True,
    ))

    steps.append(schema.PlanStep(
        order=0,
        action=schema.ACTION_WARMUP_MODEL,
        target=entry.id,
        detail=(
            "Préchauffer le modèle pour que le premier utilisateur ne paie pas le "
            "chargement après un déploiement réussi (AUT-010)."
        ),
        requires_root=False,
        reversible=True,
    ))

    return steps
