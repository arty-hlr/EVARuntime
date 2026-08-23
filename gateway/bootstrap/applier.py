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

Activation provisoire et compensation (DEC-010)
-------------------------------------------------
La recette publique a besoin d'un modèle visible, mais l'activation définitive
exige justement cette recette. Le plan résout ce cycle explicitement :
`calibrate_model → enable_model (provisoire) → smoke_test → warmup_model`.

L'applicateur porte la compensation : seule une calibration recoupée autorise la
transition provisoire dans le registre vivant, tandis que le disque reste à
`enabled: false`. Le smoke test suivant doit produire le second volet, puis la
preuve complète est validée et seulement alors persistée. Un échec HTTP, une
preuve illisible ou un refus final ferme l'admission live et décharge le modèle.
Un plan mal ordonné est refusé au pré-vol, avant toute mutation.

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

import asyncio
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterator, Mapping, Sequence, TypeVar

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


_T = TypeVar("_T")


async def _to_thread_completed(
    function: Callable[..., _T], *args: Any, **kwargs: Any
) -> _T:
    """Attend la fin réelle d'une mutation disque même si l'appelant est annulé.

    Annuler ``asyncio.to_thread`` n'arrête pas son thread. Rendre la main avant
    sa fin permettrait au rollback de constater un fichier encore désactivé,
    puis au thread orphelin de publier ``enabled: true`` après la compensation.
    On préserve l'annulation, mais seulement après la terminaison de l'opération
    bloquante afin que l'appelant puisse compenser son état final réel.
    """
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except Exception:
            # L'appelant exécute le rollback dans son ``except BaseException``.
            # L'annulation originale reste le signal le plus fidèle à propager.
            pass
        raise


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
    generation_probe_factory: Callable[[str], Any] | None = None
    sleep: warmup_mod.AsyncSleep = warmup_mod._no_sleep

    def __post_init__(self) -> None:
        if self.generation_probe is not None and self.generation_probe_factory is not None:
            raise ApplierUsageError(
                "WarmupWiring : fournissez generation_probe OU "
                "generation_probe_factory, jamais les deux"
            )


@dataclass(frozen=True)
class RegistrySyncWiring:
    """Synchronisation éphémère du registre disque avec la gateway mono-worker."""

    activate: Callable[[str, float, str], Awaitable[None]]
    rollback: Callable[[str, str], Awaitable[None]]
    confirm: Callable[[str, str], Awaitable[None]]


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
    registry_sync: RegistrySyncWiring | None = None
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
        self._provisional: set[str] = set()
        self._awaiting_confirmation: set[str] = set()
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

    def calibration(self, model_id: str) -> writer_mod.CalibrationProof | None:
        """Calibration courante, ou celle d'une preuve complète fournie."""
        measured = self._calibrations.get(model_id)
        if measured is not None:
            return measured
        supplied = self._supplied.get(model_id)
        return supplied.calibration if supplied is not None else None

    def mark_provisional(self, model_id: str) -> None:
        self._provisional.add(model_id)
        self._awaiting_confirmation.add(model_id)

    def mark_awaiting_confirmation(self, model_id: str) -> None:
        self._awaiting_confirmation.add(model_id)

    def clear_provisional(self, model_id: str) -> None:
        self._provisional.discard(model_id)

    def clear_awaiting_confirmation(self, model_id: str) -> None:
        self._awaiting_confirmation.discard(model_id)

    def is_awaiting_confirmation(self, model_id: str) -> bool:
        return model_id in self._awaiting_confirmation

    def is_provisional(self, model_id: str) -> bool:
        return model_id in self._provisional

    def provisional_models(self) -> tuple[str, ...]:
        return tuple(sorted(self._provisional))

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
            _guarded_enable(writer_config, ledger, config.registry_sync),
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
                _targeted_smoke_test_executor(wiring),
                ledger,
                replace(config.writer, activation_proofs=ledger)
                if config.writer is not None else None,
                config.registry_sync,
            ),
        )

    if config.warmup is not None:
        registry.register(
            schema.ACTION_WARMUP_MODEL,
            _targeted_warmup_executor(config.warmup),
        )

    return registry


