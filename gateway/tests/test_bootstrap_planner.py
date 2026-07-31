"""
AUT-001 — régressions du planificateur (`bootstrap/planner.py`).

Le planificateur est le seul module qui connaît tous les producteurs. Ce sont
donc ses raccords qu'on teste ici, pas les producteurs eux-mêmes : chacun a déjà
sa propre suite. Trois familles d'invariants :

1. **le plan reste un plan** — aucune écriture, aucun téléchargement, et il
   existe même quand tout va mal, pour dire pourquoi ;
2. **l'ordre des étapes** — vérifier après avoir posé l'artefact ne protège de
   rien, activer avant de calibrer publie une capacité supposée ;
3. **LLMfit est un conseiller** — il peut réordonner des candidats approuvés, il
   ne peut ni en ajouter, ni en ressusciter un non épinglé, ni relever un budget.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from bootstrap import catalog as catalog_mod
from bootstrap import llmfit as llmfit_mod
from bootstrap import planner
from bootstrap import runtime_resolver as runtime_mod
from bootstrap import schema as sc

GENERATED_AT = "2026-07-31T12:00:00Z"
GIB = 1024 ** 3


# ── Fabriques ─────────────────────────────────────────────────────────────────

def _profile_document(**overrides) -> dict:
    """Profil §5 d'un hôte GPU confortable. Déclaré, donc reproductible en test."""
    document = {
        "os": "ubuntu",
        "os_version": "24.04",
        "arch": "x86_64",
        "cpu_model": "Intel Xeon Gold 6338",
        "cpu_flags": ["avx2", "avx512_vnni"],
        "ram_total_bytes": 256 * GIB,
        "ram_available_bytes": 200 * GIB,
        "disk_available_bytes": 2000 * GIB,
        "gpus": [{
            "index": 0,
            "uuid": "GPU-00000000-0000-0000-0000-000000000000",
            "vendor": "nvidia",
            "model": "NVIDIA L40S",
            "vram_total_bytes": 48301604864,
            "driver_version": "550.90.07",
            "compute_capability": "8.9",
        }],
        "backend_candidates": ["cuda12", "cpu"],
    }
    document.update(overrides)
    return document


def _release() -> runtime_mod.ReleasePolicy:
    return runtime_mod.ReleasePolicy(
        pinned_version="b6800", pinned_commit="a" * 40, security_floor_build=6600
    )


def _options(tmp_path, *, profile: dict | None = None, **overrides) -> planner.PlannerOptions:
    models_dir = tmp_path / "models"
    models_dir.mkdir(exist_ok=True)
    profile_path = tmp_path / "profil.json"
    profile_path.write_text(json.dumps(profile or _profile_document()), encoding="utf-8")

    base = {
        "generated_at": GENERATED_AT,
        "hardware_profile_path": profile_path,
        "models_dir": models_dir,
        "release_policy": _release(),
    }
    base.update(overrides)
    return planner.PlannerOptions(**base)


def _plan(options: planner.PlannerOptions) -> sc.BootstrapPlan:
    return asyncio.run(planner.build_plan(options))


def _actions(plan: sc.BootstrapPlan) -> list[str]:
    return [step.action for step in plan.steps]


def _advice(*candidates: str) -> llmfit_mod.LLMfitResult:
    return llmfit_mod.LLMfitResult(
        status="ok",
        summary="conseil consultatif — recommandation de test",
        source="llmfit",
        recommendation=llmfit_mod.LLMfitRecommendation(
            llmfit_version="0.6.1",
            candidates=tuple(llmfit_mod.LLMfitCandidate(candidate=c) for c in candidates),
        ),
    )


# ── 1. Le plan reste un plan ──────────────────────────────────────────────────

def test_le_plan_nominal_est_valide_et_applicable(tmp_path):
    """Contrôle positif de tout le fichier : le cas sain produit un plan sain."""
    plan = _plan(_options(tmp_path))
    assert sc.validate_plan_dict(plan.to_dict()) == ()
    assert plan.applicable is True
    assert plan.steps


