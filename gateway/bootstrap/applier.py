"""
AUT-015 — application du plan de bootstrap : le seul module qui connaît tous les exécuteurs.

Ce que fait ce module, et ce qu'il ne fait pas
---------------------------------------------
Il est à M2 ce que `planner` est à M1. `planner` est le seul à connaître tous les
PRODUCTEURS ; celui-ci est le seul à connaître tous les EXÉCUTEURS. Il relit un
plan, construit un registre d'exécuteurs complet, vérifie **avant de commencer**
que chaque action du plan en a un, exécute, puis produit le rapport
d'installation d'AUT-011.

Il n'implémente aucune action métier : il n'écrit aucun registre, ne télécharge
rien, ne charge aucun modèle. Tout cela appartient aux six chantiers d'AUT-006 →
AUT-011. Ce qu'il apporte, et qu'aucun d'eux ne pouvait apporter seul, c'est le
**raccord** : le câblage, l'ordre, et la chaîne des preuves.

Pourquoi l'applicateur est le seul à tout importer
--------------------------------------------------
Les exécuteurs ne se connaissent pas — même règle que les producteurs de la
vague 5, et pour la même raison : l'un peut être mis en échec sans entraîner les
autres. Le prix de cette indépendance est que quelqu'un doit faire les raccords.
C'est ici, et nulle part ailleurs.

La chaîne des preuves, et pourquoi elle demande un porteur d'état
-----------------------------------------------------------------
`enable_model` (AUT-007) n'active un modèle que sur `ActivationProof`, qui exige
DEUX volets : une calibration réelle (AUT-008) et une recette du premier token
(AUT-009). Ces deux volets sont produits par des étapes qui s'exécutent APRÈS
que le registre d'exécuteurs a été construit. Le raccord ne peut donc pas être
un argument de construction : c'est un `ProofLedger`, passé comme
`WriterConfig.activation_proofs` et rempli au fil de l'exécution.

Le registre est vide au départ et **ne fabrique jamais rien** :

- il n'accepte un volet que depuis le résultat d'une étape qui a RÉUSSI ;
- il ne rend un `ActivationProof` que lorsque les DEUX volets sont là ;
- un volet illisible ou incomplet fait échouer l'étape qui devait le produire,
  au lieu d'être silencieusement perdu — sans quoi l'échec surviendrait plus
  tard, à l'activation, sans dire d'où il vient ;
- une simulation ne produit aucun volet, donc n'autorise aucune activation.

Contradiction d'ordonnancement, constatée et non masquée
---------------------------------------------------------
Le planificateur ordonne `calibrate_model → enable_model → warmup_model`, puis
un `smoke_test` **unique et final**. Or `enable_model` exige la preuve de ce
smoke test. Aucun plan produit par `bootstrap-plan` ne peut donc satisfaire
`enable_model` en application réelle : la recette a besoin d'un modèle activé,
et l'activation a besoin de la recette.

Ce module ne tranche pas ce nœud — il n'en a pas le mandat — mais il refuse de
le masquer :

- `proof_chain_findings()` le CONSTATE avant toute exécution et le publie ;
- en application, `enable_model` échoue explicitement en nommant le volet
  manquant et l'étape qui aurait dû le produire ;
- en simulation, `enable_model` est **sauté** avec la même explication : une
  simulation ne peut pas produire de preuve, et prétendre qu'elle « appliquerait »
  ferait croire l'activation acquise.

`verify_artifact`, une action à deux domaines
----------------------------------------------
Le plan émet `verify_artifact` pour l'archive de `llama-server` ET pour chaque
ensemble de GGUF, avec deux grammaires de cible. `ExecutorRegistry` n'admet
qu'un exécuteur par action : l'applicateur enregistre donc un aiguilleur, qui
interroge le catalogue puis la décision de runtime. La vérification de l'archive
du runtime n'a pas d'exécuteur propre — elle est faite par `install_runtime`
lui-même, avant extraction — et l'étape est donc `skipped` avec sa raison,
jamais rapportée comme faite.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from bootstrap import calibration as calibration_mod
from bootstrap import downloader as downloader_mod
from bootstrap import execution
from bootstrap import first_token as first_token_mod
from bootstrap import install_report as install_report_mod
from bootstrap import registry_writer as writer_mod
from bootstrap import runtime_installer as runtime_installer_mod
from bootstrap import schema
from bootstrap import warmup as warmup_mod

# Statuts d'étape qui valent « la mesure a réellement eu lieu ». `would_apply`
# n'en fait pas partie : une simulation ne mesure rien. `already_satisfied` si,
# et c'est délibéré — une calibration réutilisée est une mesure faite, relue et
# revalidée par AUT-008, pas une mesure absente.
_PROOF_BEARING_STATUSES: frozenset[str] = frozenset({
    execution.STEP_DONE, execution.STEP_ALREADY_SATISFIED,
})


class ApplierError(schema.PlanError):
    """L'application ne peut pas avoir lieu du tout (pas un échec métier)."""