def _targeted_smoke_test_executor(wiring: FirstTokenWiring) -> execution.StepExecutor:
    """Lie chaque recette au modèle nommé par SON étape, y compris en plan multi-modèle."""

    async def executer(
        step: schema.PlanStep, context: execution.ExecutionContext
    ) -> execution.StepResult:
        model_id = writer_mod.model_id_from_target(step.action, step.target)
        inner = first_token_mod.make_smoke_test_executor(
            settings=replace(wiring.settings, model_id=model_id),
            client=wiring.client,
            admin_secret=wiring.admin_secret,
            sleep=wiring.sleep,
            identity_suffix=wiring.identity_suffix,
        )
        return await inner(step, context)

    return executer


def _targeted_warmup_executor(wiring: WarmupWiring) -> execution.StepExecutor:
    """Lie le pré-chauffage à la cible relue dans le plan, pas à un réglage global."""

    async def executer(
        step: schema.PlanStep, context: execution.ExecutionContext
    ) -> execution.StepResult:
        model_id = writer_mod.model_id_from_target(step.action, step.target)
        inner = warmup_mod.make_warmup_executor(
            settings=replace(wiring.settings, model_id=model_id),
            client=wiring.client,
            admin_secret=wiring.admin_secret,
            generation_probe=(
                wiring.generation_probe_factory(model_id)
                if wiring.generation_probe_factory is not None
                else wiring.generation_probe
            ),
            sleep=wiring.sleep,
        )
        return await inner(step, context)

    return executer


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
    inner: execution.StepExecutor,
    ledger: ProofLedger,
    writer_config: writer_mod.WriterConfig | None = None,
    registry_sync: RegistrySyncWiring | None = None,
) -> execution.StepExecutor:
    """
    Exécute la recette, capture sa preuve et confirme l'activation provisoire.

    Dès que `enable_model` a réellement ouvert le modèle, tout chemin d'échec de
    cette fonction compense sur disque ET dans la gateway avant de rendre la main. Une recette qui
    répond mais dont le document de preuve est incomplet n'est donc pas un
    succès : sans preuve complète, l'entrée ne peut rester servable.
    """

    async def executer(
        step: schema.PlanStep, context: execution.ExecutionContext
    ) -> execution.StepResult:
        model_id = writer_mod.model_id_from_target(step.action, step.target)
        try:
            result = await inner(step, context)
        except BaseException:
            if writer_config is not None and ledger.is_provisional(model_id):
                await _rollback_provisional(
                    writer_config, ledger, model_id,
                    mode=context.mode, reason="exception pendant la recette publique",
                    registry_sync=registry_sync,
                )
            raise
        if result.status != execution.STEP_DONE:
            if writer_config is not None and ledger.is_provisional(model_id):
                rollback = await _rollback_provisional(
                    writer_config, ledger, model_id,
                    mode=context.mode, reason="échec de la recette publique",
                    registry_sync=registry_sync,
                )
                return _result_with_rollback(result, rollback)
            return result
        try:
            proof = smoke_test_proof_from_evidence(result.evidence)
            if proof.model_id != model_id:
                raise writer_mod.ProofRejected(
                    f"la recette annonce « {proof.model_id} » mais l'étape cible "
                    f"« {model_id} »"
                )
            ledger.record_smoke_test(proof)
        except (writer_mod.ProofRejected, writer_mod.RegistryWriterError) as exc:
            message = (
                f"la recette de « {model_id} » a répondu mais sa preuve n'est pas "
                f"exploitable : {exc}"
            )
            rollback = None
            if writer_config is not None and ledger.is_provisional(model_id):
                rollback = await _rollback_provisional(
                    writer_config, ledger, model_id,
                    mode=context.mode, reason="preuve de recette inexploitable",
                    registry_sync=registry_sync,
                )
            failed = execution.StepResult.for_step(
                step,
                status=execution.STEP_FAILED,
                summary=f"preuve de recette inexploitable pour « {model_id} »",
                duration_ms=result.duration_ms,
                evidence=result.evidence,
                findings=result.findings + (_finding(
                    "preuve_recette_inexploitable", "fail", message,
                ),),
                error=message,
            )
            return _result_with_rollback(failed, rollback) if rollback else failed
        except BaseException:
            if writer_config is not None and ledger.is_provisional(model_id):
                await _rollback_provisional(
                    writer_config, ledger, model_id,
                    mode=context.mode, reason="exception pendant la capture de preuve",
                    registry_sync=registry_sync,
                )
            raise

        if writer_config is None or not ledger.is_awaiting_confirmation(model_id):
            return result

        try:
            activation_proof = ledger[model_id]
        except KeyError:
            message = f"preuve complète absente après la recette de « {model_id} »"
            rollback = await _rollback_provisional(
                writer_config, ledger, model_id,
                mode=context.mode, reason=message, registry_sync=registry_sync,
            )
            failed = execution.StepResult.for_step(
                step, status=execution.STEP_FAILED,
                summary=f"activation définitive impossible pour « {model_id} »",
                evidence=result.evidence,
                findings=result.findings + (_finding(
                    "activation_preuve_complete_absente", "fail", message,
                ),),
                error=message,
            )
            return _result_with_rollback(failed, rollback)

        try:
            confirmation = await _to_thread_completed(
                writer_mod.enable_model_entry,
                writer_config,
                model_id,
                activation_proof,
                mode=context.mode,
            )
        except BaseException:
            if ledger.is_provisional(model_id):
                await _rollback_provisional(
                    writer_config, ledger, model_id,
                    mode=context.mode, reason="exception pendant la confirmation de preuve",
                    registry_sync=registry_sync,
                )
            raise
        if confirmation.failed:
            rollback = None
            if ledger.is_provisional(model_id):
                rollback = await _rollback_provisional(
                    writer_config, ledger, model_id,
                    mode=context.mode, reason="preuve complète refusée après la recette",
                    registry_sync=registry_sync,
                )
            message = confirmation.error or confirmation.summary
            failed = execution.StepResult.for_step(
                step, status=execution.STEP_FAILED,
                summary=f"activation définitive refusée pour « {model_id} »",
                duration_ms=result.duration_ms,
                evidence={
                    **result.evidence,
                    "activation_confirmation": confirmation.evidence,
                },
                findings=result.findings + confirmation.findings,
                error=message,
            )
            return _result_with_rollback(failed, rollback) if rollback else failed

        if (
            context.mode is execution.ExecutionMode.APPLY
            and registry_sync is not None
            and ledger.is_provisional(model_id)
        ):
            digest = confirmation.evidence.get("registry_sha256")
            if not isinstance(digest, str) or not digest:
                sync_error = "confirmation sans empreinte du registre publié"
            else:
                try:
                    await registry_sync.confirm(model_id, digest)
                except Exception as exc:
                    sync_error = execution.redact_for_log(
                        f"{type(exc).__name__}: {exc}"
                    )
                else:
                    sync_error = ""
            if sync_error:
                rollback = await _rollback_provisional(
                    writer_config,
                    ledger,
                    model_id,
                    mode=context.mode,
                    reason="confirmation live du registre impossible",
                    registry_sync=registry_sync,
                )
                failed = execution.StepResult.for_step(
                    step,
                    status=execution.STEP_FAILED,
                    summary=f"synchronisation live refusée pour « {model_id} »",
                    duration_ms=result.duration_ms,
                    evidence={
                        **result.evidence,
                        "activation_confirmation": confirmation.evidence,
                    },
                    findings=result.findings + (_finding(
                        "activation_live_confirmation_echouee",
                        "fail",
                        f"La preuve est valide, mais la gateway n'a pas confirmé le "
                        f"snapshot persistant de « {model_id} » : {sync_error}.",
                    ),),
                    error=sync_error,
                )
                return _result_with_rollback(failed, rollback)

        ledger.clear_provisional(model_id)
        ledger.clear_awaiting_confirmation(model_id)
        return replace(
            result,
            evidence={
                **result.evidence,
                "activation_confirmation": confirmation.evidence,
            },
            findings=result.findings + confirmation.findings,
        )

    return executer


