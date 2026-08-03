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
import hashlib
import json
import re
import struct

import pytest

from bootstrap import catalog as catalog_mod
from bootstrap import downloader, execution, gguf_meta
from bootstrap import llmfit as llmfit_mod
from bootstrap import planner
from bootstrap import runtime_resolver as runtime_mod
from bootstrap import schema as sc
from llama_version import LlamaVersion

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


def test_un_runtime_non_resolu_ne_laisse_passer_aucune_etape(tmp_path):
    """
    Régression : l'invariant « un plan bloqué ne propose aucune étape » n'était
    tenu QUE pour l'absence de politique de release. Un résolveur rendant
    `resolved=False` laissait passer téléchargement, écriture de registre,
    activation et recette — **sans la moindre étape d'installation**. Un
    applicateur y aurait lu de quoi consommer disque et réseau pour rien.

    Une plateforme sans aucune variante d'artefact produit exactement ce cas.
    """
    exotique = _profile_document(arch="riscv64", backend_candidates=["cpu"])
    plan = _plan(_options(tmp_path, profile=exotique))
    assert "runtime_unresolved" in [f.code for f in plan.blockers]
    assert plan.applicable is False
    assert plan.steps == ()


def test_aucun_bloqueur_quelle_qu_en_soit_la_cause_ne_laisse_d_etapes(tmp_path):
    """
    L'invariant est adossé aux BLOQUEURS, pas à une cause particulière : il doit
    donc valoir aussi pour une cause qui n'a rien à voir avec le runtime.
    """
    plan = _plan(_options(tmp_path, catalog_path=tmp_path / "absent.yaml"))
    assert plan.blockers
    assert plan.steps == ()
    # Contrôle positif : le même hôte, catalogue sain, produit bien des étapes.
    assert _plan(_options(tmp_path)).steps


def test_cuda_visible_devices_s_applique_a_un_profil_declare(tmp_path, monkeypatch):
    """
    Régression : le planificateur appelait le chargeur sans lui transmettre
    l'environnement, qui retombait sur `{}`. Deux GPU déclarés avec
    `CUDA_VISIBLE_DEVICES=1` donnaient un budget VRAM **doublé** — la variable
    gouverne ce que CUDA expose au runtime, quelle que soit l'origine de la liste.
    """
    premier = _profile_document()["gpus"][0]
    second = dict(premier, index=1, uuid="GPU-11111111-1111-1111-1111-111111111111")
    deux_gpu = _profile_document(gpus=[premier, second])

    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "1")
    data = _plan(_options(tmp_path, profile=deux_gpu)).section(sc.SECTION_HARDWARE).data
    assert data["visible_gpu_count"] == 1
    assert data["visible_vram_total_bytes"] == premier["vram_total_bytes"]

    # Contrôle positif : sans restriction, les deux GPU comptent bien.
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES")
    data = _plan(_options(tmp_path, profile=deux_gpu)).section(sc.SECTION_HARDWARE).data
    assert data["visible_gpu_count"] == 2


def test_le_mode_cluster_est_refuse_explicitement(tmp_path):
    """
    Le mode était accepté et simplement recopié dans le document. Le plan produit
    inventoriait l'hôte gateway, choisissait un runtime LOCAL et prévoyait des
    GGUF sous son propre volume — un plan cohérent et entièrement faux, ce qui
    est pire qu'une absence de plan.
    """
    with pytest.raises(planner.PlannerUsageError) as exc:
        _plan(_options(tmp_path, mode="cluster"))
    message = str(exc.value)
    assert "cluster" in message
    assert "M1" in message
    # Le message doit dire quoi faire, pas seulement ce qui ne va pas.
    assert "--hardware-profile" in message or "--mode local" in message


def test_un_mode_inconnu_est_refuse(tmp_path):
    with pytest.raises(planner.PlannerUsageError):
        _plan(_options(tmp_path, mode="bizarre"))


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
        sc.ACTION_SMOKE_TEST,
        sc.ACTION_WARMUP_MODEL,
    ]


def _politique_epinglee() -> runtime_mod.ResolverPolicy:
    """
    Matrice d'artefacts ÉPINGLÉE, telle que `--runtime-variants` en fournit une.

    La matrice livrée avec EVARuntime ne porte aucune empreinte : seule une
    variante `local-build` y est éligible, et un build local n'a rien à vérifier
    avant construction. C'est pourquoi COR-030 ne se voyait pas sur le plan par
    défaut — il n'est devenu atteignable qu'avec AUT-018.
    """
    return runtime_mod.ResolverPolicy(
        release=_release(),
        variants=(runtime_mod.ArtifactVariant(
            source=runtime_mod.SOURCE_OFFICIAL_RELEASE,
            backend="cuda12",
            platform="linux-x86_64",
            evidence=runtime_mod.EVIDENCE_OPERATOR,
            evidence_note="Empreinte relevée par l'opérateur pour ce test.",
            artifact_sha256="c" * 64,
        ),),
    )


