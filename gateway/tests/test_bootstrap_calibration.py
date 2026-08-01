"""
AUT-008 — régressions de la calibration réelle RAM/VRAM (`bootstrap/calibration.py`).

Ce module est le seul du paquet qui prétend **mesurer**. Ces tests verrouillent
donc, dans l'ordre, les cinq propriétés qui font la différence entre une mesure
et un chiffre :

1. **on mesure pendant, pas après** — un relevé unique pris une fois la charge
   retombée ne mesure rien. Les doubles de sondes n'exposent le pic que TANT QUE
   l'opération est en cours : une implémentation qui échantillonnerait après coup
   ne verrait que le repos, et ces tests deviendraient rouges ;
2. **une mesure ratée n'est jamais une mesure optimiste** — pas de repli sur
   l'estimation statique de `gguf_meta`, pas de zéro tenu pour un GPU vide ;
3. **une mesure n'est réutilisable que si matériel, runtime et paramètres sont
   compatibles** (§9) — implémenté comme une décision, testé comme telle, et le
   message doit nommer laquelle des empreintes diverge ;
4. **mesurer et proposer, jamais appliquer** — aucune écriture de `models.yaml`,
   et la valeur brute est publiée à côté de la valeur proposée pour que le
   calcul soit contestable ;
5. **rien ne reste chargé** — y compris quand la calibration échoue en cours de
   route. Un modèle laissé sur le GPU par un outil de diagnostic est une fuite.

Aucun test ne lance de processus, n'ouvre de socket ni n'attend une seconde
réelle : toutes les sondes sont des doubles, l'horloge est fausse, et le seul
disque touché est `tmp_path`.

Chaque test d'ABSENCE porte son contrôle positif — règle de `CLAUDE.md` : un test
qui affirme « aucun YAML écrit » ou « aucune sonde appelée » sans prouver qu'il
saurait en voir un passerait au vert le jour où le module deviendrait inerte.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from bootstrap import calibration as cal
from bootstrap import execution as ex
from bootstrap import registry_writer as rw
from bootstrap import schema as sc

GIB = 1024 ** 3

RUNTIME = "llama-server b4321 (CUDA 12.4)"
MODELE = "gemma-4-26b-a4b"

# Le cas réel de §0.9 : 48 Go nominaux annoncés par `install.sh` contre ~45 Go
# réellement exposés par une L40S. Les deux doivent donner des empreintes
# différentes, sinon une preuve prise sur l'un serait réutilisée sur l'autre.
GPU_L40S_REEL = {
    "name": "NVIDIA L40S", "vram_total_mib": 46068,
    "driver_version": "550.90.07", "compute_cap": "8.9",
}
GPU_L40S_NOMINAL = {
    "name": "NVIDIA L40S", "vram_total_mib": 49152,
    "driver_version": "550.90.07", "compute_cap": "8.9",
}

IDLE_VRAM = 1 * GIB
IDLE_RAM = 4 * GIB
PIC_VRAM_REDUIT = 8 * GIB
PIC_VRAM_CIBLE = 12 * GIB
PIC_RAM_REDUIT = 6 * GIB
PIC_RAM_CIBLE = 7 * GIB

# Construit à l'exécution : un littéral ressemblant à un vrai jeton n'a rien à
# faire dans un dépôt, même en fixture.
FAUX_TOKEN = "hf_" + "B" * 24


# ── Doubles ───────────────────────────────────────────────────────────────────

class Horloge:
    """Horloge monotone fausse. N'avance que quand on le lui demande."""

    def __init__(self, depart: float = 1000.0) -> None:
        self.t = depart

    def __call__(self) -> float:
        return self.t

    def avancer(self, secondes: float) -> None:
        self.t += secondes