def _guarded_enable(
    writer_config: writer_mod.WriterConfig,
    ledger: ProofLedger,
    registry_sync: RegistrySyncWiring | None = None,
) -> execution.StepExecutor:
    """
    Confirme sur preuve complète, ou ouvre provisoirement sur calibration.

    L'ouverture provisoire est enregistrée dans le ledger uniquement quand une
    écriture réelle a eu lieu. Le smoke test qui suit en devient alors
    responsable jusqu'à confirmation ou rollback.
    """

    async def executer(
        step: schema.PlanStep, context: execution.ExecutionContext
    ) -> execution.StepResult:
        model_id = writer_mod.model_id_from_target(step.action, step.target)
        if model_id in ledger:
            return await writer_mod.make_enable_model_executor(writer_config)(step, context)

        calibration = ledger.calibration(model_id)
        if calibration is not None:
            change = await asyncio.to_thread(
                writer_mod.provisionally_enable_model_entry,
                writer_config,
                model_id,
                calibration,
                mode=context.mode,
            )
            if change.status == execution.STEP_DONE:
                # Le marqueur précède le réseau : une réponse perdue après une
                # activation live réussie doit tout de même déclencher le rollback.
                ledger.mark_provisional(model_id)
                if (
                    context.mode is execution.ExecutionMode.APPLY
                    and registry_sync is not None
                ):
                    digest = change.evidence.get("registry_sha256")
                    vram_gb = change.evidence.get("vram_gb")
                    try:
                        if not isinstance(digest, str) or not digest:
                            raise ApplierError("empreinte du registre provisoire absente")
                        if not isinstance(vram_gb, (int, float)) or isinstance(vram_gb, bool):
                            raise ApplierError("capacité VRAM provisoire absente")
                        await registry_sync.activate(model_id, float(vram_gb), digest)
                    except Exception as exc:
                        detail = execution.redact_for_log(
                            f"{type(exc).__name__}: {exc}"
                        )
                        rollback = await _rollback_provisional(
                            writer_config,
                            ledger,
                            model_id,
                            mode=context.mode,
                            reason="activation live impossible",
                            registry_sync=registry_sync,
                        )
                        failed = execution.StepResult.for_step(
                            step,
                            status=execution.STEP_FAILED,
                            summary=f"activation live refusée pour « {model_id} »",
                            evidence=change.evidence,
                            findings=change.findings + (_finding(
                                "activation_live_echouee", "fail",
                                f"La gateway n'a pas publié l'activation provisoire : {detail}",
                            ),),
                            error=detail,
                        )
                        return _result_with_rollback(failed, rollback)
            elif change.status == execution.STEP_ALREADY_SATISFIED:
                ledger.mark_awaiting_confirmation(model_id)
            context.journaliser(f"activation provisoire [{model_id}] → {change.status}")
            return writer_mod._to_step_result(step, change)

        manquants = ledger.missing_volets(model_id)

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