def test_un_runtime_epingle_n_ajoute_pas_d_etape_de_verification_vide(tmp_path):
    """
    COR-030 — le plan de l'opérateur ne porte qu'UNE `verify_artifact` : celle des
    GGUF, qui relit réellement les octets.

    L'étape jumelle du domaine runtime précédait `install_runtime` sans pouvoir
    rien lire — l'archive n'est pas encore téléchargée à ce numéro. L'applicateur
    la sautait, et une étape sautée fait retomber la condition n°1 du jalon M2 :
    une installation réussie sortait en `partial`, code 3.
    """
    plan = _plan(_options(tmp_path, resolver_policy=_politique_epinglee()))
    actions = _actions(plan)

    # Contrôle positif : la variante épinglée est bien celle qui a été retenue,
    # sans quoi ce test observerait le `local-build` par défaut et ne prouverait rien.
    section = next(s for s in plan.sections if s.name == sc.SECTION_RUNTIME)
    assert section.data["variant"]["source"] == runtime_mod.SOURCE_OFFICIAL_RELEASE
    assert section.data["variant"]["artifact_sha256"] == "c" * 64

    assert actions.count(sc.ACTION_VERIFY_ARTIFACT) == 1
    verification = _etape(plan, sc.ACTION_VERIFY_ARTIFACT)
    assert "llama-server" not in verification.target

    # L'empreinte épinglée n'a pas disparu du plan : elle est portée par l'étape
    # qui la confronte vraiment.
    installation = _etape(plan, sc.ACTION_INSTALL_RUNTIME)
    assert "c" * 64 in installation.detail


def test_la_verification_suit_immediatement_le_telechargement(tmp_path):
    """Contrôler l'empreinte après avoir mis le modèle en service ne protège de rien."""
    actions = _actions(_plan(_options(tmp_path)))
    assert actions.index(sc.ACTION_VERIFY_ARTIFACT) == actions.index(sc.ACTION_DOWNLOAD_MODEL) + 1


def test_l_activation_suit_la_calibration(tmp_path):
    """Activer avant de calibrer publierait une capacité supposée (AUT-007)."""
    actions = _actions(_plan(_options(tmp_path)))
    assert actions.index(sc.ACTION_ENABLE_MODEL) > actions.index(sc.ACTION_CALIBRATE_MODEL)
    assert actions.index(sc.ACTION_SMOKE_TEST) == actions.index(sc.ACTION_ENABLE_MODEL) + 1
    assert actions.index(sc.ACTION_WARMUP_MODEL) > actions.index(sc.ACTION_SMOKE_TEST)


def test_la_licence_est_acceptee_avant_tout_telechargement(tmp_path):
    actions = _actions(_plan(_options(tmp_path)))
    assert actions.index(sc.ACTION_ACCEPT_LICENSE) < actions.index(sc.ACTION_DOWNLOAD_MODEL)


def test_le_registre_est_ecrit_desactive(tmp_path):
    plan = _plan(_options(tmp_path))
    step = next(s for s in plan.steps if s.action == sc.ACTION_WRITE_REGISTRY)
    assert "enabled: false" in step.detail


def test_le_smoke_test_est_execute_et_cible_pour_chaque_modele(tmp_path):
    """
    DEC-010 ouvre chaque modèle provisoirement : chacun doit donc produire sa
    propre preuve publique avant son warmup, sans pouvoir réutiliser celle du voisin.
    """
    plan = _plan(_options(tmp_path, max_models=2))
    actions = _actions(plan)
    assert actions.count(sc.ACTION_SMOKE_TEST) == 2
    assert actions.count(sc.ACTION_DOWNLOAD_MODEL) == 2
    for step in plan.steps:
        if step.action != sc.ACTION_ENABLE_MODEL:
            continue
        smoke = plan.steps[step.order]
        warmup = plan.steps[step.order + 1]
        assert (smoke.action, smoke.target) == (sc.ACTION_SMOKE_TEST, step.target)
        assert (warmup.action, warmup.target) == (sc.ACTION_WARMUP_MODEL, step.target)


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


# ── 4. AUT-014 — artefacts déjà présents ──────────────────────────────────────
#
# Sur le second déploiement réel (§0.13), les deux GGUF du catalogue étaient déjà
# sur l'hôte et le plan annonçait quand même 837,1 Mio de téléchargement. Ce qui
# est testé ici est le RAISONNEMENT, pas la collecte : le planificateur lisait
# déjà les en-têtes, il n'en tirait rien.
#
# Le catalogue de production épingle des fichiers de plusieurs centaines de Mio :
# les tests fabriquent donc leur propre catalogue, avec de vrais GGUF minimaux
# de quelques dizaines d'octets et de vraies empreintes.

ARTEFACT_REVISION = "0123456789abcdef0123456789abcdef01234567"
ARTEFACT_ID = "modele-aut-014"
POIDS = "modele-aut-014-q4_k_m.gguf"
MMPROJ = "mmproj-modele-aut-014.gguf"