def test_le_plan_n_ecrit_rien_sur_le_disque(tmp_path):
    """
    Un plan qui écrirait ne serait plus un plan. Le répertoire des modèles doit
    rester exactement tel qu'il était : c'est la promesse du jalon M1.
    """
    options = _options(tmp_path)
    avant = sorted(p.name for p in options.models_dir.iterdir())
    _plan(options)
    assert sorted(p.name for p in options.models_dir.iterdir()) == avant == []


def test_le_plan_ne_fuit_aucun_secret(tmp_path):
    document = _plan(_options(tmp_path)).to_dict()
    assert sc.find_secret_leaks(document) == ()
    # Contrôle positif : le détecteur appliqué à ce même document saurait voir
    # une fuite si on en injectait une.
    document["sections"][0]["data"]["hf_token"] = "hf_abcdefghijklmnopqrstuvwx"
    assert sc.find_secret_leaks(document)


def test_sans_politique_de_release_le_plan_existe_mais_bloque(tmp_path):
    """
    Le planificateur refuse d'inventer un numéro de build : il se propagerait
    dans tous les manifestes de provenance avec l'apparence d'un fait vérifié.
    """
    plan = _plan(_options(tmp_path, release_policy=None))
    assert plan.applicable is False
    assert "politique_de_release_absente" in [f.code for f in plan.blockers]
    # Aucune séquence n'est proposée : des téléchargements sans binaire capable
    # de servir les modèles inviteraient à n'exécuter que la moitié du plan.
    assert plan.steps == ()
    # Mais rien n'est caché — ce qui SERAIT retenu reste visible.
    assert plan.section(sc.SECTION_MODELS).data["retained"]
    # Le plan reste lisible et validable : bloqué n'est pas absent.
    assert sc.validate_plan_dict(plan.to_dict()) == ()
    assert "BLOQUEURS" in sc.render_human(plan)


def test_un_catalogue_illisible_bloque_sans_tuer_le_plan(tmp_path):
    absent = tmp_path / "catalogue-inexistant.yaml"
    plan = _plan(_options(tmp_path, catalog_path=absent))
    section = plan.section(sc.SECTION_CATALOG)
    assert section is not None and section.status == "fail"
    assert "catalogue_illisible" in [f.code for f in section.findings]
    # L'inventaire, lui, reste exploitable par l'opérateur qui diagnostique.
    assert plan.section(sc.SECTION_HARDWARE).status in ("ok", "warn")


def test_un_profil_materiel_illisible_est_une_erreur_d_entree(tmp_path):
    """
    Un profil explicitement demandé et introuvable n'est pas une situation à
    dégrader en silence : c'est une consigne qu'on ne peut pas honorer.
    """
    options = _options(tmp_path, hardware_profile_path=tmp_path / "nulle-part.json")
    with pytest.raises(planner.PlannerUsageError) as exc:
        _plan(options)
    assert "hardware-profile" in str(exc.value)


def test_un_profil_materiel_invalide_est_refuse(tmp_path):
    mauvais = tmp_path / "mauvais.json"
    mauvais.write_text(json.dumps(_profile_document(ram_available_bytes=999 * GIB)), encoding="utf-8")
    with pytest.raises(planner.PlannerUsageError):
        _plan(_options(tmp_path, hardware_profile_path=mauvais))


def test_toutes_les_sections_du_contrat_sont_produites(tmp_path):
    plan = _plan(_options(tmp_path))
    assert [s.name for s in plan.sections] == list(sc.SECTION_NAMES)


# ── 2. L'ordre des étapes ─────────────────────────────────────────────────────