class Sondes:
    """
    Doubles des sondes de calibration. Aucun processus, aucun réseau, aucune attente.

    Le point clé est `phase` : la VRAM « occupée » n'est élevée que PENDANT un
    chargement ou un prompt, et retombe au repos dès que l'opération rend la
    main. C'est ce qui rend les tests de pic discriminants — un module qui
    relèverait la mémoire après l'opération ne lirait que le repos.
    """

    def __init__(
        self,
        *,
        horloge: Horloge | None = None,
        runtime_version: str = RUNTIME,
        vram_par_phase: dict[str, int] | None = None,
        ram_par_phase: dict[str, int] | None = None,
        load_ok: bool = True,
        unload_ok: bool = True,
        prompt_ok: bool = True,
        prompt_kwargs: dict | None = None,
        vram_repos_ok: bool = True,
        ram_repos_ok: bool = True,
        vram_echoue_apres: int | None = None,
        ram_echoue_apres: int | None = None,
        tours_par_operation: int = 3,
    ) -> None:
        self.horloge = horloge or Horloge()
        self.runtime_version = runtime_version
        self.vram_par_phase = vram_par_phase or {
            cal.PHASE_REDUCED: PIC_VRAM_REDUIT, cal.PHASE_TARGET: PIC_VRAM_CIBLE,
        }
        self.ram_par_phase = ram_par_phase or {
            cal.PHASE_REDUCED: PIC_RAM_REDUIT, cal.PHASE_TARGET: PIC_RAM_CIBLE,
        }
        self.load_ok = load_ok
        self.unload_ok = unload_ok
        self.prompt_ok = prompt_ok
        self.prompt_kwargs = prompt_kwargs or {}
        self.vram_repos_ok = vram_repos_ok
        self.ram_repos_ok = ram_repos_ok
        self.vram_echoue_apres = vram_echoue_apres
        self.ram_echoue_apres = ram_echoue_apres
        self.tours = tours_par_operation

        self.phase: str | None = None
        self.appels = {"vram": 0, "ram": 0, "load": 0, "unload": 0, "prompt": 0, "sleep": 0}
        self.charges: list[cal.LoadRequest] = []
        self.decharges: list[str] = []
        self.vram_vues: list[int] = []

    # ── Sondes mémoire ────────────────────────────────────────────────────────

    async def read_vram(self) -> cal.MemoryReading:
        self.appels["vram"] += 1
        if not self.vram_repos_ok:
            return cal.MemoryReading(ok=False, detail="nvidia-smi introuvable")
        if self.vram_echoue_apres is not None and self.appels["vram"] > self.vram_echoue_apres:
            return cal.MemoryReading(ok=False, detail="nvidia-smi n'a pas répondu")
        occupe = self.vram_par_phase.get(self.phase, IDLE_VRAM) if self.phase else IDLE_VRAM
        self.vram_vues.append(occupe)
        return cal.MemoryReading(ok=True, used_bytes=occupe, total_bytes=46068 * 1024 * 1024)

    async def read_ram(self) -> cal.MemoryReading:
        self.appels["ram"] += 1
        if not self.ram_repos_ok:
            return cal.MemoryReading(ok=False, detail="/proc/meminfo illisible")
        if self.ram_echoue_apres is not None and self.appels["ram"] > self.ram_echoue_apres:
            return cal.MemoryReading(ok=False, detail="/proc/meminfo illisible")
        occupe = self.ram_par_phase.get(self.phase, IDLE_RAM) if self.phase else IDLE_RAM
        return cal.MemoryReading(ok=True, used_bytes=occupe, total_bytes=768 * GIB)

    # ── Cycle de vie du modèle ────────────────────────────────────────────────

    async def load_model(self, request: cal.LoadRequest) -> cal.LoadOutcome:
        self.appels["load"] += 1
        self.charges.append(request)
        if not self.load_ok:
            return cal.LoadOutcome(ok=False, detail="VRAM insuffisante")
        self.phase = request.phase
        for _ in range(self.tours):
            await asyncio.sleep(0)
        self.horloge.avancer(12.0)
        self.phase = None
        return cal.LoadOutcome(ok=True, runtime_version=self.runtime_version)

    async def unload_model(self, model_id: str) -> cal.UnloadOutcome:
        self.appels["unload"] += 1
        self.decharges.append(model_id)
        self.phase = None
        if not self.unload_ok:
            return cal.UnloadOutcome(ok=False, detail="le processus llama-server ne rend pas la main")
        return cal.UnloadOutcome(ok=True)

    async def run_prompt(self, model_id: str, prompt: str) -> cal.PromptOutcome:
        self.appels["prompt"] += 1
        if not self.prompt_ok:
            return cal.PromptOutcome(ok=False, detail="502 depuis llama-server")
        # La phase est celle du dernier chargement : le prompt occupe le GPU.
        self.phase = self.charges[-1].phase if self.charges else None
        for _ in range(self.tours):
            await asyncio.sleep(0)
        self.phase = None
        defauts = {
            "ok": True, "ttft_ms": 180, "prompt_tokens": 24, "prompt_seconds": 0.12,
            "generation_tokens": 64, "generation_seconds": 1.6,
        }
        defauts.update(self.prompt_kwargs)
        return cal.PromptOutcome(**defauts)

    async def sleep(self, secondes: float) -> None:
        self.appels["sleep"] += 1
        self.horloge.avancer(secondes)
        await asyncio.sleep(0)

    # ── Commodités ────────────────────────────────────────────────────────────

    def bundle(self) -> cal.CalibrationProbes:
        return cal.CalibrationProbes(
            read_vram=self.read_vram,
            read_ram=self.read_ram,
            load_model=self.load_model,
            unload_model=self.unload_model,
            run_prompt=self.run_prompt,
            sleep=self.sleep,
        )

    @property
    def charges_non_dechargees(self) -> int:
        reussis = sum(1 for _ in self.charges) if self.load_ok else 0
        return reussis - len(self.decharges)


# ── Fabriques ─────────────────────────────────────────────────────────────────

def _params(**kwargs) -> cal.CalibrationParams:
    defauts = {"ctx_size": 8192, "parallel": 4, "reduced_ctx_size": 512, "reduced_parallel": 1}
    defauts.update(kwargs)
    return cal.CalibrationParams(**defauts)


def _options(sondes: Sondes, report_dir: Path, **kwargs) -> cal.CalibrationOptions:
    defauts = {
        "probes": sondes.bundle(),
        "runtime_version": RUNTIME,
        "hardware_fingerprint": cal.hardware_fingerprint([GPU_L40S_REEL]),
        "report_dir": report_dir,
        "params": {MODELE: _params()},
        "sample_interval_seconds": 0.5,
    }
    defauts.update(kwargs)
    return cal.CalibrationOptions(**defauts)


def _contexte(tmp_path: Path, mode: ex.ExecutionMode = ex.ExecutionMode.APPLY, horloge=None):
    return ex.ExecutionContext(
        mode,
        allowed_roots=(tmp_path,),
        monotonic=horloge or Horloge(),
        now=lambda: "2026-08-01T10:00:00Z",
    )


def _step(order: int = 1, target: str = MODELE) -> sc.PlanStep:
    return sc.PlanStep(
        order=order,
        action=sc.ACTION_CALIBRATE_MODEL,
        target=target,
        detail=f"calibrer {target} par chargement réel",
    )


def _identity(**kwargs) -> cal.CalibrationIdentity:
    defauts = {
        "model_id": MODELE,
        "runtime_version": RUNTIME,
        "hardware_fingerprint": cal.hardware_fingerprint([GPU_L40S_REEL]),
        "params_fingerprint": _params().fingerprint(),
    }
    defauts.update(kwargs)
    return cal.CalibrationIdentity(**defauts)


def _executer(sondes: Sondes, tmp_path: Path, *, mode=ex.ExecutionMode.APPLY,
              step: sc.PlanStep | None = None, **kwargs) -> ex.StepResult:
    """Exécute l'étape de calibration et rend son résultat. Aucun I/O hors tmp_path."""
    options = _options(sondes, tmp_path / "rapports", **kwargs)
    executeur = cal.make_executor(options)
    contexte = _contexte(tmp_path, mode, horloge=sondes.horloge)
    return asyncio.run(executeur(step or _step(), contexte))


def _calibrer(sondes: Sondes, tmp_path: Path, **kwargs) -> cal.CalibrationReport:
    options = _options(sondes, tmp_path / "rapports", **kwargs)
    return asyncio.run(cal.calibrate(MODELE, options, _contexte(tmp_path, horloge=sondes.horloge)))


# ══ Contrat consommateur ══════════════════════════════════════════════════════