def _gguf(taille: int, *, arch: str = "qwen2") -> bytes:
    """
    Un GGUF minimal mais RÉELLEMENT lisible, complété jusqu'à la taille voulue.

    Un fichier illisible ferait apparaître un constat `gguf_illisible` dans la
    section « modèles » et brouillerait ce qu'on cherche à observer ici.
    """
    valeur = arch.encode("utf-8")
    cle = b"general.architecture"
    kv = (
        struct.pack("<Q", len(cle)) + cle
        + struct.pack("<I", gguf_meta._T_STRING)
        + struct.pack("<Q", len(valeur)) + valeur
    )
    tete = b"GGUF" + struct.pack("<I", 3) + struct.pack("<Q", 0) + struct.pack("<Q", 1) + kv
    assert taille >= len(tete)
    return tete + b"\0" * (taille - len(tete))


POIDS_OCTETS = _gguf(4096)
MMPROJ_OCTETS = _gguf(2048)


def _catalogue_artefact(tmp_path) -> catalog_mod.Catalog:
    """Catalogue synthétique relu par le VRAI chargeur — contraintes du catalogue incluses."""
    import yaml

    def fichier(nom: str, role: str, data: bytes) -> dict:
        return {
            "name": nom,
            "role": role,
            "sha256": hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data),
        }

    document = {
        "catalog_version": 1,
        "downloader": {"name": "eva-bootstrap downloader", "license_id": "agpl-3.0"},
        "models": [{
            "id": ARTEFACT_ID,
            "family": "test",
            "display_name": "Modèle synthétique AUT-014",
            "description": "Entrée fabriquée pour les régressions d'AUT-014.",
            "use_cases": ["smoke_test"],
            "source": {
                "provider": "huggingface",
                "repo_id": "organisation/depot-aut-014",
                "repo_url": "https://huggingface.co/organisation/depot-aut-014",
                "revision": ARTEFACT_REVISION,
                "revision_recorded_on": "2026-08-01",
                "files": [
                    fichier(POIDS, "weights", POIDS_OCTETS),
                    fichier(MMPROJ, "mmproj", MMPROJ_OCTETS),
                ],
            },
            "license": {
                "base_model": {"id": "apache-2.0"},
                "fine_tune": {"id": "apache-2.0"},
                "usage_terms": None,
                "gated": False,
                "redistribution_allowed": True,
                "operator_acceptance_required": False,
                "notes": None,
            },
            "runtime": {
                "min_llama_build": 0,
                "capabilities": ["text_generation"],
                "requires_mmproj": True,
                "defaults": {
                    "ctx_size": 4096, "parallel": 1,
                    "cache_type_k": "f16", "cache_type_v": "f16",
                },
            },
            "resources": {
                "disk_gb": 1.0, "initial_vram_gb": 1.0, "initial_ram_gb": 1.0,
                "estimation_basis": "gguf_header",
            },
        }],
    }
    cible = tmp_path / "catalogue-aut-014.yaml"
    cible.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
    return catalog_mod.load_catalog(cible)


def _options_artefact(tmp_path, **overrides) -> planner.PlannerOptions:
    _catalogue_artefact(tmp_path)
    return _options(
        tmp_path, catalog_path=tmp_path / "catalogue-aut-014.yaml", **overrides
    )


def _poser(options, *, poids: bytes | None = POIDS_OCTETS, mmproj: bytes | None = MMPROJ_OCTETS):
    """Pose sur le disque ce qu'un déploiement antérieur y aurait laissé."""
    if poids is not None:
        (options.models_dir / POIDS).write_bytes(poids)
    if mmproj is not None:
        (options.models_dir / MMPROJ).write_bytes(mmproj)


def _attester(options) -> None:
    """
    Écrit le manifeste de provenance par la VRAIE fabrique du téléchargeur.

    Le recopier à la main dans le test ferait exactement ce que le module refuse
    de faire dans le code : une seconde définition du manifeste, qui dériverait.
    """
    catalogue = catalog_mod.load_catalog(options.catalog_path)
    entree = catalogue.get(ARTEFACT_ID)
    downloader.provenance_path(options.models_dir, ARTEFACT_ID).write_text(
        json.dumps(downloader.build_manifest(
            entree, options.models_dir,
            downloaded_at="2026-08-01T12:00:00Z", token_used=False,
            acceptance=None, catalog=catalogue,
        )),
        encoding="utf-8",
    )


def _etape(plan, action):
    return next((s for s in plan.steps if s.action == action), None)


def test_un_artefact_absent_est_bien_planifie_au_telechargement(tmp_path):
    """
    Contrôle positif de toute la section : sans fichier sur l'hôte, le plan
    propose le téléchargement et en annonce le volume. Sans lui, les tests
    d'absence qui suivent seraient verts pour la mauvaise raison.
    """
    plan = _plan(_options_artefact(tmp_path))
    telechargement = _etape(plan, sc.ACTION_DOWNLOAD_MODEL)

    assert telechargement is not None
    assert telechargement.estimated_bytes == len(POIDS_OCTETS) + len(MMPROJ_OCTETS)
    assert plan.total_download_bytes() == telechargement.estimated_bytes
    assert _actions(plan).index(sc.ACTION_VERIFY_ARTIFACT) == _actions(plan).index(
        sc.ACTION_DOWNLOAD_MODEL
    ) + 1