class ApplierUsageError(ApplierError):
    """
    L'appelant a mal formé sa demande : plan introuvable, câblage incomplet.

    Distincte de `ApplierError` pour la même raison que `PlannerUsageError` l'est
    de `PlannerError` : la CONSÉQUENCE diffère et un script la lit. « votre
    commande ne fournit pas de quoi exécuter ce plan » sort en 2 ; « l'applicateur
    lui-même a échoué » sort en 4. Les confondre ferait conclure à une panne de
    l'outil sur une option oubliée.
    """


# ── Câblages qui demandent plus qu'une configuration ──────────────────────────

@dataclass(frozen=True)
class FirstTokenWiring:
    """Ce qu'AUT-009 exige et qu'aucune configuration seule ne porte."""
    settings: first_token_mod.FirstTokenSettings
    client: Any
    admin_secret: str
    sleep: first_token_mod.AsyncSleep = first_token_mod._no_sleep
    identity_suffix: Callable[[], str] = first_token_mod._default_identity_suffix


@dataclass(frozen=True)
class WarmupWiring:
    """Ce qu'AUT-010 exige. Le secret d'administration ne transite que par ici."""
    settings: warmup_mod.WarmupSettings
    client: Any
    admin_secret: str
    generation_probe: Any = None
    sleep: warmup_mod.AsyncSleep = warmup_mod._no_sleep


@dataclass(frozen=True)
class ApplierConfig:
    """
    Le câblage complet. Chaque champ absent est une famille d'actions non exécutable.

    Aucun champ n'a de valeur par défaut fonctionnelle : `None` signifie « je
    n'ai pas de quoi exécuter cela », et le contrôle de pré-vol le dira avant que
    quoi que ce soit ne commence. Un défaut implicite ferait exactement l'inverse
    — il ferait croire au câblage.
    """
    runtime: runtime_installer_mod.RuntimeInstaller | None = None
    download: downloader_mod.DownloadConfig | None = None
    writer: writer_mod.WriterConfig | None = None
    calibration: calibration_mod.CalibrationOptions | None = None
    first_token: FirstTokenWiring | None = None
    warmup: WarmupWiring | None = None

    # Preuves d'activation fournies par l'opérateur — issues d'une installation
    # antérieure sur le même hôte. Elles passent par la même validation que
    # celles produites ici : `_check_proof` recoupe empreintes, runtime, matériel
    # et fraîcheur. Ce n'est donc pas une porte dérobée, mais le seul chemin
    # praticable tant que la contradiction d'ordonnancement n'est pas tranchée.
    supplied_proofs: Mapping[str, writer_mod.ActivationProof] = field(default_factory=dict)


# ── Porteur d'état des preuves ────────────────────────────────────────────────