def test_le_bloc_de_preuve_satisfait_le_contrat_du_consommateur(tmp_path):
    """
    AUT-007 n'active un modèle que sur `ActivationProof`, et son volet de
    calibration déclare les clés qu'il exige. Ce test-ci ne compare pas deux
    listes écrites à la main : il prend le jeu de clés du CONSOMMATEUR et le
    confronte à ce que ce module publie RÉELLEMENT.

    Sans lui, retirer une clé du bloc ne faisait tomber aucun test ici — le
    défaut ne serait apparu qu'à l'activation, sur un hôte de production.
    """
    bloc = _calibrer(Sondes(), tmp_path).to_dict()["calibration"]
    assert rw.CALIBRATION_PROOF_KEYS <= set(bloc), sorted(
        rw.CALIBRATION_PROOF_KEYS - set(bloc)
    )
    # Contrôle positif : le jeu de clés du consommateur n'est pas vide, et la
    # preuve construite depuis la projection est acceptée telle quelle.
    assert rw.CALIBRATION_PROOF_KEYS
    preuve = rw.CalibrationProof.from_mapping(
        {k: bloc[k] for k in rw.CALIBRATION_PROOF_KEYS}
    )
    assert preuve.model_id == MODELE


# ══ Empreintes ════════════════════════════════════════════════════════════════

def test_empreinte_materielle_insensible_a_l_ordre_des_cartes():
    a = cal.hardware_fingerprint([GPU_L40S_REEL, GPU_L40S_NOMINAL])
    b = cal.hardware_fingerprint([GPU_L40S_NOMINAL, GPU_L40S_REEL])
    assert a == b
    assert cal.is_fingerprint(a)


def test_empreinte_materielle_distingue_48_go_nominaux_de_45_go_reels():
    """§0.9 : le nominal commercial n'est pas la VRAM utilisable."""
    assert cal.hardware_fingerprint([GPU_L40S_REEL]) != cal.hardware_fingerprint([GPU_L40S_NOMINAL])


def test_empreinte_materielle_ignore_l_uuid_de_la_carte():
    """Remplacer une L40S par une L40S identique ne périme pas une mesure."""
    avec_uuid = dict(GPU_L40S_REEL, uuid="GPU-1111", index=0)
    assert cal.hardware_fingerprint([avec_uuid]) == cal.hardware_fingerprint([GPU_L40S_REEL])


def test_empreinte_materielle_change_avec_le_pilote():
    autre = dict(GPU_L40S_REEL, driver_version="555.42.02")
    assert cal.hardware_fingerprint([autre]) != cal.hardware_fingerprint([GPU_L40S_REEL])


def test_empreinte_materielle_refuse_une_liste_vide():
    with pytest.raises(cal.CalibrationError, match="aucun GPU"):
        cal.hardware_fingerprint([])


def test_empreinte_des_parametres_ignore_la_passe_reduite():
    """La passe réduite est un échafaudage de diagnostic, pas ce qui sera servi."""
    a = _params(reduced_ctx_size=512).fingerprint()
    b = _params(reduced_ctx_size=256).fingerprint()
    assert a == b
    # Contrôle positif : l'empreinte n'est pas constante pour autant.
    assert _params(ctx_size=16384).fingerprint() != a


def test_empreinte_des_parametres_change_avec_le_cache_kv():
    assert _params(cache_type_k="q8_0").fingerprint() != _params().fingerprint()


def test_empreinte_des_parametres_est_canonique_avec_le_registre_aut_007():
    """
    Une calibration ne sert à rien si l'écrivain refuse ensuite son empreinte.

    Ce test relie les deux consommateurs réels : ajouter un paramètre de service
    d'un seul côté doit le faire tomber avant tout déploiement GPU.
    """
    params = _params(
        batch_size=2048,
        ubatch_size=256,
        threads=6,
        threads_http=3,
        cpu_moe=True,
        flash_attention=True,
        n_gpu_layers=999,
    )
    assert params.fingerprint() == rw.params_fingerprint(params.target())


def test_parametres_refusent_une_passe_reduite_plus_grande_que_la_cible():
    with pytest.raises(cal.CalibrationError, match="reduced_ctx_size"):
        _params(ctx_size=512, reduced_ctx_size=4096)


# ══ Réutilisabilité — §9, décision testable ═══════════════════════════════════

def _preuve(identity: cal.CalibrationIdentity | None = None) -> cal.CalibrationProof:
    return cal.CalibrationProof(
        identity=identity or _identity(),
        idle_vram_bytes=IDLE_VRAM,
        peak_vram_bytes=PIC_VRAM_CIBLE,
        peak_ram_bytes=PIC_RAM_CIBLE,
        load_seconds=1.5,
        safety_margin=0.10,
        measured_at="2026-08-01T10:00:00Z",
    )


def test_une_mesure_du_meme_triplet_est_reutilisable():
    verdict = cal.evaluate_reuse(_preuve(), _identity())
    assert verdict.reusable is True
    assert verdict.divergences == ()


def test_une_mesure_prise_sur_un_autre_gpu_est_refusee_et_le_dit():
    attendu = _identity(hardware_fingerprint=cal.hardware_fingerprint([GPU_L40S_NOMINAL]))
    verdict = cal.evaluate_reuse(_preuve(), attendu)
    assert verdict.reusable is False
    assert verdict.divergences == ("hardware_fingerprint",)
    assert "matériel" in verdict.message


def test_une_mesure_prise_avec_un_autre_build_est_refusee_et_le_dit():
    verdict = cal.evaluate_reuse(_preuve(), _identity(runtime_version="llama-server b9999"))
    assert verdict.reusable is False
    assert verdict.divergences == ("runtime_version",)
    assert "runtime" in verdict.message


def test_une_mesure_prise_avec_d_autres_parametres_est_refusee_et_le_dit():
    attendu = _identity(params_fingerprint=_params(ctx_size=65536).fingerprint())
    verdict = cal.evaluate_reuse(_preuve(), attendu)
    assert verdict.reusable is False
    assert verdict.divergences == ("params_fingerprint",)
    assert "paramètres" in verdict.message


def test_le_verdict_nomme_les_trois_empreintes_quand_les_trois_divergent():
    attendu = _identity(
        runtime_version="llama-server b9999",
        hardware_fingerprint=cal.hardware_fingerprint([GPU_L40S_NOMINAL]),
        params_fingerprint=_params(ctx_size=65536).fingerprint(),
    )
    verdict = cal.evaluate_reuse(_preuve(), attendu)
    assert set(verdict.divergences) == {
        "runtime_version", "hardware_fingerprint", "params_fingerprint",
    }
    for mot in ("matériel", "runtime", "paramètres"):
        assert mot in verdict.message