def test_un_artefact_present_et_atteste_n_est_plus_retelecharge(tmp_path):
    """
    Le cas d'EvR-A : les fichiers étaient là, vérifiés, et le plan proposait
    quand même 837,1 Mio. L'étape de téléchargement disparaît, son volume est
    décompté, et la `verify_artifact` restée seule porte le motif.
    """
    options = _options_artefact(tmp_path)
    _poser(options)
    _attester(options)
    plan = _plan(options)

    assert sc.ACTION_DOWNLOAD_MODEL not in _actions(plan)
    assert plan.total_download_bytes() == 0
    assert plan.applicable is True
    assert sc.validate_plan_dict(plan.to_dict()) == ()

    verification = _etape(plan, sc.ACTION_VERIFY_ARTIFACT)
    assert verification is not None
    assert "Aucun téléchargement proposé" in verification.detail
    assert "manifeste de provenance atteste" in verification.detail
    # Le motif reste aussi lisible dans la section « modèles », pour l'opérateur
    # qui lit le plan et non ses étapes.
    retenu = plan.section(sc.SECTION_MODELS).data["retained"][0]
    assert retenu["local_artifact"]["attested"] is True


def test_le_plan_qui_ne_telecharge_plus_reste_execute_par_l_applicateur(tmp_path):
    """
    Une `verify_artifact` seule doit s'inscrire proprement dans le journal.

    L'applicateur enregistre bien un exécuteur pour elle, et cet exécuteur rend
    `already_satisfied` — jamais `done` : une vérification qui réussit n'a rien
    changé sur l'hôte, et `ExecutionReport.changed()` ne doit pas devenir vrai à
    cause d'une lecture.
    """
    options = _options_artefact(tmp_path)
    _poser(options)
    _attester(options)
    plan = _plan(options)

    catalogue = catalog_mod.load_catalog(options.catalog_path)
    config = downloader.DownloadConfig(
        catalog=catalogue,
        models_dir=options.models_dir,
        transport=None,
        token_provider=lambda: None,
        disk_free=lambda path: 10 ** 12,
        chunk_bytes=64,
    )
    executeurs = downloader.make_executors(config)
    registre = execution.ExecutorRegistry()
    for action, executeur in executeurs.items():
        registre.register(action, executeur)

    assert registre.missing_actions(
        [s for s in plan.steps if s.action in executeurs]
    ) == ()

    contexte = execution.ExecutionContext(
        execution.ExecutionMode.APPLY,
        allowed_roots=(tmp_path,),
        now=lambda: "2026-08-01T12:00:00Z",
        log=lambda message: None,
    )
    resultat = asyncio.run(
        executeurs[sc.ACTION_VERIFY_ARTIFACT](
            _etape(plan, sc.ACTION_VERIFY_ARTIFACT), contexte
        )
    )
    assert resultat.status == execution.STEP_ALREADY_SATISFIED


def test_un_artefact_present_mais_divergent_bloque_le_plan(tmp_path):
    """
    Une taille différente de celle épinglée interdit au SHA-256 de correspondre.

    C'est une preuve, pas une présomption : le plan bloque, et ne propose ni de
    réutiliser le fichier ni de l'écraser.
    """
    options = _options_artefact(tmp_path)
    _poser(options, poids=_gguf(4096 + 64))
    _attester(options)
    plan = _plan(options)

    assert "artefact_local_divergent" in [f.code for f in plan.blockers]
    assert plan.applicable is False
    assert plan.effective_steps() == ()
    message = next(f.message for f in plan.blockers if f.code == "artefact_local_divergent")
    assert POIDS in message
    assert "supprimez-le vous-même" in message


def test_un_ensemble_partiellement_present_reste_indivisible(tmp_path):
    """
    Un `mmproj` sans ses poids ne charge rien (§8), et l'inverse non plus.

    Créditer la moitié présente annoncerait un volume que rien n'atteste : le
    téléchargement reste proposé EN ENTIER, attestation ou pas.
    """
    options = _options_artefact(tmp_path)
    _poser(options, mmproj=None)
    _attester(options)
    plan = _plan(options)

    telechargement = _etape(plan, sc.ACTION_DOWNLOAD_MODEL)
    assert telechargement is not None
    assert telechargement.estimated_bytes == len(POIDS_OCTETS) + len(MMPROJ_OCTETS)
    assert "ensemble incomplet" in telechargement.detail
    assert "indivisible" in telechargement.detail


def test_sans_attestation_le_telechargement_reste_propose(tmp_path):
    """
    Fichiers en place, tailles conformes, aucun manifeste : le plan ne présume
    pas d'une empreinte qu'il n'a pas vérifiée. Le cas de l'opérateur qui a
    recopié ses GGUF à la main.
    """
    options = _options_artefact(tmp_path)
    _poser(options)
    plan = _plan(options)

    telechargement = _etape(plan, sc.ACTION_DOWNLOAD_MODEL)
    assert telechargement is not None
    assert "aucune attestation de vérification exploitable" in telechargement.detail
    assert plan.applicable is True