class ProofLedger(Mapping[str, writer_mod.ActivationProof]):
    """
    Ce qu'une exécution a PROUVÉ, modèle par modèle. Jamais ce qu'elle suppose.

    Implémente `Mapping` pour être passé tel quel à
    `WriterConfig.activation_proofs` : `enable_model_entry()` interroge ce
    dictionnaire au moment où il s'exécute, donc après les étapes qui le
    remplissent. C'est le seul raccord possible entre des exécuteurs construits
    avant le départ et des preuves qui n'existent qu'en cours de route.

    `__getitem__` ne rend une preuve que si les DEUX volets sont présents. Un
    volet seul n'est pas une preuve incomplète qu'on pourrait compléter par
    défaut : c'est une absence de preuve.
    """

    def __init__(
        self, supplied: Mapping[str, writer_mod.ActivationProof] | None = None
    ) -> None:
        self._calibrations: dict[str, writer_mod.CalibrationProof] = {}
        self._smoke_tests: dict[str, writer_mod.SmokeTestProof] = {}
        self._supplied: dict[str, writer_mod.ActivationProof] = dict(supplied or {})
        for model_id, proof in self._supplied.items():
            if not isinstance(proof, writer_mod.ActivationProof):
                raise ApplierUsageError(
                    f"preuve fournie pour « {model_id} » : attendu un ActivationProof, "
                    f"reçu {type(proof).__name__}. Un dictionnaire libre n'est pas une "
                    "preuve — passez par ActivationProof.from_mapping()."
                )

    # ── Enregistrement ────────────────────────────────────────────────────────

    def record_calibration(self, proof: writer_mod.CalibrationProof) -> None:
        self._calibrations[proof.model_id] = proof

    def record_smoke_test(self, proof: writer_mod.SmokeTestProof) -> None:
        self._smoke_tests[proof.model_id] = proof

    def missing_volets(self, model_id: str) -> tuple[str, ...]:
        """Volets qui manquent pour ce modèle. Vide si une preuve est disponible."""
        if model_id in self._supplied:
            return ()
        manquants: list[str] = []
        if model_id not in self._calibrations:
            manquants.append("calibration (étape « calibrate_model »)")
        if model_id not in self._smoke_tests:
            manquants.append("recette du premier token (étape « smoke_test »)")
        return tuple(manquants)

    # ── Contrat Mapping ───────────────────────────────────────────────────────

    def __getitem__(self, model_id: str) -> writer_mod.ActivationProof:
        # Une preuve produite par CETTE exécution prime sur une preuve fournie :
        # elle décrit l'état courant de l'hôte, l'autre décrit un passé.
        calibration = self._calibrations.get(model_id)
        smoke = self._smoke_tests.get(model_id)
        if calibration is not None and smoke is not None:
            return writer_mod.ActivationProof(calibration=calibration, smoke_test=smoke)
        supplied = self._supplied.get(model_id)
        if supplied is not None:
            return supplied
        raise KeyError(model_id)

    def __iter__(self) -> Iterator[str]:
        candidats = set(self._supplied) | (set(self._calibrations) & set(self._smoke_tests))
        return iter(sorted(candidats))

    def __len__(self) -> int:
        return len(set(iter(self)))


# ── Projection stricte d'un document de producteur ────────────────────────────

def project_proof(node: Any, keys: frozenset[str], label: str) -> dict[str, Any]:
    """
    Extrait EXACTEMENT `keys` d'un document de producteur, ou lève.

    Ce n'est pas une traduction : aucun nom n'est réécrit, aucune valeur n'est
    déduite, aucun défaut n'est comblé. Les producteurs publient déjà les noms du
    contrat consommateur ; ce qui reste à faire est de retenir les clés que ce
    contrat déclare — et de refuser bruyamment s'il en manque une.

    Une clé absente est un refus, jamais un `None` : c'est la seule barrière qui
    empêche une preuve d'être fabriquée à partir d'un document partiel.
    """
    if not isinstance(node, Mapping):
        raise writer_mod.ProofRejected(
            f"{label} : attendu un objet, reçu {type(node).__name__}"
        )
    manquantes = sorted(k for k in keys if k not in node)
    if manquantes:
        raise writer_mod.ProofRejected(
            f"{label} : le producteur ne fournit pas {manquantes} — aucune valeur par "
            "défaut n'est inventée, la preuve n'est pas constituée"
        )
    return {k: node[k] for k in keys}