async def _rollback_provisional(
    writer_config: writer_mod.WriterConfig,
    ledger: ProofLedger,
    model_id: str,
    *,
    mode: execution.ExecutionMode,
    reason: str,
    registry_sync: RegistrySyncWiring | None = None,
) -> writer_mod.RegistryChange:
    """Compense sur disque puis en mémoire, et ne libère le marqueur qu'après les deux."""
    try:
        change = await _to_thread_completed(
            writer_mod.rollback_provisional_model_entry,
            writer_config,
            model_id,
            mode=mode,
            reason=reason,
        )
    except Exception as exc:
        detail = execution.redact_for_log(f"{type(exc).__name__}: {exc}")
        return writer_mod.RegistryChange(
            status=execution.STEP_FAILED,
            summary=f"rollback impossible pour « {model_id} »",
            evidence={
                "model_id": model_id,
                "written": False,
                "rollback": False,
            },
            findings=(_finding(
                "rollback_activation_exception", "fail",
                f"La désactivation compensatoire de « {model_id} » a levé : {detail}",
            ),),
            error=f"rollback impossible pour « {model_id} » : {detail}",
        )
    disk_safe = change.status in {
        execution.STEP_DONE, execution.STEP_ALREADY_SATISFIED,
    }
    if not disk_safe:
        return change

    if mode is execution.ExecutionMode.APPLY and registry_sync is not None:
        digest = change.evidence.get("registry_sha256")
        try:
            if not isinstance(digest, str) or not digest:
                raise ApplierError("empreinte du registre désactivé absente")
            await registry_sync.rollback(model_id, digest)
        except Exception as exc:
            detail = execution.redact_for_log(f"{type(exc).__name__}: {exc}")
            return writer_mod.RegistryChange(
                status=execution.STEP_FAILED,
                summary=f"rollback live impossible pour « {model_id} »",
                evidence={
                    **change.evidence,
                    "live_rollback": False,
                },
                findings=change.findings + (_finding(
                    "rollback_activation_live_echoue", "fail",
                    f"Le disque est désactivé, mais la gateway n'a pas confirmé la "
                    f"fermeture live de « {model_id} » : {detail}",
                ),),
                error=f"rollback live impossible pour « {model_id} » : {detail}",
            )

    ledger.clear_provisional(model_id)
    ledger.clear_awaiting_confirmation(model_id)
    return change