def test_une_attestation_perimee_ne_credite_pas_l_ensemble(tmp_path):
    """Un manifeste qui parle d'une autre révision décrit des fichiers que le catalogue n'approuve plus."""
    options = _options_artefact(tmp_path)
    _poser(options)
    _attester(options)
    chemin = downloader.provenance_path(options.models_dir, ARTEFACT_ID)
    document = json.loads(chemin.read_text(encoding="utf-8"))
    document["source"]["revision"] = "f" * 40
    chemin.write_text(json.dumps(document), encoding="utf-8")

    plan = _plan(options)
    assert _etape(plan, sc.ACTION_DOWNLOAD_MODEL) is not None
    retenu = plan.section(sc.SECTION_MODELS).data["retained"][0]
    assert retenu["local_artifact"]["attested"] is False
    assert retenu["local_artifact"]["attestation_problems"]


def test_le_plan_ne_hache_aucun_octet_pour_conclure(tmp_path, monkeypatch):
    """
    L'arbitrage du chantier, rendu exécutable : `bootstrap-plan` doit rester une
    commande rapide et rejouable. Hacher 40 Gio à chaque planification
    échangerait un défaut d'affichage contre un défaut de conception.
    """
    appels: list[str] = []
    vrai_sha256 = hashlib.sha256

    def espion(*args, **kwargs):
        appels.append("sha256")
        return vrai_sha256(*args, **kwargs)

    monkeypatch.setattr(hashlib, "sha256", espion)

    options = _options_artefact(tmp_path)
    _poser(options)
    _attester(options)
    appels.clear()
    plan = _plan(options)

    assert sc.ACTION_DOWNLOAD_MODEL not in _actions(plan)
    assert appels == []
    # Contrôle positif : l'espion voit bien passer un hachage quand il y en a un.
    hashlib.sha256(b"controle")
    assert appels == ["sha256"]


# ── Intégration CLI (`python cli.py bootstrap-plan`) ──────────────────────────

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _sans_ansi(texte: str) -> str:
    """
    Retire les séquences de couleur avant toute recherche dans une sortie CLI.

    rich colorise dès qu'il détecte GitHub Actions : sans ce nettoyage, une
    recherche de sous-chaîne devient dépendante de l'environnement — verte en
    local, rouge en CI. Le cas est pire pour un test d'ABSENCE : un secret
    fragmenté par des séquences ANSI passerait inaperçu, et le test rassurerait
    à tort.
    """
    return _ANSI_RE.sub("", texte)


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
    assert "politique_de_release_absente" in _sans_ansi(result.output)


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


def test_cli_refuse_le_mode_cluster_en_erreur_d_usage(tmp_path):
    result = _invoke(tmp_path, "--mode", "cluster")
    assert result.exit_code == sc.EXIT_USAGE
    assert "cluster" in _sans_ansi(result.output)


def _options_de_la_commande(nom: str = "bootstrap-plan") -> set[str]:
    """
    Options réellement déclarées par la commande, lues sur l'objet Click.

    Volontairement PAS lues dans la sortie de `--help` : rich colorise cette
    sortie dès qu'il détecte GitHub Actions, et découpe alors les noms d'options
    en fragments séparés par des séquences ANSI. Un `--llmfit-bin` parfaitement
    présent devenait introuvable par recherche de sous-chaîne — un test vert en
    local et rouge en CI, qui ne disait rien de la commande et tout de son
    rendu. On interroge donc le contrat, pas l'affichage.
    """
    import typer.main

    import cli

    groupe = typer.main.get_command(cli.app)
    commande = groupe.commands[nom]  # type: ignore[attr-defined]
    # `secondary_opts` porte la face NÉGATIVE d'un drapeau booléen
    # (`--allow-local-build/--no-local-build`). L'omettre rendait l'introspection
    # aveugle à la moitié des options d'un drapeau à deux faces — et un test
    # d'existence adossé à elle serait passé au vert sans rien prouver.
    return {
        opt
        for param in commande.params
        for opt in (*getattr(param, "opts", ()), *getattr(param, "secondary_opts", ()))
    }


def test_cli_expose_l_epinglage_llmfit(tmp_path):
    """
    Régression : l'adaptateur refusait — correctement — d'exécuter LLMfit sans
    version ni empreinte épinglées, mais AUCUNE option ne permettait de les
    fournir. AUT-004 était implémenté comme bibliothèque et inatteignable depuis
    le parcours opérateur.
    """
    options = _options_de_la_commande()
    for option in ("--llmfit-bin", "--llmfit-version", "--llmfit-sha256",
                   "--llmfit-profile", "--llmfit-timeout", "--no-llmfit"):
        assert option in options, f"{option} absente de la CLI"


def test_cli_expose_les_options_du_plan():
    """
    Contrôle positif de l'introspection : elle voit bien les autres options.
    Sans lui, une extraction devenue inerte rendrait le test précédent vide de
    sens tout en le laissant vert.
    """
    options = _options_de_la_commande()
    for option in ("--json", "--mode", "--catalog", "--hardware-profile",
                   "--models-dir", "--model", "--max-models", "--llama-bin",
                   "--pin-version", "--pin-commit", "--min-build", "--strict"):
        assert option in options, f"{option} absente de la CLI"
    assert "--option-qui-n-existe-pas" not in options