def calibration_proof_from_evidence(evidence: Any) -> writer_mod.CalibrationProof:
    """Volet « calibration » d'une preuve, depuis l'`evidence` d'AUT-008."""
    if not isinstance(evidence, Mapping) or "calibration" not in evidence:
        raise writer_mod.ProofRejected(
            "le résultat de calibration ne porte pas de bloc « calibration » : rien "
            "n'atteste de la mesure, l'activation ne pourra pas être autorisée"
        )
    return writer_mod.CalibrationProof.from_mapping(
        project_proof(
            evidence["calibration"], writer_mod.CALIBRATION_PROOF_KEYS, "calibration"
        )
    )


def smoke_test_proof_from_evidence(evidence: Any) -> writer_mod.SmokeTestProof:
    """
    Volet « smoke_test » d'une preuve, depuis l'`evidence` d'AUT-009.

    `proof_authorizes()` est interrogé AVANT la projection, et c'est la seule
    règle admise pour transformer une recette en feu vert : elle refuse un
    document tronqué, d'une autre version, issu d'une simulation, ou dont la
    génération n'a pas été facturée. La réécrire ici en aurait fait une seconde
    version, et c'est toujours celle qui n'a pas été mise à jour qui autorise.

    Limite à dire à voix haute : le modèle confronté est celui que le document
    annonce, donc ce contrôle-là est tautologique ICI. Il ne peut pas en être
    autrement — la cible de l'étape `smoke_test` est la chaîne publique
    (« nginx → gateway → llama-server »), pas un modèle, et la recette dérive
    elle-même le plus petit modèle activé. C'est `registry_writer._check_proof`
    qui confronte le modèle de la preuve à celui qu'on active, et lui seul.
    """
    if not isinstance(evidence, Mapping) or not isinstance(evidence.get("proof"), Mapping):
        raise writer_mod.ProofRejected(
            "le résultat de la recette ne porte pas de document « proof » exploitable"
        )
    document = evidence["proof"]
    model_id = document.get("model_id")
    if not first_token_mod.proof_authorizes(dict(document), model_id):
        raise writer_mod.ProofRejected(
            f"la recette du premier token n'autorise pas « {model_id} » "
            f"(verdict {document.get('verdict')!r}, cause {document.get('reason')!r}) : "
            "seule une génération réellement servie ET facturée vaut preuve"
        )
    return writer_mod.SmokeTestProof.from_mapping(
        project_proof(document, writer_mod.SMOKE_TEST_PROOF_KEYS, "smoke_test")
    )


# ── Construction du registre ──────────────────────────────────────────────────

def _finding(code: str, level: str, message: str) -> schema.Finding:
    return schema.Finding(code=code, level=level, message=message)  # type: ignore[arg-type]