def test_une_mesure_d_un_autre_modele_est_refusee():
    verdict = cal.evaluate_reuse(_preuve(), _identity(model_id="qwen3-8b"))
    assert verdict.reusable is False
    assert verdict.divergences == ("model_id",)


# ══ Échantillonnage — pendant la charge, borné, injectable ════════════════════

def test_les_pics_sont_releves_pendant_la_charge_pas_apres(tmp_path):
    """
    Le double n'expose le pic que TANT QUE l'opération est en cours.

    Un module qui relèverait la mémoire une fois l'opération terminée ne lirait
    que le repos : le pic mesuré serait `IDLE_VRAM`, et ce test tomberait.
    """
    sondes = Sondes()
    rapport = _calibrer(sondes, tmp_path)
    assert rapport.peak_vram_bytes == PIC_VRAM_CIBLE
    assert rapport.peak_vram_bytes > rapport.idle_vram_bytes
    assert rapport.peak_ram_bytes == PIC_RAM_CIBLE


def test_l_echantillonnage_est_borne_par_le_budget_de_temps():
    sondes = Sondes()
    horloge = sondes.horloge
    echantillonneur = cal.PeakSampler(
        sondes.bundle(), monotonic=horloge,
        interval_seconds=1.0, budget_seconds=3.0, max_samples=10_000,
    )

    async def interminable():
        for _ in range(10_000):
            await asyncio.sleep(0)

    asyncio.run(echantillonneur.during(interminable()))
    assert echantillonneur.rounds <= 5
    assert any("budget" in raison for raison in echantillonneur.stop_reasons())


def test_l_echantillonnage_est_borne_par_le_plafond_d_echantillons():
    """Une horloge qui n'avance pas rendrait la borne temporelle inopérante."""
    sondes = Sondes()
    gelee = Horloge()
    sondes.horloge = gelee

    async def sleep_sans_horloge(_secondes: float) -> None:
        await asyncio.sleep(0)

    bundle = cal.CalibrationProbes(
        read_vram=sondes.read_vram, read_ram=sondes.read_ram,
        load_model=sondes.load_model, unload_model=sondes.unload_model,
        run_prompt=sondes.run_prompt, sleep=sleep_sans_horloge,
    )
    echantillonneur = cal.PeakSampler(
        bundle, monotonic=gelee, interval_seconds=1.0, budget_seconds=1e9, max_samples=3,
    )

    async def interminable():
        for _ in range(10_000):
            await asyncio.sleep(0)

    asyncio.run(echantillonneur.during(interminable()))
    assert echantillonneur.rounds == 3
    assert any("plafond" in raison for raison in echantillonneur.stop_reasons())


def test_un_tour_d_echantillonnage_est_toujours_effectue():
    """Une opération qui rend la main aussitôt doit quand même produire un relevé."""
    sondes = Sondes()
    echantillonneur = cal.PeakSampler(sondes.bundle(), monotonic=sondes.horloge)

    async def immediate():
        return "fini"

    assert asyncio.run(echantillonneur.during(immediate())) == "fini"
    assert echantillonneur.rounds >= 1
    assert echantillonneur.peak_vram_bytes is not None


def test_l_echantillonneur_arrete_la_boucle_meme_si_l_operation_leve():
    sondes = Sondes()
    echantillonneur = cal.PeakSampler(sondes.bundle(), monotonic=sondes.horloge)

    async def qui_leve():
        raise RuntimeError("chargement interrompu")

    with pytest.raises(RuntimeError):
        asyncio.run(echantillonneur.during(qui_leve()))
    assert echantillonneur.rounds >= 1


@pytest.mark.parametrize("kwargs", [
    {"interval_seconds": 0.0},
    {"budget_seconds": 0.0},
    {"max_samples": 0},
])
def test_l_echantillonneur_refuse_des_bornes_inexploitables(kwargs):
    sondes = Sondes()
    with pytest.raises(cal.CalibrationError):
        cal.PeakSampler(sondes.bundle(), monotonic=sondes.horloge, **kwargs)


# ══ Séquence de §9 ════════════════════════════════════════════════════════════

def test_les_deux_passes_sont_executees_dans_l_ordre_de_la_section_9(tmp_path):
    sondes = Sondes()
    rapport = _calibrer(sondes, tmp_path)
    assert [c.phase for c in sondes.charges] == [cal.PHASE_REDUCED, cal.PHASE_TARGET]
    assert (sondes.charges[0].ctx_size, sondes.charges[0].parallel) == (512, 1)
    assert (sondes.charges[1].ctx_size, sondes.charges[1].parallel) == (8192, 4)
    assert [p.phase for p in rapport.passes] == [cal.PHASE_REDUCED, cal.PHASE_TARGET]


def test_un_prompt_court_est_envoye_a_chaque_passe(tmp_path):
    sondes = Sondes()
    _calibrer(sondes, tmp_path)
    assert sondes.appels["prompt"] == 2


def test_le_maximum_observe_est_conserve_pas_la_derniere_passe(tmp_path):
    """
    §9 étape 6. Le double fait ici piquer la passe RÉDUITE plus haut que la cible.

    Une implémentation qui garderait simplement la dernière passe rendrait
    `PIC_VRAM_CIBLE` et ce test tomberait.
    """
    sondes = Sondes(vram_par_phase={
        cal.PHASE_REDUCED: 20 * GIB, cal.PHASE_TARGET: 12 * GIB,
    })
    rapport = _calibrer(sondes, tmp_path)
    assert rapport.peak_vram_bytes == 20 * GIB
    assert rapport.proof().measured_vram_gb == 20.0


def test_ttft_et_debits_viennent_de_la_passe_cible(tmp_path):
    """Les débits d'un contexte de 512 jetons ne décrivent pas le service réel."""
    sondes = Sondes()
    rapport = _calibrer(sondes, tmp_path)
    assert rapport.target_pass.phase == cal.PHASE_TARGET
    document = rapport.to_dict()
    assert document["ttft_ms"] == rapport.target_pass.ttft_ms
    assert document["generation_tokens_per_second"] == 40.0