@pytest.mark.parametrize("args", [
    ("--llmfit-version", "0.6.1"),                       # sans son empreinte
    ("--llmfit-sha256", "a" * 64),                       # sans sa version
    ("--llmfit-version", "0.6.1", "--llmfit-sha256", "trop-court"),
    ("--llmfit-timeout", "0"),
])
def test_cli_refuse_un_epinglage_llmfit_incoherent(tmp_path, args):
    """Une version seule se déclare ; une empreinte seule ne dit pas ce qu'on installait."""
    assert _invoke(tmp_path, *args).exit_code == sc.EXIT_USAGE


def test_cli_permet_de_desactiver_llmfit(tmp_path):
    pin = ("--pin-version", "b6800", "--pin-commit", "a" * 40, "--min-build", "6600")
    result = _invoke(tmp_path, *pin, "--no-llmfit", "--json")
    assert result.exit_code in (sc.EXIT_OK, sc.EXIT_WARNINGS), result.output
    document = json.loads(result.output)
    section = next(s for s in document["sections"] if s["name"] == sc.SECTION_RECOMMENDATION)
    assert section["status"] == "skip"


def test_cli_accepte_un_profil_de_recommandation_manuel(tmp_path):
    """Le fallback de §7 : une recommandation écrite à la main, MÊME validation."""
    profil = tmp_path / "recommandation.json"
    profil.write_text(json.dumps({"recommendations": [{"model": "qwen2.5-0.5b"}]}), encoding="utf-8")
    pin = ("--pin-version", "b6800", "--pin-commit", "a" * 40, "--min-build", "6600")
    result = _invoke(tmp_path, *pin, "--llmfit-profile", str(profil), "--json")
    assert result.exit_code in (sc.EXIT_OK, sc.EXIT_WARNINGS, sc.EXIT_BLOCKED), result.output
    document = json.loads(result.output)
    assert sc.validate_plan_dict(document) == ()


def test_cli_n_expose_aucun_secret(tmp_path, monkeypatch):
    """
    Le plan est fait pour être copié dans un ticket. Un jeton présent dans
    l'environnement ne doit jamais s'y retrouver.
    """
    monkeypatch.setenv("HF_TOKEN", "hf_zzzzzzzzzzzzzzzzzzzzzzzz")
    result = _invoke(
        tmp_path, "--pin-version", "b6800", "--pin-commit", "a" * 40, "--min-build", "6600",
    )
    texte = _sans_ansi(result.output)
    assert "hf_zzzzzzzzzzzzzzzzzzzzzzzz" not in texte
    # Contrôle positif : la sortie n'est pas vide, le test voit bien quelque chose.
    assert "PLAN DE BOOTSTRAP" in texte


# ══════════════════════════════════════════════════════════════════════════════
# SEC-017 — le manifeste du binaire en place est réellement relu et recoupé
# ══════════════════════════════════════════════════════════════════════════════
#
# SEC-009 recoupait le manifeste contre le binaire — version, commit, backend,
# empreinte — mais `_resolve_runtime` ne passait que `existing_binary` :
# `existing_manifest` valait toujours `None`, et le recoupement n'avait donc
# jamais lieu sur le parcours de l'opérateur. Ces tests exercent la chaîne
# complète, depuis le fichier posé sur disque jusqu'au constat du plan.

_COMMIT_POSE = "0123456789abcdef0123456789abcdef01234567"
_SHA_BINAIRE = "d" * 64


def _binaire_avec_manifeste(tmp_path, document=None, *, sans_manifeste=False):
    """Pose un faux `llama-server` et, sauf demande contraire, son manifeste §6."""
    bin_dir = tmp_path / "opt" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    binaire = bin_dir / "llama-server"
    binaire.write_bytes(b"#!/bin/false\n")
    if sans_manifeste:
        return binaire

    if document is None:
        document = runtime_mod.ProvenanceManifest(
            version="b6750", commit=_COMMIT_POSE, source=runtime_mod.SOURCE_LOCAL_BUILD,
            backend="cuda12", platform="linux-x86_64", installed_at=GENERATED_AT,
            build_options={"GGML_CUDA": True},
        ).to_document()
        document["install"] = {"binary_sha256": _SHA_BINAIRE}
    if isinstance(document, str):
        runtime_mod.provenance_path(binaire).write_text(document, encoding="utf-8")
    else:
        runtime_mod.provenance_path(binaire).write_text(
            json.dumps(document), encoding="utf-8",  # JSON est un sous-ensemble de YAML
        )
    return binaire


def _sonder(monkeypatch, *, build=6750, commit="0123456", empreinte=_SHA_BINAIRE):
    """Sonde `--version` et empreinte injectées : aucun sous-processus ici non plus."""
    async def probe(_binaire):
        return LlamaVersion(build=build, raw=f"version: {build} ({commit})", commit=commit)

    async def digest(_binaire):
        return empreinte

    monkeypatch.setattr(runtime_mod, "probe_llama_version", probe)
    monkeypatch.setattr(runtime_mod, "sha256_binary", digest)