def build_registry(config: ApplierConfig, ledger: ProofLedger) -> execution.ExecutorRegistry:
    """
    Assemble le registre complet à partir du câblage fourni.

    Ce qui n'est pas câblé n'est **pas** enregistré : le registre reflète ce que
    l'applicateur peut réellement faire, et `missing_actions()` le dira avant le
    départ. Enregistrer un exécuteur « qui échoue toujours » aurait été pire —
    le plan aurait été entamé, puis abandonné à mi-parcours.
    """
    registry = execution.ExecutorRegistry()

    if config.runtime is not None:
        runtime_installer_mod.register_runtime_installer(registry, config.runtime)

    if config.download is not None:
        executors = downloader_mod.make_executors(config.download)
        registry.register(
            schema.ACTION_DOWNLOAD_MODEL, executors[schema.ACTION_DOWNLOAD_MODEL]
        )
        registry.register(
            schema.ACTION_ACCEPT_LICENSE, executors[schema.ACTION_ACCEPT_LICENSE]
        )
        registry.register(
            schema.ACTION_VERIFY_ARTIFACT,
            _verify_dispatcher(config, executors[schema.ACTION_VERIFY_ARTIFACT]),
        )
    elif config.runtime is not None:
        registry.register(schema.ACTION_VERIFY_ARTIFACT, _verify_dispatcher(config, None))

    if config.writer is not None:
        # Le registre de preuves est injecté ICI, dans une copie de la
        # configuration : `WriterConfig` est gelée, mais le `Mapping` qu'elle
        # porte est vivant. C'est ce qui permet à `enable_model_entry()`
        # d'interroger, au moment où il s'exécute, ce que les étapes précédentes
        # ont prouvé — sans qu'aucune preuve ne puisse être présumée.
        writer_config = replace(config.writer, activation_proofs=ledger)
        registry.register(
            schema.ACTION_WRITE_REGISTRY,
            writer_mod.make_write_registry_executor(writer_config),
        )
        registry.register(
            schema.ACTION_ENABLE_MODEL,
            _guarded_enable(writer_mod.make_enable_model_executor(writer_config), ledger),
        )

    if config.calibration is not None:
        registry.register(
            schema.ACTION_CALIBRATE_MODEL,
            _capturing_calibration(calibration_mod.make_executor(config.calibration), ledger),
        )

    if config.first_token is not None:
        wiring = config.first_token
        registry.register(
            schema.ACTION_SMOKE_TEST,
            _capturing_smoke_test(
                first_token_mod.make_smoke_test_executor(
                    settings=wiring.settings, client=wiring.client,
                    admin_secret=wiring.admin_secret, sleep=wiring.sleep,
                    identity_suffix=wiring.identity_suffix,
                ),
                ledger,
            ),
        )

    if config.warmup is not None:
        warmup_mod.register_executors(
            registry,
            settings=config.warmup.settings,
            client=config.warmup.client,
            admin_secret=config.warmup.admin_secret,
            generation_probe=config.warmup.generation_probe,
            sleep=config.warmup.sleep,
        )

    return registry


def _verify_dispatcher(
    config: ApplierConfig, catalog_executor: execution.StepExecutor | None
) -> execution.StepExecutor:
    """
    Aiguille `verify_artifact` entre ses deux domaines. Ne devine jamais.

    Le catalogue est interrogé d'abord parce qu'il refuse bruyamment ce qui ne
    lui appartient pas. Si la cible désigne la décision de runtime, l'étape est
    sautée avec sa raison : cette vérification-là est faite par `install_runtime`
    avant extraction, et la rapporter comme faite ici affirmerait un contrôle qui
    n'a pas encore eu lieu.
    """

    async def executer(
        step: schema.PlanStep, context: execution.ExecutionContext
    ) -> execution.StepResult:
        if config.download is not None and catalog_executor is not None:
            try:
                downloader_mod.resolve_entry(step, config.download.catalog)
            except downloader_mod.DownloadError:
                pass
            else:
                return await catalog_executor(step, context)

        if config.runtime is not None and runtime_installer_mod.covers_step(
            config.runtime.request, step
        ):
            return execution.StepResult.for_step(
                step,
                status=execution.STEP_SKIPPED,
                summary=(
                    "vérification de l'archive du runtime : assurée par l'étape "
                    "« install_runtime » elle-même, avant extraction"
                ),
                findings=(_finding(
                    "verification_runtime_integree", "info",
                    "Le plan décrit la vérification de l'archive de llama-server comme une "
                    "étape distincte, mais l'installateur (AUT-016) contrôle l'empreinte "
                    "avant d'extraire quoi que ce soit. Rien n'est vérifié à CE numéro "
                    "d'étape : le contrôle a lieu au suivant, et l'installation est annulée "
                    "s'il échoue.",
                ),),
            )

        message = (
            f"la cible « {step.target} » de l'action « verify_artifact » ne correspond ni à "
            "une entrée du catalogue ni à la décision de runtime câblée : l'applicateur "
            "refuse de choisir entre ses deux domaines"
        )
        return execution.StepResult.for_step(
            step,
            status=execution.STEP_FAILED,
            summary="cible de vérification non rattachable",
            findings=(_finding("verification_cible_inconnue", "fail", message),),
            error=message,
        )

    return executer