def test_le_rapport_porte_toutes_les_empreintes_de_la_section_9(tmp_path):
    sondes = Sondes()
    document = _calibrer(sondes, tmp_path).to_dict()
    calibration = document["calibration"]
    for cle in (
        "model_id", "runtime_version", "hardware_fingerprint", "params_fingerprint",
        "idle_vram_gb", "peak_vram_gb", "peak_ram_gb", "measured_at",
    ):
        assert cle in calibration, cle
    for cle in ("load_seconds", "ttft_ms",
                "prompt_tokens_per_second", "generation_tokens_per_second"):
        assert cle in document, cle
    assert document["kind"] == cal.CALIBRATION_KIND
    assert calibration["measured_at"] == "2026-08-01T10:00:00Z"


def test_le_rapport_publie_les_noms_de_debits_litteraux_de_la_section_9(tmp_path):
    """
    §9 prescrit `prompt_tokens_per_second` et `generation_tokens_per_second`.

    Ces noms ont été un temps impubliables : `schema._SECRET_KEY_RE` traitait
    toute clé contenant « TOKEN » comme sensible, et le rendu refusait alors le
    document. Le défaut est corrigé à sa source ; le rapport suit désormais §9
    à la lettre, sans alias. Ce test verrouille les deux faces — les noms sont
    présents ET le document reste publiable.
    """
    document = _document(tmp_path)
    assert document["prompt_tokens_per_second"] == 200.0
    assert document["generation_tokens_per_second"] == 40.0
    assert sc.find_secret_leaks(document) == ()
    # Contrôle positif : le détecteur voit toujours une vraie fuite ici.
    assert sc.find_secret_leaks({**document, "hf_token": "x"}) != ()


def test_le_rapport_publie_la_sequence_litterale_des_neuf_etapes(tmp_path):
    sondes = Sondes()
    document = _calibrer(sondes, tmp_path).to_dict()
    assert len(document["sequence"]) == 9
    assert document["sequence"][0].startswith("relever RAM/VRAM au repos")
    assert "sans l'appliquer silencieusement" in document["sequence"][8]


# ══ Marge — explicite, configurable, contestable ══════════════════════════════

def test_la_valeur_brute_et_la_valeur_proposee_sont_publiees_toutes_les_deux(tmp_path):
    sondes = Sondes()
    calibration = _calibrer(sondes, tmp_path).to_dict()["calibration"]
    assert calibration["measured_vram_gb"] == 12.0
    assert calibration["proposed_vram_gb"] == 13.2
    assert calibration["safety_margin"] == cal.DEFAULT_SAFETY_MARGIN
    # Le calcul en toutes lettres : sans lui, l'opérateur ne peut pas contester.
    assert "12.00" in calibration["margin_formula"]
    assert "13.20" in calibration["margin_formula"]


def test_une_marge_nulle_propose_exactement_la_valeur_mesuree(tmp_path):
    sondes = Sondes()
    calibration = _calibrer(sondes, tmp_path, safety_margin=0.0).to_dict()["calibration"]
    assert calibration["proposed_vram_gb"] == 12.0


def test_la_marge_est_configurable_et_change_la_proposition(tmp_path):
    sondes = Sondes()
    calibration = _calibrer(sondes, tmp_path, safety_margin=0.25).to_dict()["calibration"]
    assert calibration["proposed_vram_gb"] == 15.0


def test_la_proposition_arrondit_vers_le_haut_jamais_vers_le_bas():
    preuve = cal.CalibrationProof(
        identity=_identity(),
        idle_vram_bytes=IDLE_VRAM,
        # 10,05 Gio × 1,10 = 11,055 → 11,06 et non 11,05.
        peak_vram_bytes=int(10.05 * GIB),
        peak_ram_bytes=PIC_RAM_CIBLE,
        load_seconds=1.5,
        safety_margin=0.10,
        measured_at="2026-08-01T10:00:00Z",
    )
    assert preuve.measured_vram_gb == 10.05
    assert preuve.proposed_vram_gb == 11.06


def test_les_options_refusent_une_marge_negative(tmp_path):
    sondes = Sondes()
    with pytest.raises(cal.CalibrationError, match="safety_margin"):
        _options(sondes, tmp_path, safety_margin=-0.1)


# ══ Fail-closed — une mesure ratée n'est jamais optimiste ═════════════════════

def test_un_pic_non_relevable_fait_echouer_la_calibration(tmp_path):
    """
    Le relevé au repos réussit, puis la sonde VRAM se tait. Aucun pic n'est
    relevé : la calibration ÉCHOUE, elle ne conclut pas au repos.
    """
    sondes = Sondes(vram_echoue_apres=1)
    resultat = _executer(sondes, tmp_path)
    assert resultat.status == ex.STEP_FAILED
    assert "pic" in resultat.error


def test_une_calibration_ratee_ne_propose_aucune_valeur_vram(tmp_path):
    """
    Contrôle d'ABSENCE avec son contrôle positif : la même exécution réussie
    porte bien une proposition, donc l'assertion sait voir quelque chose.
    """
    rate = _executer(Sondes(vram_echoue_apres=1), tmp_path / "echec")
    assert rate.status == ex.STEP_FAILED
    assert "proposed_vram_gb" not in json.dumps(rate.evidence)
    assert not any("estim" in cle for cle in rate.evidence)

    reussi = _executer(Sondes(), tmp_path / "succes")
    assert reussi.status == ex.STEP_DONE
    assert "proposed_vram_gb" in json.dumps(reussi.evidence)


def test_un_repos_non_relevable_fait_echouer_avant_tout_chargement(tmp_path):
    sondes = Sondes(vram_repos_ok=False)
    resultat = _executer(sondes, tmp_path)
    assert resultat.status == ex.STEP_FAILED
    assert sondes.appels["load"] == 0


def test_une_ram_au_repos_non_relevable_fait_echouer(tmp_path):
    resultat = _executer(Sondes(ram_repos_ok=False), tmp_path)
    assert resultat.status == ex.STEP_FAILED


def test_un_runtime_different_de_celui_annonce_fait_echouer(tmp_path):
    """Une preuve étiquetée d'un build qui ne l'a pas produite serait fausse."""
    sondes = Sondes(runtime_version="llama-server b0001 (ROCm)")
    resultat = _executer(sondes, tmp_path)
    assert resultat.status == ex.STEP_FAILED
    assert "runtime" in resultat.error