def _section_runtime(plan):
    return next(s for s in plan.sections if s.name == sc.SECTION_RUNTIME)


def test_un_binaire_atteste_est_conserve_par_le_planificateur(tmp_path, monkeypatch):
    """
    Contrôle positif de la famille : manifeste cohérent, version, commit, backend
    et empreinte concordants — le binaire est conservé.

    Ce test ne peut passer QUE si le planificateur a effectivement lu le manifeste
    sur disque : sans lecture, `existing_manifest` reste `None` et un hôte GPU
    fait remplacer le binaire faute de provenance.
    """
    _sonder(monkeypatch)
    binaire = _binaire_avec_manifeste(tmp_path)
    plan = _plan(_options(tmp_path, existing_binary=binaire))

    section = _section_runtime(plan)
    assert section.data["reuse_existing"] is True
    assert section.data["observed_build"] == 6750


def test_un_manifeste_qui_ne_decrit_pas_ce_binaire_fait_remplacer(tmp_path, monkeypatch):
    """
    Le recoupement a lieu POUR DE VRAI : l'empreinte consignée ne correspond plus
    au binaire, tout le reste concorde, et la réutilisation est refusée.

    C'est la mutation qui prouve que SEC-009 n'est plus dormant.
    """
    _sonder(monkeypatch, empreinte="f" * 64)
    binaire = _binaire_avec_manifeste(tmp_path)
    plan = _plan(_options(tmp_path, existing_binary=binaire))

    section = _section_runtime(plan)
    assert section.data["reuse_existing"] is False
    assert "runtime_binary_tampered" in {f.code for f in section.findings}


@pytest.mark.parametrize("fabrique, code", [
    (lambda: None, "runtime_manifest_absent"),
    (lambda: "runtime: [pas du yaml\n  valide: du tout\n", "runtime_manifest_unreadable"),
    (lambda: {"runtime": {"version": "pas-un-tag"}}, "runtime_manifest_invalid"),
])
def test_un_manifeste_absent_illisible_ou_incoherent_porte_son_constat(
    tmp_path, monkeypatch, fabrique, code,
):
    """
    Les trois issues dégradées produisent un CONSTAT NOMMÉ dans le plan — jamais
    un plantage, jamais un silence — et aucune ne vaut attestation.
    """
    _sonder(monkeypatch)
    document = fabrique()
    binaire = _binaire_avec_manifeste(
        tmp_path, document, sans_manifeste=document is None,
    )
    plan = _plan(_options(tmp_path, existing_binary=binaire))

    section = _section_runtime(plan)
    codes = {f.code for f in section.findings}
    assert code in codes, codes
    # Fail-closed : sur un hôte GPU, aucune de ces trois issues ne conserve le binaire.
    assert section.data["reuse_existing"] is False


# ══════════════════════════════════════════════════════════════════════════════
# AUT-019 — les trois drapeaux de politique sont pilotables et visibles
# ══════════════════════════════════════════════════════════════════════════════
#
# `ResolverPolicy` porte `allow_container`, `allow_local_build` et
# `allow_cpu_fallback`, qu'aucune option n'exposait. Depuis AUT-018, un opérateur
# pouvait fournir une variante `official-container` correctement épinglée par
# digest et la voir systématiquement écartée : il avait le moyen de l'épingler et
# aucun moyen de l'autoriser.

_PIN = ("--pin-version", "b6800", "--pin-commit", "a" * 40, "--min-build", "6600")


def test_cli_expose_les_trois_drapeaux_de_politique():
    options = _options_de_la_commande()
    for option in ("--allow-container", "--allow-local-build", "--no-local-build",
                   "--allow-cpu-fallback"):
        assert option in options, f"{option} absente de la CLI"


def _politique_du_plan(document) -> dict:
    section = next(s for s in document["sections"] if s["name"] == sc.SECTION_RUNTIME)
    return section["data"]["policy"]


def test_la_politique_retenue_est_inscrite_dans_le_plan(tmp_path):
    """
    AUT-019 — un plan doit dire sous quelles règles il a été calculé.

    « Aucune variante GPU sûre » ne veut pas dire la même chose selon que le
    conteneur était accepté ou non ; relire un plan sans sa politique, c'est le
    comparer à un autre sans savoir que les règles différaient.
    """
    resultat = _invoke(tmp_path, "--json", *_PIN)
    defaut = _politique_du_plan(json.loads(resultat.output))
    assert defaut == {
        "allow_container": False, "allow_local_build": True, "allow_cpu_fallback": False,
    }

    resultat = _invoke(
        tmp_path, "--json", *_PIN, "--allow-container", "--allow-cpu-fallback",
        "--no-local-build",
    )
    assert _politique_du_plan(json.loads(resultat.output)) == {
        "allow_container": True, "allow_local_build": False, "allow_cpu_fallback": True,
    }