def _capturing_calibration(
    inner: execution.StepExecutor, ledger: ProofLedger
) -> execution.StepExecutor:
    """
    Exécute la calibration, puis CAPTURE son volet de preuve, ou échoue.

    Une calibration réussie dont la preuve est inexploitable ne peut pas être
    rapportée comme un succès : l'étape suivante échouerait sans dire pourquoi,
    et l'opérateur chercherait le défaut à l'activation alors qu'il est ici.
    """

    async def executer(
        step: schema.PlanStep, context: execution.ExecutionContext
    ) -> execution.StepResult:
        result = await inner(step, context)
        if result.status not in _PROOF_BEARING_STATUSES:
            return result
        try:
            ledger.record_calibration(calibration_proof_from_evidence(result.evidence))
        except (writer_mod.ProofRejected, writer_mod.RegistryWriterError) as exc:
            message = (
                f"la calibration de « {step.target} » a abouti mais sa preuve n'est pas "
                f"exploitable : {exc}"
            )
            return execution.StepResult.for_step(
                step,
                status=execution.STEP_FAILED,
                summary=f"preuve de calibration inexploitable pour « {step.target} »",
                duration_ms=result.duration_ms,
                evidence=result.evidence,
                findings=result.findings + (
                    _finding("preuve_calibration_inexploitable", "fail", message),
                ),
                error=message,
            )
        return result

    return executer


def _capturing_smoke_test(
    inner: execution.StepExecutor, ledger: ProofLedger
) -> execution.StepExecutor:
    """
    Exécute la recette, puis capture son volet de preuve **sans jamais échouer dessus**.

    Asymétrie assumée avec la calibration : la recette est l'étape FINALE du
    plan. Sa preuve ne sert plus à rien dans cette exécution-ci — elle ne peut
    servir qu'à une exécution ultérieure. Faire échouer une recette réussie
    parce que sa preuve n'est pas capturable dégraderait un succès réel en échec
    pour une raison qui ne concerne personne à cet instant. Le constat est
    publié, le résultat ne change pas.
    """

    async def executer(
        step: schema.PlanStep, context: execution.ExecutionContext
    ) -> execution.StepResult:
        result = await inner(step, context)
        if result.status != execution.STEP_DONE:
            return result
        try:
            ledger.record_smoke_test(smoke_test_proof_from_evidence(result.evidence))
        except (writer_mod.ProofRejected, writer_mod.RegistryWriterError) as exc:
            return replace(
                result,
                findings=result.findings + (_finding(
                    "preuve_recette_non_capturee", "warn",
                    f"La recette a réussi, mais sa preuve n'a pas pu être constituée "
                    f"({exc}). Elle n'autorisera donc aucune activation ultérieure.",
                ),),
            )
        return result

    return executer