def test_la_sequence_suit_l_ordre_impose(tmp_path):
    plan = _plan(_options(tmp_path))
    assert _actions(plan) == [
        sc.ACTION_INSTALL_RUNTIME,
        sc.ACTION_ACCEPT_LICENSE,
        sc.ACTION_DOWNLOAD_MODEL,
        sc.ACTION_VERIFY_ARTIFACT,
        sc.ACTION_WRITE_REGISTRY,
        sc.ACTION_CALIBRATE_MODEL,
        sc.ACTION_ENABLE_MODEL,
        sc.ACTION_WARMUP_MODEL,
        sc.ACTION_SMOKE_TEST,
    ]


def test_la_verification_suit_immediatement_le_telechargement(tmp_path):
    """Contrôler l'empreinte après avoir mis le modèle en service ne protège de rien."""
    actions = _actions(_plan(_options(tmp_path)))
    assert actions.index(sc.ACTION_VERIFY_ARTIFACT) == actions.index(sc.ACTION_DOWNLOAD_MODEL) + 1


def test_l_activation_suit_la_calibration(tmp_path):
    """Activer avant de calibrer publierait une capacité supposée (AUT-007)."""
    actions = _actions(_plan(_options(tmp_path)))
    assert actions.index(sc.ACTION_ENABLE_MODEL) > actions.index(sc.ACTION_CALIBRATE_MODEL)
    assert actions.index(sc.ACTION_WARMUP_MODEL) > actions.index(sc.ACTION_ENABLE_MODEL)


def test_la_licence_est_acceptee_avant_tout_telechargement(tmp_path):
    actions = _actions(_plan(_options(tmp_path)))
    assert actions.index(sc.ACTION_ACCEPT_LICENSE) < actions.index(sc.ACTION_DOWNLOAD_MODEL)


def test_le_registre_est_ecrit_desactive(tmp_path):
    plan = _plan(_options(tmp_path))
    step = next(s for s in plan.steps if s.action == sc.ACTION_WRITE_REGISTRY)
    assert "enabled: false" in step.detail


def test_le_smoke_test_est_unique_et_final_meme_avec_plusieurs_modeles(tmp_path):
    """
    La recette traverse le chemin public (§10) : elle valide la chaîne, pas un
    modèle. La répliquer par modèle serait un contresens.
    """
    plan = _plan(_options(tmp_path, max_models=2))
    actions = _actions(plan)
    assert actions.count(sc.ACTION_SMOKE_TEST) == 1
    assert actions[-1] == sc.ACTION_SMOKE_TEST
    # Contrôle positif : les étapes par modèle, elles, sont bien dupliquées.
    assert actions.count(sc.ACTION_DOWNLOAD_MODEL) == 2


def test_la_numerotation_reste_continue_apres_assemblage(tmp_path):
    """
    Chaque producteur numérote depuis 1 dans son coin ; seul le planificateur
    connaît l'ordre global. Une renumérotation ratée serait invisible à l'œil.
    """
    plan = _plan(_options(tmp_path, max_models=2))
    assert [s.order for s in plan.steps] == list(range(1, len(plan.steps) + 1))
    assert sc.validate_plan_dict(plan.to_dict()) == ()


def test_le_volume_a_telecharger_est_annonce(tmp_path):
    plan = _plan(_options(tmp_path))
    assert plan.total_download_bytes() > 0
    assert "Volume à télécharger" in sc.render_human(plan)