def test_une_variante_conteneur_epinglee_reste_ecartee_sans_l_option(tmp_path):
    """Le défaut, tel qu'il se produisait : épinglée correctement, et jamais retenue."""
    plan = _plan(_options(tmp_path, resolver_policy=runtime_mod.ResolverPolicy(
        release=_release(), variants=(_variante_conteneur(),),
    )))
    section = _section_runtime(plan)

    assert section.data["resolved"] is False
    assert any("allow_container=False" in r for r in section.data["rejected"])


def test_une_variante_conteneur_epinglee_est_retenue_avec_l_option(tmp_path):
    """Le correctif : l'opérateur peut enfin autoriser ce qu'il a épinglé."""
    plan = _plan(_options(tmp_path, resolver_policy=runtime_mod.ResolverPolicy(
        release=_release(), variants=(_variante_conteneur(),), allow_container=True,
    )))
    section = _section_runtime(plan)

    assert section.data["resolved"] is True
    assert section.data["variant"]["source"] == runtime_mod.SOURCE_OFFICIAL_CONTAINER
    assert section.data["policy"]["allow_container"] is True


def _variante_conteneur() -> runtime_mod.ArtifactVariant:
    return runtime_mod.ArtifactVariant(
        source=runtime_mod.SOURCE_OFFICIAL_CONTAINER,
        backend="cuda12",
        platform="linux-x86_64",
        evidence=runtime_mod.EVIDENCE_OPERATOR,
        evidence_note="Digest relevé par l'opérateur pour ce test.",
        container_digest="sha256:" + "b" * 64,
    )


def _politique_cpu(**kwargs) -> runtime_mod.ResolverPolicy:
    """Aucune variante GPU : seule une variante CPU épinglée reste disponible."""
    return runtime_mod.ResolverPolicy(
        release=_release(),
        variants=(runtime_mod.ArtifactVariant(
            source=runtime_mod.SOURCE_OFFICIAL_RELEASE,
            backend="cpu",
            platform="linux-x86_64",
            evidence=runtime_mod.EVIDENCE_OPERATOR,
            evidence_note="Empreinte relevée par l'opérateur pour ce test.",
            artifact_sha256="e" * 64,
        ),),
        **kwargs,
    )


def test_sans_autorisation_le_repli_cpu_reste_refuse(tmp_path):
    """Le défaut qui compte : `allow_cpu_fallback` est faux, et le reste."""
    plan = _plan(_options(tmp_path, resolver_policy=_politique_cpu()))
    section = _section_runtime(plan)

    assert section.data["resolved"] is False
    assert section.data["degraded"] is False
    assert "cpu_fallback_refused" in {f.code for f in section.findings}
    # Aucune note : une politique par défaut n'a rien à annoncer.
    assert section.notes == ()


def test_un_repli_cpu_autorise_et_non_emprunte_se_voit_quand_meme(tmp_path):
    """
    L'autorisation seule est déjà une information : un plan calculé sous une
    politique permissive ne doit pas se lire comme un plan calculé sous la
    politique par défaut, même quand le repli n'a pas servi.
    """
    plan = _plan(_options(tmp_path, resolver_policy=runtime_mod.ResolverPolicy(
        release=_release(), allow_cpu_fallback=True,  # matrice livrée : local-build cuda12 gagne
    )))
    section = _section_runtime(plan)

    assert section.data["degraded"] is False
    assert section.data["variant"]["backend"] == "cuda12"
    assert "cpu_fallback_authorized" in {f.code for f in section.findings}
    assert any("non emprunté" in n for n in section.notes), section.notes


def test_un_repli_cpu_autorise_et_emprunte_est_annonce_en_toutes_lettres(tmp_path):
    """
    §6 : aucun repli CPU ne doit être silencieux. Quand il est autorisé ET pris,
    le plan doit le dire dans ce que l'opérateur LIT — `data` n'est pas imprimé
    par le rendu humain, `notes` l'est.
    """
    plan = _plan(_options(tmp_path, resolver_policy=_politique_cpu(allow_cpu_fallback=True)))
    section = _section_runtime(plan)

    assert section.data["resolved"] is True
    assert section.data["degraded"] is True
    assert section.data["variant"]["backend"] == "cpu"

    codes = {f.code for f in section.findings}
    assert {"cpu_fallback_authorized", "cpu_fallback_degraded"} <= codes
    assert section.status == "warn"

    texte = "\n".join(section.notes)
    assert "EMPRUNTÉ" in texte
    assert "CE PLAN EST DÉGRADÉ" in texte

    # Et le rendu humain — ce que l'opérateur voit réellement — le porte aussi.
    rendu = sc.render_human(plan, strict=False)
    assert "CE PLAN EST DÉGRADÉ" in rendu


def test_les_options_de_politique_exigent_un_epinglage(tmp_path):
    """
    Autoriser une branche de §6 sans épingler de version ne résout rien : la
    commande le dit au lieu de laisser croire que l'option a été prise en compte.
    """
    for option in ("--allow-container", "--allow-cpu-fallback", "--no-local-build"):
        resultat = _invoke(tmp_path, option)
        assert resultat.exit_code == sc.EXIT_USAGE, option
        assert "--pin-version" in _sans_ansi(resultat.output)