def _result_with_rollback(
    result: execution.StepResult, rollback: writer_mod.RegistryChange
) -> execution.StepResult:
    """Joint la preuve de compensation au résultat qui a déclenché le rollback."""
    evidence = dict(result.evidence)
    evidence["rollback"] = rollback.evidence
    findings = result.findings + rollback.findings
    error = result.error
    if rollback.failed:
        detail = rollback.error or rollback.summary
        error = f"{error or result.summary} ; rollback ÉCHOUÉ : {detail}"
        findings += (_finding(
            "rollback_activation_echoue", "fail",
            f"La recette a échoué et la désactivation compensatoire a aussi échoué : {detail}",
        ),)
    return replace(result, evidence=evidence, findings=findings, error=error)


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
    Valide le triplet atomique calibration → activation provisoire → recette.

    Sans preuve complète déjà fournie, la calibration doit précéder
    immédiatement l'activation et un smoke test du MÊME modèle doit la suivre
    immédiatement. Toute autre forme est refusée avant le départ : exécuter la
    partie amont d'un plan structurellement incapable de compenser laisserait
    précisément l'état partiel que le pré-vol doit empêcher.
    """
    constats: list[schema.Finding] = []
    steps = list(plan.steps)
    for index, step in enumerate(steps):
        if step.action != schema.ACTION_ENABLE_MODEL:
            continue
        try:
            model_id = writer_mod.model_id_from_target(step.action, step.target)
        except writer_mod.RegistryWriterError as exc:
            constats.append(_finding(
                "chaine_activation_invalide", "fail",
                f"Étape {step.order} : cible d'activation invalide ({exc}).",
            ))
            continue
        if model_id in ledger:
            continue

        previous = steps[index - 1] if index > 0 else None
        following = steps[index + 1] if index + 1 < len(steps) else None
        calibration_ok = (
            previous is not None
            and previous.action == schema.ACTION_CALIBRATE_MODEL
            and previous.target == model_id
        )
        smoke_ok = (
            following is not None
            and following.action == schema.ACTION_SMOKE_TEST
            and following.target == model_id
        )
        if calibration_ok and smoke_ok:
            continue
        attendu = (
            f"calibrate_model({model_id}) → enable_model({model_id}) → "
            f"smoke_test({model_id})"
        )
        recu = (
            f"{previous.action if previous else '<début>'} → enable_model → "
            f"{following.action if following else '<fin>'}"
        )
        constats.append(_finding(
            "chaine_activation_non_compensable", "fail",
            f"Étape {step.order} : « {model_id} » ne suit pas le triplet atomique "
            f"DEC-010. Attendu {attendu}, reçu {recu}. Rien ne sera exécuté.",
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

async def apply_loaded_plan(
    plan: execution.LoadedPlan,
    config: ApplierConfig,
    *,
    mode: execution.ExecutionMode,
    allowed_roots: Sequence[Path],
    now: Callable[[], str] = execution._utc_now_iso,
    monotonic: Callable[[], float] = time.monotonic,
    log: Callable[[str], None] = execution._discard_log,
) -> ApplicationOutcome:
    """
    Exécute exactement un plan déjà relu et rend journal + rapport.

    `mode` n'a **pas de défaut** : personne n'applique par omission d'argument.
    C'est la propriété que `ExecutionMode` a été conçu pour donner, et la
    reprendre ici est ce qui la rend effective au niveau de l'applicateur.

    Le consommateur passe le `LoadedPlan` issu de `execution.load_plan_file()` ou
    `execution.load_plan_document()`. L'applicateur ne relit aucun chemin ici :
    les raccords de production et l'exécution portent donc sur le même instantané
    validé, même si le fichier source change ensuite.

    Ordre des refus, du moins cher au plus cher :

    1. une action du plan n'a pas d'exécuteur : refus **avant** tout départ ;
    2. une chaîne d'activation n'est pas compensable : refus avant tout départ.
    """
    if not isinstance(plan, execution.LoadedPlan):
        raise ApplierUsageError(
            "apply_loaded_plan exige un LoadedPlan déjà validé ; "
            "utilisez execution.load_plan_file() ou apply_plan_file()"
        )

    ledger = ProofLedger(config.supplied_proofs)
    registry = build_registry(config, ledger)

    reasons = missing_executor_reasons(plan, registry)
    if reasons:
        raise ApplierUsageError(
            "ce plan ne peut pas être exécuté avec le câblage fourni — rien n'a été "
            "tenté : " + " ; ".join(reasons)
        )

    if (
        mode is execution.ExecutionMode.APPLY
        and any(step.action == schema.ACTION_ENABLE_MODEL for step in plan.steps)
        and config.registry_sync is None
    ):
        raise ApplierUsageError(
            "ce plan active un modèle, mais ApplierConfig.registry_sync est absent — "
            "rien n'a été tenté : écrire models.yaml ne met pas à jour le registre "
            "déjà chargé par la gateway et ne permet pas un rollback live sûr"
        )

    findings = proof_chain_findings(plan, ledger)
    if findings:
        raise ApplierUsageError(
            "ce plan porte une chaîne d'activation non compensable — rien n'a été "
            "tenté : " + " ; ".join(f.message for f in findings)
        )

    context = execution.ExecutionContext(
        mode,
        allowed_roots=tuple(Path(root) for root in allowed_roots),
        monotonic=monotonic,
        now=now,
        log=log,
    )
    rollback_failures: list[str] = []
    try:
        report = await execution.execute_plan(plan, registry, context)
    finally:
        # Filet de dernier recours : couvre notamment une annulation entre
        # `enable_model` et l'entrée dans l'exécuteur `smoke_test`, ou un callback
        # de journalisation qui lève. Le chemin normal compense dans l'étape de
        # recette et retire déjà le marqueur.
        if config.writer is not None:
            writer_config = replace(config.writer, activation_proofs=ledger)
            for model_id in ledger.provisional_models():
                rollback = await _rollback_provisional(
                    writer_config,
                    ledger,
                    model_id,
                    mode=mode,
                    reason="sortie de l'applicateur avant confirmation de la recette",
                    registry_sync=config.registry_sync,
                )
                if rollback.failed:
                    rollback_failures.append(rollback.error or rollback.summary)
        if rollback_failures:
            raise ApplierError(
                "l'applicateur s'est arrêté avec une activation provisoire et sa "
                "compensation a échoué : " + " ; ".join(rollback_failures)
            )
    install = install_report_mod.build_install_report(
        plan_document=plan.document, execution_report=report, now=now,
    )
    return ApplicationOutcome(
        plan=plan, report=report, install=install, findings=findings
    )


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
    Relit une fois un fichier de plan, puis délègue son instantané validé.

    Cette entrée est conservée pour les appelants qui ne construisent pas encore
    leur raccord depuis un `LoadedPlan`. Un appelant qui a déjà relu le document
    doit employer `apply_loaded_plan()` afin d'éviter une seconde lecture TOCTOU.
    """
    plan = execution.load_plan_file(path)
    return await apply_loaded_plan(
        plan,
        config,
        mode=mode,
        allowed_roots=allowed_roots,
        now=now,
        monotonic=monotonic,
        log=log,
    )


__all__ = [
    "ApplicationOutcome",
    "ApplierConfig",
    "ApplierError",
    "ApplierUsageError",
    "FirstTokenWiring",
    "ProofLedger",
    "RegistrySyncWiring",
    "WarmupWiring",
    "apply_loaded_plan",
    "apply_plan_file",
    "build_registry",
    "calibration_proof_from_evidence",
    "missing_executor_reasons",
    "project_proof",
    "proof_chain_findings",
    "smoke_test_proof_from_evidence",
]