def test_sans_modele_retenu_aucune_etape_n_est_proposee(tmp_path):
    """Un hôte sans place ne reçoit pas un plan « au mieux » : il reçoit un blocage."""
    petit = _profile_document(disk_available_bytes=1 * GIB // 100)
    plan = _plan(_options(tmp_path, profile=petit))
    assert "aucun_modele_retenu" in [f.code for f in plan.blockers]
    assert sc.ACTION_DOWNLOAD_MODEL not in _actions(plan)
    assert sc.ACTION_SMOKE_TEST not in _actions(plan)


# ── 3. LLMfit conseille, il ne décide pas ─────────────────────────────────────

def test_llmfit_absent_ne_bloque_pas_le_plan(tmp_path):
    """Le cas par défaut sur toute machine sans LLMfit — dont la CI."""
    plan = _plan(_options(tmp_path))
    section = plan.section(sc.SECTION_RECOMMENDATION)
    assert section is not None and section.status == "skip"
    assert plan.applicable is True


def test_les_limites_de_llmfit_sont_visibles_en_rendu_humain(tmp_path):
    """
    Elles ne vivaient que dans `data`, que le rendu humain n'imprime pas. Le
    planificateur les remonte dans `notes`, qui est imprimé.
    """
    texte = sc.render_human(_plan(_options(tmp_path)))
    assert "conseiller, pas une autorité" in texte
    assert "ctx_size" in texte


def test_llmfit_ne_peut_pas_ajouter_un_modele_absent_du_catalogue(tmp_path, monkeypatch):
    """
    Le filtre catalogue est en amont : une recommandation pour un modèle inconnu
    n'a aucun effet, ni sur les retenus, ni sur les écartés.
    """
    monkeypatch.setattr(planner.llmfit_mod, "run_llmfit", lambda _c: _advice("meta-llama/Llama-3.3-70B"))
    plan = _plan(_options(tmp_path))
    retenus = [m["id"] for m in plan.section(sc.SECTION_MODELS).data["retained"]]
    assert retenus == ["qwen2.5-0.5b-instruct-q4_k_m"]
    assert all("llama" not in i for i in retenus)


def test_llmfit_peut_reordonner_des_candidats_deja_approuves(tmp_path, monkeypatch):
    """Le seul pouvoir qu'on lui laisse : faire passer un approuvé devant un autre."""
    catalog = catalog_mod.load_catalog()
    ids = [e.id for e in catalog.plannable_entries()]
    assert len(ids) >= 2, "le catalogue livré doit proposer au moins deux entrées"

    sans_conseil = _plan(_options(tmp_path))
    defaut = sans_conseil.section(sc.SECTION_MODELS).data["retained"][0]["id"]
    autre = next(i for i in ids if i != defaut)

    monkeypatch.setattr(planner.llmfit_mod, "run_llmfit", lambda _c: _advice(autre))
    avec_conseil = _plan(_options(tmp_path))
    section = avec_conseil.section(sc.SECTION_MODELS)
    assert section.data["retained"][0]["id"] == autre
    assert section.data["ordered_by_llmfit"] is True


def test_llmfit_ne_peut_pas_relever_un_budget(tmp_path, monkeypatch):
    """
    Un hôte sans place reste sans place, quelle que soit la confiance de l'outil.
    L'estimation conservatrice est un filtre dur, pas une préférence.
    """
    monkeypatch.setattr(
        planner.llmfit_mod, "run_llmfit",
        lambda _c: _advice("qwen2.5-0.5b-instruct-q4_k_m"),
    )
    petit = _profile_document(disk_available_bytes=1 * GIB // 100)
    plan = _plan(_options(tmp_path, profile=petit))
    assert plan.section(sc.SECTION_MODELS).data["retained"] == []
    assert plan.applicable is False


def test_un_conseil_sans_correspondance_laisse_l_ordre_naturel(tmp_path, monkeypatch):
    """
    Le rapprochement est volontairement strict : un rapprochement flou ferait
    entrer par la petite porte le pouvoir qu'on refuse à LLMfit par la grande.
    """
    monkeypatch.setattr(planner.llmfit_mod, "run_llmfit", lambda _c: _advice("qwen"))
    section = _plan(_options(tmp_path)).section(sc.SECTION_MODELS)
    assert section.data["ordered_by_llmfit"] is False


# ── Budget et sélection ───────────────────────────────────────────────────────

def test_un_hote_sans_gpu_est_juge_sur_sa_ram(tmp_path):
    sans_gpu = _profile_document(gpus=[], backend_candidates=["cpu"])
    plan = _plan(_options(tmp_path, profile=sans_gpu))
    budget = plan.section(sc.SECTION_MODELS).data["budget"]
    assert budget["target"] == "ram"
    assert budget["ram_budget_bytes"] < budget["ram_bytes"]


def test_un_hote_avec_gpu_est_juge_sur_sa_vram(tmp_path):
    budget = _plan(_options(tmp_path)).section(sc.SECTION_MODELS).data["budget"]
    assert budget["target"] == "vram"
    assert budget["vram_bytes"] > 0


def test_le_disque_est_juge_avec_une_marge(tmp_path):
    """
    Télécharger jusqu'au dernier octet libre rend l'hôte inexploitable bien
    avant la fin du téléchargement. La marge doit se voir dans le refus.
    """
    catalog = catalog_mod.load_catalog()
    plus_petit = min(e.resources.disk_gb for e in catalog.plannable_entries())
    juste = _profile_document(disk_available_bytes=int(plus_petit * GIB * 1.05))
    plan = _plan(_options(tmp_path, profile=juste))
    rejets = plan.section(sc.SECTION_MODELS).data["rejected"]
    assert rejets and all("disque insuffisant" in r["reason"] for r in rejets)


def test_une_entree_non_epinglee_ne_peut_pas_etre_retenue(tmp_path):
    """
    Le fail-closed du catalogue doit tenir *à travers* le planificateur, pas
    seulement dans le catalogue.

    Ce test comble un trou réel : toutes les entrées livrées étant épinglées,
    remplacer `plannable_entries()` par `entries` dans le planificateur ne
    faisait tomber aucun test. Le filtre était donc libre de disparaître.
    """
    import yaml

    document = yaml.safe_load(catalog_mod.DEFAULT_CATALOG_PATH.read_text(encoding="utf-8"))
    assert len(document["models"]) >= 2
    depingle = document["models"][1]
    depingle["source"]["files"][0]["sha256"] = None
    chemin = tmp_path / "catalogue-partiel.yaml"
    chemin.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")

    plan = _plan(_options(tmp_path, catalog_path=chemin, max_models=5))
    section = plan.section(sc.SECTION_MODELS)
    retenus = [m["id"] for m in section.data["retained"]]
    assert depingle["id"] not in retenus
    assert any(r["id"] == depingle["id"] for r in section.data["rejected"])
    # Contrôle positif : l'entrée restée épinglée, elle, est bien retenue.
    assert document["models"][0]["id"] in retenus
    # Et le plan entier est bloqué : une entrée non épinglée est un défaut de
    # l'artefact catalogue, pas une condition d'environnement qu'on subirait.
    assert plan.applicable is False


def test_le_nombre_de_modeles_retenus_est_borne(tmp_path):
    section = _plan(_options(tmp_path, max_models=1)).section(sc.SECTION_MODELS)
    assert len(section.data["retained"]) == 1
    assert any("max-models" in r["reason"] for r in section.data["rejected"])


def test_selectionner_un_identifiant_inconnu_est_une_faute_de_saisie(tmp_path):
    """
    Défaut trouvé en rédigeant la documentation : l'erreur remontait jusqu'au
    `except Exception` de la CLI et sortait en 4 — « le planificateur est
    cassé » — alors qu'une faute de frappe dans `--model` relève de l'usage.
    Un script qui lit 4 conclurait à une panne de l'outil.
    """
    with pytest.raises(planner.PlannerUsageError):
        _plan(_options(tmp_path, selected_ids=("modele-imaginaire",)))


def test_la_decision_dit_pourquoi_ce_modele(tmp_path):
    """
    Condition de sortie du jalon M1 : le système sait expliquer ce qu'il
    installera ET pourquoi. Une décision sans raison ne tient pas ce contrat.
    """
    plan = _plan(_options(tmp_path))
    decision = next(d for d in plan.decisions if d.topic == "modèle par défaut")
    assert decision.choice != "aucun"
    assert "tient dans" in decision.rationale


def test_la_section_modeles_rappelle_que_les_ressources_sont_estimees(tmp_path):
    texte = sc.render_human(_plan(_options(tmp_path)))
    assert "ESTIMATIONS" in texte
    assert "calibrate_model" in texte


# ── Intégration CLI (`python cli.py bootstrap-plan`) ──────────────────────────

def _invoke(tmp_path, *extra: str, profile: dict | None = None):
    """Appelle la commande telle qu'un opérateur la lancera avant d'installer."""
    from typer.testing import CliRunner

    import cli

    models_dir = tmp_path / "models"
    models_dir.mkdir(exist_ok=True)
    profile_path = tmp_path / "profil.json"
    profile_path.write_text(json.dumps(profile or _profile_document()), encoding="utf-8")

    return CliRunner().invoke(cli.app, [
        "bootstrap-plan",
        "--hardware-profile", str(profile_path),
        "--models-dir", str(models_dir),
        *extra,
    ])


def test_cli_json_est_un_plan_valide(tmp_path):
    result = _invoke(
        tmp_path, "--json",
        "--pin-version", "b6800", "--pin-commit", "a" * 40, "--min-build", "6600",
    )
    assert result.exit_code in (sc.EXIT_OK, sc.EXIT_WARNINGS), result.output
    document = json.loads(result.output)
    assert sc.validate_plan_dict(document) == ()
    assert document["tool"] == sc.PLAN_TOOL_NAME


def test_cli_bloque_sans_epinglage_du_runtime(tmp_path):
    result = _invoke(tmp_path)
    assert result.exit_code == sc.EXIT_BLOCKED
    assert "politique_de_release_absente" in result.output


def test_cli_strict_promeut_les_avertissements(tmp_path):
    pin = ("--pin-version", "b6800", "--pin-commit", "a" * 40, "--min-build", "6600")
    assert _invoke(tmp_path, *pin).exit_code == sc.EXIT_WARNINGS
    assert _invoke(tmp_path, *pin, "--strict").exit_code == sc.EXIT_BLOCKED


@pytest.mark.parametrize("args", [
    ("--mode", "bizarre"),
    ("--pin-version", "b6800"),                      # sans son commit
    ("--pin-version", "pas-une-version", "--pin-commit", "a" * 40),
    ("--model", "modele-imaginaire"),
])
def test_cli_refuse_un_usage_incoherent(tmp_path, args):
    """
    Une erreur d'usage sort en 2, jamais en 1 : un opérateur — ou `update.sh` —
    ne doit pas confondre « ta commande est mal formée » et « cet hôte est bloqué ».
    """
    assert _invoke(tmp_path, *args).exit_code == sc.EXIT_USAGE


def test_cli_signale_un_profil_illisible_sans_pretendre_bloquer_l_hote(tmp_path):
    """
    Un chemin qui n'existe pas est une faute de saisie (2), pas un hôte bloqué
    (1) ni une panne du planificateur (4). Les trois se lisent dans un script.
    """
    from typer.testing import CliRunner

    import cli

    result = CliRunner().invoke(cli.app, [
        "bootstrap-plan", "--hardware-profile", str(tmp_path / "nulle-part.json"),
    ])
    assert result.exit_code == sc.EXIT_USAGE


def test_cli_n_expose_aucun_secret(tmp_path, monkeypatch):
    """
    Le plan est fait pour être copié dans un ticket. Un jeton présent dans
    l'environnement ne doit jamais s'y retrouver.
    """
    monkeypatch.setenv("HF_TOKEN", "hf_zzzzzzzzzzzzzzzzzzzzzzzz")
    result = _invoke(
        tmp_path, "--pin-version", "b6800", "--pin-commit", "a" * 40, "--min-build", "6600",
    )
    assert "hf_zzzzzzzzzzzzzzzzzzzzzzzz" not in result.output
    # Contrôle positif : la sortie n'est pas vide, le test voit bien quelque chose.
    assert "PLAN DE BOOTSTRAP" in result.output