def test_un_chargement_impossible_fait_echouer(tmp_path):
    resultat = _executer(Sondes(load_ok=False), tmp_path)
    assert resultat.status == ex.STEP_FAILED
    assert "chargement" in resultat.error


def test_un_prompt_sans_reponse_fait_echouer(tmp_path):
    resultat = _executer(Sondes(prompt_ok=False), tmp_path)
    assert resultat.status == ex.STEP_FAILED


@pytest.mark.parametrize("champ,valeur", [
    ("ttft_ms", 0),
    ("generation_tokens", 0),
    ("generation_seconds", 0.0),
    ("prompt_tokens", 0),
])
def test_un_debit_non_mesurable_fait_echouer_plutot_que_de_publier_zero(tmp_path, champ, valeur):
    resultat = _executer(Sondes(prompt_kwargs={champ: valeur}), tmp_path)
    assert resultat.status == ex.STEP_FAILED


def test_un_modele_sans_parametres_declares_fait_echouer(tmp_path):
    """Aucun paramètre par défaut : une mesure hors du service réel n'apprend rien."""
    resultat = _executer(Sondes(), tmp_path, step=_step(target="modele-inconnu"))
    assert resultat.status == ex.STEP_FAILED
    assert "paramètre" in resultat.error


# ══ Cycle de vie — rien ne reste chargé ═══════════════════════════════════════

def test_le_modele_est_decharge_apres_chaque_passe(tmp_path):
    sondes = Sondes()
    _executer(sondes, tmp_path)
    assert sondes.appels["unload"] == 2
    assert sondes.charges_non_dechargees == 0


def test_le_modele_est_decharge_meme_quand_la_calibration_echoue(tmp_path):
    """`CLAUDE.md` : un modèle laissé chargé par un diagnostic est une fuite de VRAM."""
    sondes = Sondes(prompt_ok=False)
    resultat = _executer(sondes, tmp_path)
    assert resultat.status == ex.STEP_FAILED
    # Contrôle positif : un chargement a bien eu lieu, donc le déchargement
    # observé n'est pas trivialement vrai.
    assert sondes.appels["load"] == 1
    assert sondes.appels["unload"] == 1
    assert sondes.charges_non_dechargees == 0


def test_un_dechargement_rate_fait_echouer_la_calibration(tmp_path):
    sondes = Sondes(unload_ok=False)
    resultat = _executer(sondes, tmp_path)
    assert resultat.status == ex.STEP_FAILED
    assert "déchargé" in resultat.error


# ══ Idempotence ═══════════════════════════════════════════════════════════════

def test_une_mesure_reutilisable_rend_already_satisfied_sans_recharger(tmp_path):
    premier = _executer(Sondes(), tmp_path)
    assert premier.status == ex.STEP_DONE

    second_sondes = Sondes()
    second = _executer(second_sondes, tmp_path)
    assert second.status == ex.STEP_ALREADY_SATISFIED
    assert second_sondes.appels["load"] == 0
    assert second_sondes.appels["vram"] == 0
    assert second.evidence["reused"] is True
    assert second.evidence["calibration"]["proposed_vram_gb"] == 13.2


def test_attestation_hote_precede_toute_reutilisation_de_preuve(tmp_path):
    assert _executer(Sondes(), tmp_path).status == ex.STEP_DONE
    calls = []

    async def runtime_changed(identity):
        calls.append(identity)
        raise cal.CalibrationError("runtime courant changé depuis la preuve")

    sondes = Sondes()
    result = _executer(
        sondes, tmp_path, validate_environment=runtime_changed
    )
    assert result.status == ex.STEP_FAILED
    assert "runtime courant changé" in result.error
    assert len(calls) == 1
    assert sondes.appels["load"] == 0
    assert result.evidence.get("reused") is not True


def test_attestation_hote_protege_aussi_une_nouvelle_calibration(tmp_path):
    calls = []

    async def validate(identity):
        calls.append(identity)

    result = _executer(
        Sondes(), tmp_path, validate_environment=validate
    )
    assert result.status == ex.STEP_DONE
    assert len(calls) == 1


def test_un_autre_gpu_force_une_recalibration_et_nomme_la_divergence(tmp_path):
    assert _executer(Sondes(), tmp_path).status == ex.STEP_DONE

    autres_sondes = Sondes()
    second = _executer(
        autres_sondes, tmp_path,
        hardware_fingerprint=cal.hardware_fingerprint([GPU_L40S_NOMINAL]),
    )
    assert second.status == ex.STEP_DONE
    assert autres_sondes.appels["load"] == 2
    codes = {f.code for f in second.findings}
    assert "mesure_non_reutilisable" in codes
    message = " ".join(f.message for f in second.findings)
    assert "matériel" in message


def test_deux_empreintes_differentes_coexistent_dans_le_repertoire(tmp_path):
    _executer(Sondes(), tmp_path)
    _executer(Sondes(), tmp_path,
              hardware_fingerprint=cal.hardware_fingerprint([GPU_L40S_NOMINAL]))
    rapports = sorted((tmp_path / "rapports").glob("calibration-*.json"))
    assert len(rapports) == 2


def test_un_rapport_illisible_n_est_jamais_reutilise(tmp_path):
    dossier = tmp_path / "rapports"
    dossier.mkdir(parents=True)
    identity = _identity()
    (dossier / cal.report_filename(identity)).write_text("{ pas du json", encoding="utf-8")

    sondes = Sondes()
    resultat = _executer(sondes, tmp_path)
    assert resultat.status == ex.STEP_DONE
    assert sondes.appels["load"] == 2
    assert "rapport_illisible" in {f.code for f in resultat.findings}


def test_un_rapport_dont_la_proposition_a_ete_abaissee_n_est_pas_reutilise(tmp_path):
    """La preuve est recoupée à la relecture : la retoucher la rend inutilisable."""
    assert _executer(Sondes(), tmp_path).status == ex.STEP_DONE
    chemin = next((tmp_path / "rapports").glob("calibration-*.json"))
    document = json.loads(chemin.read_text(encoding="utf-8"))
    document["calibration"]["proposed_vram_gb"] = 4.0
    chemin.write_text(json.dumps(document), encoding="utf-8")

    sondes = Sondes()
    resultat = _executer(sondes, tmp_path)
    assert resultat.status == ex.STEP_DONE
    assert sondes.appels["load"] == 2