def _guarded_enable(
    inner: execution.StepExecutor, ledger: ProofLedger
) -> execution.StepExecutor:
    """
    Refuse l'activation AVANT de la tenter quand la preuve n'existe pas.

    AUT-007 refuserait de lui-même, mais avec un message qui ne dit pas QUEL
    volet manque ni quelle étape aurait dû le produire. Cette garde nomme les
    deux. En simulation, elle **saute** l'étape au lieu de la faire échouer :
    une simulation ne produit aucune preuve, et rapporter un échec ferait
    conclure à un défaut de l'hôte là où il n'y a qu'une propriété du mode.
    """

    async def executer(
        step: schema.PlanStep, context: execution.ExecutionContext
    ) -> execution.StepResult:
        model_id = writer_mod.model_id_from_target(step.action, step.target)
        manquants = ledger.missing_volets(model_id)
        if not manquants:
            return await inner(step, context)

        detail = (
            f"« {model_id} » ne sera pas activé : "
            + " et ".join(manquants)
            + " — aucune preuve n'a été produite par cette exécution, et aucune n'a été "
            "fournie. L'activation est la seule action qui met un modèle en service : "
            "elle ne s'exécute que sur preuve recoupée (AUT-007)."
        )
        if context.dry_run:
            return execution.StepResult.for_step(
                step,
                status=execution.STEP_SKIPPED,
                summary=f"activation de « {model_id} » non simulable, faute de preuve",
                findings=(_finding("activation_non_simulable", "warn", detail),),
            )
        return execution.StepResult.for_step(
            step,
            status=execution.STEP_FAILED,
            summary=f"activation de « {model_id} » refusée, faute de preuve",
            findings=(_finding("activation_sans_preuve", "fail", detail),),
            error=detail,
        )

    return executer


# ── Contrôles de pré-vol ──────────────────────────────────────────────────────

def missing_executor_reasons(
    plan: execution.LoadedPlan, registry: execution.ExecutorRegistry
) -> tuple[str, ...]:
    """
    Actions du plan qu'aucun exécuteur ne prend en charge, avec ce qui les câblerait.

    Contrôlé avant tout départ : un plan à moitié exécuté laisse l'hôte dans un
    état que personne n'a décrit, et c'est le seul état dont aucun rapport ne
    peut rendre compte.
    """
    manquantes = registry.missing_actions(plan.steps)
    return tuple(
        f"« {action} » : aucun exécuteur — câblez {_WIRING_FOR.get(action, 'ce chantier')}"
        for action in manquantes
    )


_WIRING_FOR: dict[str, str] = {
    schema.ACTION_INSTALL_RUNTIME: "ApplierConfig.runtime (AUT-016)",
    schema.ACTION_DOWNLOAD_MODEL: "ApplierConfig.download (AUT-006)",
    schema.ACTION_VERIFY_ARTIFACT: "ApplierConfig.download ou .runtime (AUT-006/015)",
    schema.ACTION_ACCEPT_LICENSE: "ApplierConfig.download (AUT-006)",
    schema.ACTION_WRITE_REGISTRY: "ApplierConfig.writer (AUT-007)",
    schema.ACTION_ENABLE_MODEL: "ApplierConfig.writer (AUT-007)",
    schema.ACTION_CALIBRATE_MODEL: "ApplierConfig.calibration (AUT-008)",
    schema.ACTION_SMOKE_TEST: "ApplierConfig.first_token (AUT-009)",
    schema.ACTION_WARMUP_MODEL: "ApplierConfig.warmup (AUT-010)",
}


def proof_chain_findings(
    plan: execution.LoadedPlan, ledger: ProofLedger
) -> tuple[schema.Finding, ...]:
    """
    Constate, AVANT d'exécuter, les activations dont la preuve est hors d'atteinte.

    Le contrôle est purement ordinal : pour chaque `enable_model`, les deux
    étapes productrices doivent le PRÉCÉDER. Le planificateur place le smoke
    test en dernier — la contradiction est donc structurelle et non
    accidentelle, et elle est nommée ici plutôt que découverte à l'étape N.

    Un constat, pas un refus : les étapes antérieures (installation,
    téléchargement, vérification, écriture du registre) restent utiles et
    idempotentes. Les interdire ferait payer à l'opérateur un nœud qu'il n'a pas
    noué.
    """
    constats: list[schema.Finding] = []
    for step in plan.steps:
        if step.action != schema.ACTION_ENABLE_MODEL:
            continue
        try:
            model_id = writer_mod.model_id_from_target(step.action, step.target)
        except writer_mod.RegistryWriterError:
            continue
        if model_id in ledger:
            continue
        amont = [s.action for s in plan.steps if s.order < step.order]
        manquants = [
            libelle
            for action, libelle in (
                (schema.ACTION_CALIBRATE_MODEL, "calibrate_model"),
                (schema.ACTION_SMOKE_TEST, "smoke_test"),
            )
            if action not in amont
        ]
        if not manquants:
            continue
        constats.append(_finding(
            "chaine_de_preuve_impossible", "warn",
            f"Étape {step.order} : l'activation de « {model_id} » exige une preuve à deux "
            f"volets, mais le plan ne place aucune étape {' ni '.join(manquants)} avant "
            "elle. Le smoke test est unique et FINAL dans un plan de bootstrap, alors que "
            "l'activation en dépend — et la recette a elle-même besoin d'un modèle activé. "
            "En application, cette étape échouera ; fournissez une preuve d'une "
            "installation antérieure (ApplierConfig.supplied_proofs) ou faites trancher "
            "l'ordonnancement.",
        ))
    return tuple(constats)