# ══ Simulation ════════════════════════════════════════════════════════════════

def test_la_simulation_ne_charge_rien_et_n_interroge_aucune_sonde(tmp_path):
    sondes = Sondes()
    resultat = _executer(sondes, tmp_path, mode=ex.ExecutionMode.DRY_RUN)
    assert resultat.status == ex.STEP_WOULD_APPLY
    assert sondes.appels == {"vram": 0, "ram": 0, "load": 0, "unload": 0, "prompt": 0, "sleep": 0}

    # Contrôle positif : les mêmes compteurs bougent en application réelle.
    appliquees = Sondes()
    _executer(appliquees, tmp_path, mode=ex.ExecutionMode.APPLY)
    assert appliquees.appels["load"] == 2
    assert appliquees.appels["vram"] > 0


def test_la_simulation_dit_ce_qui_serait_mesure_et_combien_de_temps(tmp_path):
    resultat = _executer(Sondes(), tmp_path, mode=ex.ExecutionMode.DRY_RUN)
    preuve = resultat.evidence
    assert preuve["kind"] == "simulation"
    assert len(preuve["sequence"]) == 9
    assert [p["phase"] for p in preuve["passes"]] == [cal.PHASE_REDUCED, cal.PHASE_TARGET]
    assert preuve["passes"][1]["ctx_size"] == 8192
    assert preuve["estimated_seconds"] == 300.0
    assert "s de GPU immobilisé" in resultat.summary


def test_la_simulation_n_ecrit_aucun_rapport(tmp_path):
    _executer(Sondes(), tmp_path, mode=ex.ExecutionMode.DRY_RUN)
    assert not list(tmp_path.rglob("calibration-*.json"))
    # Contrôle positif : l'application, elle, en écrit un au même endroit.
    _executer(Sondes(), tmp_path, mode=ex.ExecutionMode.APPLY)
    assert len(list(tmp_path.rglob("calibration-*.json"))) == 1


# ══ Mesurer et proposer, jamais appliquer ═════════════════════════════════════

def test_aucun_registre_n_est_ecrit_seul_le_rapport_separe_l_est(tmp_path):
    _executer(Sondes(), tmp_path)
    ecrits = [p for p in tmp_path.rglob("*") if p.is_file()]
    # Contrôle positif : le test sait voir un fichier, et il en voit exactement un.
    assert len(ecrits) == 1
    assert ecrits[0].name.startswith("calibration-")
    assert ecrits[0].suffix == ".json"
    assert not list(tmp_path.rglob("*.yaml")) and not list(tmp_path.rglob("*.yml"))


def test_l_evidence_declare_explicitement_que_le_registre_n_a_pas_ete_ecrit(tmp_path):
    resultat = _executer(Sondes(), tmp_path)
    assert resultat.evidence["registry_written"] is False
    assert resultat.evidence["calibration"]["applied"] is False
    assert "models.yaml" in resultat.summary


def test_la_proposition_est_portee_par_un_constat_lisible(tmp_path):
    resultat = _executer(Sondes(), tmp_path)
    constats = {f.code: f for f in resultat.findings}
    assert "vram_gb_propose" in constats
    assert "PAS été appliquée" in constats["vram_gb_propose"].message


def test_le_rapport_ecrit_est_relisible_comme_preuve(tmp_path):
    """La forme de la preuve est le contrat : un autre chantier doit la relire."""
    resultat = _executer(Sondes(), tmp_path)
    chemin = Path(resultat.evidence["report_path"])
    preuve = cal.load_proof_file(chemin)
    assert preuve.identity == _identity()
    assert preuve.measured_vram_gb == 12.0
    assert preuve.proposed_vram_gb == 13.2
    assert cal.evaluate_reuse(preuve, _identity()).reusable is True


# ══ Validation du document ════════════════════════════════════════════════════

def _document(tmp_path) -> dict:
    return _calibrer(Sondes(), tmp_path).to_dict()


def test_un_rapport_intact_est_valide(tmp_path):
    assert cal.validate_calibration_document(_document(tmp_path)) == ()


def test_une_proposition_abaissee_a_la_main_est_rejetee(tmp_path):
    document = _document(tmp_path)
    document["calibration"]["proposed_vram_gb"] = 6.0
    erreurs = cal.validate_calibration_document(document)
    assert any("proposed_vram_gb" in e for e in erreurs)


def test_un_pic_inferieur_au_maximum_des_passes_est_rejete(tmp_path):
    """§9 étape 6 : le maximum observé, pas une valeur plus commode."""
    document = _document(tmp_path)
    document["calibration"]["measured_vram_gb"] = 8.0
    document["calibration"]["peak_vram_gb"] = 8.0
    document["calibration"]["proposed_vram_gb"] = 8.8
    erreurs = cal.validate_calibration_document(document)
    assert any("maximum des passes" in e for e in erreurs)


def test_un_rapport_qui_se_declare_applique_est_rejete(tmp_path):
    document = _document(tmp_path)
    document["calibration"]["applied"] = True
    assert any("applied" in e for e in cal.validate_calibration_document(document))


def test_un_document_d_estimation_n_est_pas_relu_comme_une_mesure(tmp_path):
    document = _document(tmp_path)
    document["kind"] = "estimation"
    assert any("kind" in e for e in cal.validate_calibration_document(document))


def test_les_limites_de_la_mesure_sont_obligatoires(tmp_path):
    """Le pendant de `FACTEURS_IGNORES` : une mesure sans ses limites se lit mal."""
    document = _document(tmp_path)
    assert cal.validate_calibration_document(document) == ()  # contrôle positif
    document["limites_mesure"] = []
    assert any("limites_mesure" in e for e in cal.validate_calibration_document(document))


def test_le_rapport_publie_ce_que_la_mesure_ne_garantit_pas(tmp_path):
    document = _document(tmp_path)
    joint = " ".join(document["limites_mesure"])
    assert "ÉCHANTILLONNÉ" in joint
    assert "pas une preuve de non-dépassement" in joint


def test_une_empreinte_mal_formee_est_rejetee(tmp_path):
    document = _document(tmp_path)
    document["calibration"]["hardware_fingerprint"] = "sha256:court"
    assert any("hardware_fingerprint" in e for e in cal.validate_calibration_document(document))