# ── Résultat ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ApplicationOutcome:
    """
    Tout ce qu'une application produit : le journal, le rapport, et les constats d'assemblage.

    Le code de sortie est celui du **rapport d'installation** et non celui du
    journal : le rapport est le document final, et il est strictement le plus
    conservateur des deux — il évalue les sept conditions de sortie de M2 une par
    une, alors que le journal ne connaît que ses propres étapes.
    """
    plan: execution.LoadedPlan
    report: execution.ExecutionReport
    install: install_report_mod.InstallReport
    findings: tuple[schema.Finding, ...] = ()

    def exit_code(self) -> int:
        return self.install.exit_code()


# ── Point d'entrée ────────────────────────────────────────────────────────────

async def apply_plan_file(
    path: Path | str,
    config: ApplierConfig,
    *,
    mode: execution.ExecutionMode,
    allowed_roots: Sequence[Path],
    now: Callable[[], str] = execution._utc_now_iso,
    monotonic: Callable[[], float] = time.monotonic,
    log: Callable[[str], None] = execution._discard_log,
) -> ApplicationOutcome:
    """
    Relit un plan, l'exécute, et rend journal + rapport d'installation.

    `mode` n'a **pas de défaut** : personne n'applique par omission d'argument.
    C'est la propriété que `ExecutionMode` a été conçu pour donner, et la
    reprendre ici est ce qui la rend effective au niveau de l'applicateur.

    Ordre des refus, du moins cher au plus cher :

    1. le plan est refusé par `execution.load_plan_file()` — toutes ses barrières
       s'appliquent, aucune n'est réimplémentée ici ;
    2. une action du plan n'a pas d'exécuteur : refus **avant** tout départ ;
    3. la chaîne des preuves est impossible : constat publié, exécution menée.
    """
    plan = execution.load_plan_file(path)

    ledger = ProofLedger(config.supplied_proofs)
    registry = build_registry(config, ledger)

    reasons = missing_executor_reasons(plan, registry)
    if reasons:
        raise ApplierUsageError(
            "ce plan ne peut pas être exécuté avec le câblage fourni — rien n'a été "
            "tenté : " + " ; ".join(reasons)
        )

    findings = proof_chain_findings(plan, ledger)

    context = execution.ExecutionContext(
        mode,
        allowed_roots=tuple(Path(root) for root in allowed_roots),
        monotonic=monotonic,
        now=now,
        log=log,
    )
    report = await execution.execute_plan(plan, registry, context)
    install = install_report_mod.build_install_report(
        plan_document=plan.document, execution_report=report, now=now,
    )
    return ApplicationOutcome(
        plan=plan, report=report, install=install, findings=findings
    )


__all__ = [
    "ApplicationOutcome",
    "ApplierConfig",
    "ApplierError",
    "ApplierUsageError",
    "FirstTokenWiring",
    "ProofLedger",
    "WarmupWiring",
    "apply_plan_file",
    "build_registry",
    "calibration_proof_from_evidence",
    "missing_executor_reasons",
    "project_proof",
    "proof_chain_findings",
    "smoke_test_proof_from_evidence",
]