def test_load_proof_file_refuse_un_document_incoherent(tmp_path):
    document = _document(tmp_path)
    document["calibration"]["proposed_vram_gb"] = 1.0
    chemin = tmp_path / "bidon.json"
    chemin.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(cal.CalibrationError):
        cal.load_proof_file(chemin)


def test_le_rendu_refuse_de_publier_un_rapport_qui_fuit(tmp_path):
    sondes = Sondes()
    options = _options(sondes, tmp_path, params={FAUX_TOKEN: _params()})
    rapport = asyncio.run(cal.calibrate(FAUX_TOKEN, options, _contexte(tmp_path, horloge=sondes.horloge)))
    with pytest.raises(sc.PlanError):
        cal.render_calibration_json(rapport)
    # Contrôle positif : un rapport ordinaire se rend sans lever.
    assert cal.render_calibration_json(_calibrer(Sondes(), tmp_path / "sain"))


# ══ Rendu humain ══════════════════════════════════════════════════════════════

def test_le_rendu_humain_montre_le_brut_la_proposition_et_les_limites(tmp_path):
    texte = cal.render_calibration_human(_calibrer(Sondes(), tmp_path))
    assert "VRAM au pic   : 12.00" in texte
    assert "vram_gb proposé : 13.20" in texte
    assert "CE QUE CETTE MESURE NE GARANTIT PAS" in texte
    assert "Aucune écriture n'a été faite dans models.yaml" in texte
    assert cal.PHASE_REDUCED in texte and cal.PHASE_TARGET in texte


# ══ Intégration au contrat d'exécution ════════════════════════════════════════

def _plan(steps: tuple[sc.PlanStep, ...]) -> ex.LoadedPlan:
    return ex.LoadedPlan(
        document={},
        steps=steps,
        fingerprint=ex.plan_fingerprint({"steps": [s.to_dict() for s in steps]}),
        generated_at="2026-08-01T09:00:00Z",
        mode="apply",
        origin="<test>",
    )


def test_register_executor_branche_l_action_de_calibration(tmp_path):
    registre = ex.ExecutorRegistry()
    cal.register_executor(registre, _options(Sondes(), tmp_path))
    assert sc.ACTION_CALIBRATE_MODEL in registre
    assert registre.missing_actions((_step(),)) == ()


def test_un_second_enregistrement_est_refuse(tmp_path):
    registre = ex.ExecutorRegistry()
    options = _options(Sondes(), tmp_path)
    cal.register_executor(registre, options)
    with pytest.raises(ex.ExecutionError):
        cal.register_executor(registre, options)


def test_le_journal_d_execution_est_publiable_apres_une_calibration(tmp_path):
    sondes = Sondes()
    registre = ex.ExecutorRegistry()
    cal.register_executor(registre, _options(sondes, tmp_path / "rapports"))
    contexte = _contexte(tmp_path, ex.ExecutionMode.APPLY, horloge=sondes.horloge)
    rapport = asyncio.run(ex.execute_plan(_plan((_step(),)), registre, contexte))

    assert rapport.verdict() == ex.VERDICT_OK
    assert rapport.exit_code() == ex.EXIT_OK
    # Le rendu refuse un journal qui fuit ou qui se contredit : qu'il passe
    # prouve que l'`evidence` produite ici est sérialisable et sans secret.
    publie = json.loads(ex.render_execution_json(rapport))
    assert publie["results"][0]["evidence"]["registry_written"] is False


def test_une_simulation_complete_sort_en_partiel(tmp_path):
    sondes = Sondes()
    registre = ex.ExecutorRegistry()
    cal.register_executor(registre, _options(sondes, tmp_path / "rapports"))
    contexte = _contexte(tmp_path, ex.ExecutionMode.DRY_RUN, horloge=sondes.horloge)
    rapport = asyncio.run(ex.execute_plan(_plan((_step(),)), registre, contexte))

    assert rapport.verdict() == ex.VERDICT_PARTIAL
    assert rapport.exit_code() == ex.EXIT_PARTIAL
    assert rapport.changed() is False
    assert sondes.appels["load"] == 0


def test_un_echec_de_calibration_arrete_la_sequence(tmp_path):
    registre = ex.ExecutorRegistry()
    cal.register_executor(registre, _options(Sondes(load_ok=False), tmp_path / "rapports"))
    registre.register(sc.ACTION_ENABLE_MODEL, _executeur_inerte())
    steps = (_step(1), sc.PlanStep(order=2, action=sc.ACTION_ENABLE_MODEL,
                                   target=MODELE, detail="activer"))
    contexte = _contexte(tmp_path, ex.ExecutionMode.APPLY)
    rapport = asyncio.run(ex.execute_plan(_plan(steps), registre, contexte))

    assert rapport.result(1).status == ex.STEP_FAILED
    assert rapport.result(2).status == ex.STEP_NOT_ATTEMPTED
    assert rapport.verdict() == ex.VERDICT_FAILED


def _executeur_inerte():
    async def executer(step, context):
        return ex.StepResult.for_step(step, status=ex.STEP_DONE, summary="activé")
    return executer


def test_le_repertoire_de_rapports_doit_etre_dans_les_racines_autorisees(tmp_path):
    """Fail-closed : hors des racines déclarées, rien n'est écrit."""
    sondes = Sondes()
    options = _options(sondes, tmp_path / "rapports")
    executeur = cal.make_executor(options)
    contexte = ex.ExecutionContext(
        ex.ExecutionMode.APPLY,
        allowed_roots=(tmp_path / "ailleurs",),
        monotonic=sondes.horloge,
        now=lambda: "2026-08-01T10:00:00Z",
    )
    resultat = asyncio.run(executeur(_step(), contexte))
    assert resultat.status == ex.STEP_FAILED
    assert not list(tmp_path.rglob("calibration-*.json"))
    # Le refus doit précéder la création de l'arborescence : un contrôle placé
    # après le `mkdir` laisserait des répertoires derrière lui hors des racines.
    assert not (tmp_path / "rapports").exists()
    # Contrôle positif : avec la bonne racine, le rapport est bien écrit.
    assert _executer(Sondes(), tmp_path).status == ex.STEP_DONE
    assert list(tmp_path.rglob("calibration-*.json"))
