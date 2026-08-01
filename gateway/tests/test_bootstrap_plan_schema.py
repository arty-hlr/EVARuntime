"""
AUT-001 — régressions du contrat de plan de bootstrap (`bootstrap/schema.py`).

Le plan est le seul artefact que l'opérateur lit AVANT qu'une machine ne soit
modifiée. Ces tests verrouillent les quatre promesses correspondantes :
versionné, validé, sans secret, lisible.

Chaque test d'ABSENCE porte son contrôle positif — un test qui affirme « aucune
fuite » sans prouver qu'il sait en voir une passerait au vert le jour où le
détecteur deviendrait inerte. C'est une règle du dépôt, elle a déjà attrapé de
vrais défauts.
"""
from __future__ import annotations

import json

import pytest

from bootstrap import schema as sc


# ── Fabriques ─────────────────────────────────────────────────────────────────

def _section(
    name: str = sc.SECTION_HARDWARE,
    status: str = "ok",
    findings: tuple[sc.Finding, ...] = (),
    data: dict | None = None,
) -> sc.PlanSection:
    return sc.PlanSection(
        name=name,
        version=1,
        status=status,  # type: ignore[arg-type]
        summary=f"section {name} en état {status}",
        data=data if data is not None else {"probe": "ok"},
        findings=findings,
    )


def _plan(**kwargs) -> sc.BootstrapPlan:
    base = {
        "generated_at": "2026-07-31T09:00:00Z",
        "mode": "local",
        "sections": (_section(),),
        "steps": (
            sc.PlanStep(
                order=1,
                action=sc.ACTION_DOWNLOAD_MODEL,
                target="qwen2.5-0.5b-instruct-q4-k-m",
                detail="Télécharger le GGUF à révision figée puis vérifier son SHA-256.",
                requires_root=False,
                reversible=True,
                estimated_bytes=400 * 1024 * 1024,
            ),
        ),
        "decisions": (
            sc.Decision(
                topic="runtime",
                choice="local-build",
                rationale="aucun artefact CUDA officiel ne couvre cette plateforme",
                rejected=("official-release",),
            ),
        ),
    }
    base.update(kwargs)
    return sc.BootstrapPlan(**base)  # type: ignore[arg-type]


# ── Promesse 1 : versionné ────────────────────────────────────────────────────

def test_plan_valide_ne_produit_aucune_erreur():
    """Contrôle positif de toute la section validation : le cas sain est sain."""
    assert sc.validate_plan_dict(_plan().to_dict()) == ()


def test_document_porte_sa_version_et_son_outil():
    document = _plan().to_dict()
    assert document["schema_version"] == sc.PLAN_SCHEMA_VERSION
    assert document["tool"] == sc.PLAN_TOOL_NAME


def test_schema_plus_recent_que_le_lecteur_est_refuse():
    """Un plan écrit par une version future ne doit pas être lu « au mieux »."""
    document = _plan().to_dict()
    document["schema_version"] = sc.PLAN_SCHEMA_VERSION + 1
    errors = sc.validate_plan_dict(document)
    assert any("plus récent" in e for e in errors), errors


def test_outil_inattendu_est_refuse():
    document = _plan().to_dict()
    document["tool"] = "autre-outil"
    assert any("tool" in e for e in sc.validate_plan_dict(document))


@pytest.mark.parametrize("valeur", ["1", 1.0, True, None])
def test_version_non_entiere_est_refusee(valeur):
    document = _plan().to_dict()
    document["schema_version"] = valeur
    assert any("schema_version" in e for e in sc.validate_plan_dict(document))


# ── Promesse 2 : validé ───────────────────────────────────────────────────────

def test_document_non_objet_est_refuse():
    assert sc.validate_plan_dict(["pas", "un", "objet"])


def test_section_de_nom_inconnu_est_refusee():
    document = _plan().to_dict()
    document["sections"][0]["name"] = "telemetrie"
    assert any("name inconnu" in e for e in sc.validate_plan_dict(document))


def test_deux_sections_de_meme_nom_sont_refusees():
    """Deux producteurs ne doivent pas publier la même donnée sous le même nom."""
    plan = _plan(sections=(_section(), _section()))
    assert any("en double" in e for e in sc.validate_plan_dict(plan.to_dict()))


def test_statut_de_section_invalide_est_refuse():
    document = _plan().to_dict()
    document["sections"][0]["status"] = "presque"
    assert any("status invalide" in e for e in sc.validate_plan_dict(document))


def test_section_sans_resume_est_refusee():
    document = _plan().to_dict()
    document["sections"][0]["summary"] = ""
    assert any("summary" in e for e in sc.validate_plan_dict(document))


def test_constat_incomplet_est_refuse():
    """Un code sans message n'est pas actionnable, un message sans code n'est pas testable."""
    document = _plan().to_dict()
    document["sections"][0]["findings"] = [{"code": "", "level": "warn", "message": "x"}]
    assert any("code" in e for e in sc.validate_plan_dict(document))

    document["sections"][0]["findings"] = [{"code": "c", "level": "grave", "message": "x"}]
    assert any("level" in e for e in sc.validate_plan_dict(document))


def test_action_hors_du_vocabulaire_ferme_est_refusee():
    """
    Un plan ne doit pas pouvoir décrire une action que l'applicateur ne sait pas
    exécuter, ni la légitimer en la nommant librement.
    """
    document = _plan().to_dict()
    document["steps"][0]["action"] = "rm_rf"
    assert any("action inconnue" in e for e in sc.validate_plan_dict(document))


def test_numerotation_des_etapes_doit_etre_continue():
    """Un trou dans l'ordre signifie qu'une étape a été perdue à l'assemblage."""
    plan = _plan(steps=(
        sc.PlanStep(1, sc.ACTION_VERIFY_ARTIFACT, "a", "d"),
        sc.PlanStep(3, sc.ACTION_ENABLE_MODEL, "b", "d"),
    ))
    errors = sc.validate_plan_dict(plan.to_dict())
    assert any("numérotation continue" in e for e in errors), errors


def test_drapeaux_d_etape_doivent_etre_booleens():
    document = _plan().to_dict()
    document["steps"][0]["requires_root"] = "oui"
    assert any("requires_root" in e for e in sc.validate_plan_dict(document))


def test_taille_negative_est_refusee():
    document = _plan().to_dict()
    document["steps"][0]["estimated_bytes"] = -1
    assert any("estimated_bytes" in e for e in sc.validate_plan_dict(document))


def test_decision_sans_justification_est_refusee():
    """
    Condition de sortie du jalon M1 : le système sait expliquer ce qu'il
    installera ET POURQUOI. Une décision sans `rationale` ne tient pas ce contrat.
    """
    document = _plan().to_dict()
    document["decisions"][0]["rationale"] = ""
    assert any("rationale" in e for e in sc.validate_plan_dict(document))


# ── Cohérence du verdict : un champ dérivé doit être recoupable ───────────────
#
# Ces régressions comptent surtout pour M2, qui appliquera des plans RELUS depuis
# un fichier. Un applicateur ne doit jamais pouvoir être convaincu d'agir par un
# champ dérivé que personne ne recoupe.

def test_un_verdict_falsifie_est_refuse():
    """
    Un document portant une section `fail` mais retouché en `status: ok`,
    `applicable: true`, `exit_code: 0`, `blockers: []` passait la validation sans
    une seule erreur. Il ne passe plus.
    """
    plan = _plan(sections=(_section(status="fail", findings=(sc.Finding("x", "fail", "dur"),)),))
    document = plan.to_dict()
    document["status"] = "ok"
    document["applicable"] = True
    document["exit_code"] = sc.EXIT_OK
    document["blockers"] = []
    document["counts"] = {"ok": 1, "warn": 0, "fail": 0, "skip": 0, "steps": 1, "decisions": 1}

    errors = sc.validate_plan_dict(document)
    for champ in ("status", "applicable", "exit_code", "blockers", "counts"):
        assert any(champ in e for e in errors), f"{champ} non recoupé : {errors}"


@pytest.mark.parametrize("champ,valeur", [
    ("status", "blocked"),
    ("applicable", False),
    ("exit_code", 1),
    ("estimated_download_bytes", 42),
])
def test_chaque_champ_derive_est_recoupe_isolement(champ, valeur):
    """Un seul champ retouché suffit à faire échouer la validation."""
    document = _plan().to_dict()
    assert sc.validate_plan_dict(document) == ()   # contrôle positif
    document[champ] = valeur
    assert any(champ in e for e in sc.validate_plan_dict(document))


def test_un_plan_bloque_ne_publie_plus_ses_etapes():
    """
    Le rendu lui-même retire les étapes d'un plan bloqué : elles restent portées
    par l'objet — le planificateur les a calculées — mais ne sont ni publiées ni
    imprimées tant que le verdict interdit l'application.
    """
    plan = _plan(sections=(_section(status="fail", findings=(sc.Finding("x", "fail", "dur"),)),))
    assert plan.steps, "la fabrique doit fournir au moins une étape"
    document = plan.to_dict()
    assert document["steps"] == []
    assert document["counts"]["steps"] == 0
    assert document["estimated_download_bytes"] == 0
    assert sc.validate_plan_dict(document) == ()
    # L'opérateur doit savoir que des étapes existaient, sinon il cherche au
    # mauvais endroit en croyant à un plan vide.
    texte = sc.render_human(plan)
    assert "Le plan est bloqué" in texte and "ne sont pas" in texte


def test_un_document_bloque_portant_des_etapes_est_rejete():
    """
    L'invariant le plus lourd de conséquence, verrouillé pour un document VENU DE
    L'EXTÉRIEUR : un applicateur qui lirait des étapes dans un plan bloqué
    pourrait en exécuter la moitié — celle qui consomme disque et réseau.
    """
    document = _plan().to_dict()
    assert document["steps"], "contrôle positif : le cas sain porte bien des étapes"
    # Falsification : on ajoute un constat bloquant sans retirer les étapes,
    # exactement ce que produirait une édition à la main ou un outil tiers.
    document["sections"][0]["status"] = "fail"
    document["sections"][0]["findings"] = [{"code": "x", "level": "fail", "message": "dur"}]
    errors = sc.validate_plan_dict(document)
    assert any("aucune action" in e for e in errors), errors


def test_strict_bloque_reellement_le_plan_et_pas_seulement_son_code_de_sortie():
    """
    Régression : `--strict` promouvait les avertissements pour le code de sortie
    mais **pas** pour `applicable` ni pour les étapes. Le document se
    contredisait : code 1, statut `fail`, et pourtant `applicable: true` avec
    neuf actions à exécuter — et le validateur n'y trouvait rien à redire.
    """
    plan = _plan(sections=(
        _section(status="warn", findings=(sc.Finding("gpu_absent", "warn", "Aucun GPU."),)),
    ))
    souple = plan.to_dict()
    assert souple["applicable"] is True and souple["steps"], "contrôle positif : sans strict, applicable"

    strict = plan.to_dict(strict=True)
    assert strict["status"] == "fail"
    assert strict["exit_code"] == sc.EXIT_BLOCKED
    assert strict["applicable"] is False
    assert strict["steps"] == []
    assert strict["counts"]["steps"] == 0
    assert strict["estimated_download_bytes"] == 0
    assert sc.validate_plan_dict(strict) == ()


def test_strict_retire_aussi_les_etapes_du_rendu_humain():
    plan = _plan(sections=(
        _section(status="warn", findings=(sc.Finding("gpu_absent", "warn", "Aucun GPU."),)),
    ))
    assert "download_model" in sc.render_human(plan)          # contrôle positif
    assert "download_model" not in sc.render_human(plan, strict=True)


def test_une_liste_recapitulative_falsifiee_est_refusee_meme_a_longueur_egale():
    """
    Comparer les longueurs ne suffisait pas : une liste `warnings` entièrement
    inventée mais de même taille passait sans un mot. Or c'est précisément ce
    qu'un opérateur lit en diagonale avant de décider d'appliquer.
    """
    plan = _plan(sections=(
        _section(status="warn", findings=(sc.Finding("gpu_absent", "warn", "Aucun GPU."),)),
    ))
    document = plan.to_dict()
    assert sc.validate_plan_dict(document) == ()              # contrôle positif
    document["warnings"] = [{"code": "tout_va_bien", "level": "warn", "message": "rien à signaler"}]
    errors = sc.validate_plan_dict(document)
    assert any("warnings[0].code" in e for e in errors), errors


def test_un_bloqueur_falsifie_est_refuse_meme_a_longueur_egale():
    plan = _plan(sections=(_section(status="fail", findings=(sc.Finding("x", "fail", "dur"),)),))
    document = plan.to_dict()
    document["blockers"] = [{"code": "x", "level": "fail", "message": "message réécrit"}]
    assert any("blockers[0].message" in e for e in sc.validate_plan_dict(document))


@pytest.mark.parametrize("champ,valeur", [
    ("warnings", 7),
    ("warnings", None),
    ("blockers", "aucun"),
    ("counts", []),
    ("applicable", "oui"),
    ("exit_code", "0"),
    ("estimated_download_bytes", "0"),
    ("strict", "false"),
])
def test_un_champ_recapitulatif_mal_type_produit_une_erreur_pas_une_exception(champ, valeur):
    """
    `warnings: 7` faisait lever un TypeError : un validateur qui plante n'est pas
    un validateur, il est un déni de service sur le chemin de la relecture.
    Et `strict: "false"` était converti en VRAI par `bool(...)` — toute chaîne non
    vide l'est — donc le verdict était recalculé en mode strict à contresens.
    """
    document = _plan().to_dict()
    document[champ] = valeur
    errors = sc.validate_plan_dict(document)                  # ne doit pas lever
    assert any(champ in e and "doit être" in e for e in errors), errors


@pytest.mark.parametrize("champ", [
    "applicable", "strict", "exit_code", "estimated_download_bytes",
    "counts", "blockers", "warnings",
])
def test_un_champ_recapitulatif_absent_est_refuse(champ):
    document = _plan().to_dict()
    del document[champ]
    assert any(champ in e and "obligatoire" in e for e in sc.validate_plan_dict(document))


@pytest.mark.parametrize("champ", ["ok", "warn", "fail", "skip", "steps", "decisions"])
@pytest.mark.parametrize(
    "conversion",
    [pytest.param(bool, id="booleen"), pytest.param(float, id="flottant")],
)
def test_chaque_compteur_exige_un_entier_exact(champ, conversion):
    """
    Régression : l'égalité Python assimile `True` à `1`, `False` à `0` et `1.0`
    à `1`. Comparer seulement le dictionnaire recalculé laissait donc passer un
    document JSON dont les compteurs violaient le contrat.
    """
    document = _plan().to_dict()
    document["counts"][champ] = conversion(document["counts"][champ])
    errors = sc.validate_plan_dict(document)
    assert any(f"counts.{champ}" in e and "entier" in e for e in errors), errors


def test_counts_refuse_les_cles_manquantes_et_inconnues():
    document = _plan().to_dict()
    del document["counts"]["steps"]
    document["counts"]["telechargements"] = 1
    errors = sc.validate_plan_dict(document)
    assert any("counts.steps" in e and "obligatoire" in e for e in errors), errors
    assert any("clé inconnue" in e and "telechargements" in e for e in errors), errors


def test_le_verdict_n_est_recoupe_que_si_la_structure_tient():
    """
    Recalculer un verdict depuis des sections mal formées produirait du bruit et
    noierait la vraie erreur. La validation de structure passe d'abord.
    """
    document = _plan().to_dict()
    document["sections"][0]["status"] = "presque"
    errors = sc.validate_plan_dict(document)
    assert any("status invalide" in e for e in errors)
    assert not any("les sections donnent" in e for e in errors)


# ── Statuts, bloqueurs et codes de sortie ─────────────────────────────────────

def test_plan_sain_est_applicable_et_sort_en_zero():
    plan = _plan()
    assert plan.status() == "ok"
    assert plan.applicable is True
    assert plan.exit_code() == sc.EXIT_OK


def test_avertissement_ne_bloque_pas_mais_se_voit():
    plan = _plan(sections=(
        _section(status="warn", findings=(sc.Finding("gpu_absent", "warn", "Aucun GPU détecté."),)),
    ))
    assert plan.status() == "warn"
    assert plan.applicable is True
    assert plan.exit_code() == sc.EXIT_WARNINGS
    assert [f.code for f in plan.warnings] == ["gpu_absent"]


def test_mode_strict_transforme_un_avertissement_en_blocage():
    plan = _plan(sections=(
        _section(status="warn", findings=(sc.Finding("gpu_absent", "warn", "Aucun GPU détecté."),)),
    ))
    assert plan.exit_code(strict=True) == sc.EXIT_BLOCKED


def test_section_en_echec_bloque_le_plan():
    plan = _plan(sections=(
        _section(status="fail", findings=(sc.Finding("catalog_unpinned", "fail", "SHA-256 absent."),)),
    ))
    assert plan.status() == "blocked"
    assert plan.applicable is False
    assert plan.exit_code() == sc.EXIT_BLOCKED
    assert [f.code for f in plan.blockers] == ["catalog_unpinned"]


def test_echec_sans_constat_explicite_produit_quand_meme_un_bloqueur():
    """
    Un producteur qui rend `fail` sans constat de niveau `fail` ne doit pas
    produire un plan silencieusement applicable. Le bloqueur est matérialisé.
    """
    plan = _plan(sections=(_section(status="fail", findings=()),))
    assert plan.applicable is False
    assert [f.code for f in plan.blockers] == ["hardware_failed"]


def test_un_constat_fail_bloque_meme_dans_une_section_warn():
    """
    Piège signalé indépendamment par les chantiers AUT-003 et AUT-005 : la
    première écriture ne collectait les `fail` que dans les sections déjà
    `fail`. Un producteur dont le calcul de statut diverge un peu de ses propres
    constats voyait son bloqueur disparaître, et le plan sortait applicable.
    """
    plan = _plan(sections=(
        _section(status="warn", findings=(
            sc.Finding("catalog_unpinned", "fail", "SHA-256 absent sur une entrée."),
        )),
    ))
    assert [f.code for f in plan.blockers] == ["catalog_unpinned"]
    assert plan.applicable is False
    assert plan.exit_code() == sc.EXIT_BLOCKED


def test_un_constat_fail_bloque_meme_dans_une_section_ok():
    """Même piège, cas extrême : le statut de section ne peut pas absoudre un `fail`."""
    plan = _plan(sections=(
        _section(status="ok", findings=(sc.Finding("x", "fail", "défaut dur"),)),
    ))
    assert plan.applicable is False


def test_section_ignoree_ne_degrade_pas_le_plan():
    """`skip` est un cas légitime — LLMfit absent n'est pas une panne."""
    plan = _plan(sections=(_section(sc.SECTION_RECOMMENDATION, status="skip"),))
    assert plan.status() == "ok"
    assert plan.applicable is True


def test_volume_total_agrege_les_etapes():
    plan = _plan(steps=(
        sc.PlanStep(1, sc.ACTION_DOWNLOAD_MODEL, "a", "d", estimated_bytes=1000),
        sc.PlanStep(2, sc.ACTION_DOWNLOAD_MODEL, "b", "d", estimated_bytes=2000),
        sc.PlanStep(3, sc.ACTION_ENABLE_MODEL, "c", "d", estimated_bytes=None),
    ))
    assert plan.total_download_bytes() == 3000


def test_recherche_de_section_par_nom():
    plan = _plan()
    assert plan.section(sc.SECTION_HARDWARE) is not None
    assert plan.section(sc.SECTION_CATALOG) is None


# ── Promesse 3 : sans secret ──────────────────────────────────────────────────

def test_le_detecteur_voit_un_champ_sensible_portant_une_valeur():
    """Contrôle positif du détecteur : sans lui, tous les tests d'absence sont inertes."""
    leaks = sc.find_secret_leaks({"source": {"hf_token": "valeur-quelconque"}})
    assert leaks and "source.hf_token" in leaks[0]


def test_un_booleen_de_presence_ne_fuit_pas():
    """La façon RECOMMANDÉE de dire « un token est présent » sans le dire."""
    assert sc.find_secret_leaks({"source": {"hf_token": True, "api_key": None}}) == ()


@pytest.mark.parametrize("champ", [
    "token", "hf_token", "access_token", "auth_token", "refresh_token", "token_file",
])
def test_un_porteur_dauthentification_reste_une_fuite(champ):
    """Le resserrage du motif ne doit rien laisser passer de ce qu'il attrapait."""
    assert sc.find_secret_leaks({champ: "valeur-quelconque"}), champ


@pytest.mark.parametrize("champ", [
    "prompt_tokens", "completion_tokens", "total_tokens", "max_tokens",
    "prompt_tokens_per_second", "generation_tokens_per_second", "tokens_per_second",
    "first_token_ms", "time_to_first_token_ms", "token_count", "n_tokens",
])
def test_un_comptage_de_jetons_nest_pas_un_secret(champ):
    """
    Dans une passerelle LLM, un jeton est d'abord une unité de facturation.

    Le motif `TOKEN` non ancré frappait ces noms en sous-chaîne, si bien que le
    rapport de calibration prescrit par §9 — qui exige `prompt_tokens_per_second`
    — était littéralement impubliable : `render_json()` refusait de le rendre.
    Trois chantiers de la vague 6 ont buté dessus indépendamment et l'ont
    contourné chacun de son côté avant qu'on corrige la cause.
    """
    # Contrôle positif : le détecteur voit toujours quelque chose sur ce document.
    assert sc.find_secret_leaks({champ: 42, "hf_token": "x"}), champ
    assert sc.find_secret_leaks({champ: 42}) == (), champ


@pytest.mark.parametrize("valeur,motif", [
    ("hf_abcdefghijklmnopqrstuvwx", "hf_token"),
    ("sk-abcdefghijklmnopqrstuvwxyz", "openai_key"),
    ("llmgw-abcdefghijklmnop", "gateway_key"),
    ("Authorization: Bearer abcdefghijklmnopqrst", "bearer_header"),
    ("-----BEGIN RSA PRIVATE KEY-----", "pem_private_key"),
    ("https://alice:motdepasse@interne.example/models", "url_credentials"),
])
def test_le_detecteur_voit_une_valeur_sensible_sous_un_nom_anodin(valeur, motif):
    """
    Second filet : un secret rangé sous `note` ou `cmd` échappe au filtre par nom.
    Les deux filets sont nécessaires, aucun ne couvre l'autre.
    """
    leaks = sc.find_secret_leaks({"steps": [{"note": valeur}]})
    assert leaks, f"{motif} non détecté"
    assert motif in leaks[0]


def test_le_rapport_de_fuite_ne_recopie_jamais_la_valeur():
    """Un rapport de fuite qui recopie le secret est lui-même une fuite."""
    secret = "hf_abcdefghijklmnopqrstuvwx"
    leaks = sc.find_secret_leaks({"note": secret})
    assert leaks
    assert all(secret not in leak for leak in leaks)
    # Contrôle positif : le rapport désigne bien le chemin fautif.
    assert any("note" in leak for leak in leaks)


def test_rendu_json_refuse_de_publier_un_plan_qui_fuit():
    plan = _plan(sections=(_section(data={"hf_token": "hf_abcdefghijklmnopqrstuvwx"}),))
    with pytest.raises(sc.PlanError) as exc:
        sc.render_json(plan)
    assert "sensibles" in str(exc.value)


def test_rendu_humain_refuse_aussi():
    """Les deux rendus passent par le même garde — pas de porte de service."""
    plan = _plan(sections=(_section(data={"note": "sk-abcdefghijklmnopqrstuvwxyz"}),))
    with pytest.raises(sc.PlanError):
        sc.render_human(plan)


def test_assert_no_secrets_laisse_passer_un_document_sain():
    sc.assert_no_secrets(_plan().to_dict())  # ne doit pas lever


# ── Promesse 4 : lisible avant application ────────────────────────────────────

def test_le_rendu_humain_dit_que_rien_n_a_ete_modifie():
    """
    La phrase importe : un opérateur qui lit un plan doit savoir qu'aucune
    machine n'a été touchée pour le produire.
    """
    texte = sc.render_human(_plan())
    assert "Rien n'a été installé" in texte


def test_le_rendu_humain_expose_decision_justification_et_etapes():
    texte = sc.render_human(_plan())
    assert "local-build" in texte
    assert "aucun artefact CUDA officiel" in texte
    assert "download_model" in texte
    assert "qwen2.5-0.5b-instruct-q4-k-m" in texte


def test_le_rendu_humain_signale_root_et_irreversible():
    plan = _plan(steps=(
        sc.PlanStep(1, sc.ACTION_WRITE_REGISTRY, "models.yaml", "Écrire le registre.",
                    requires_root=True, reversible=False),
    ))
    texte = sc.render_human(plan)
    assert "root" in texte
    assert "IRRÉVERSIBLE" in texte


def test_le_rendu_humain_liste_les_bloqueurs_et_le_dit():
    plan = _plan(sections=(
        _section(status="fail", findings=(sc.Finding("catalog_unpinned", "fail", "SHA-256 absent."),)),
    ))
    texte = sc.render_human(plan)
    assert "BLOQUEURS" in texte
    assert "ne rien appliquer" in texte
    assert "catalog_unpinned" in texte


def test_le_rendu_humain_dit_aucune_plutot_que_de_taire_une_section_vide():
    """Test d'absence, avec son contrôle positif juste en dessous."""
    texte = sc.render_human(_plan(steps=(), decisions=()))
    assert texte.count("(aucune)") >= 2
    # Contrôle positif : le même rendu SAIT afficher des étapes quand il y en a.
    assert "download_model" in sc.render_human(_plan())


def test_le_rendu_json_est_du_json_valide_et_revalidable():
    """Aller-retour complet : ce qu'on publie doit repasser notre propre validation."""
    document = json.loads(sc.render_json(_plan()))
    assert sc.validate_plan_dict(document) == ()


def test_le_rendu_json_preserve_les_accents():
    """`ensure_ascii=False` : un plan illisible en français n'est pas lisible."""
    assert "Télécharger" in sc.render_json(_plan())


def test_chaque_section_declaree_porte_un_libelle_humain():
    """
    Sans ce test, ajouter une section au contrat sans lui donner de libellé
    passerait inaperçu — le rendu retomberait sur le nom technique.
    """
    for name in sc.SECTION_NAMES:
        assert name in sc._SECTION_LABEL, f"section {name} sans libellé humain"


# ── Utilitaires du contrat ────────────────────────────────────────────────────

def test_les_notes_de_section_apparaissent_en_rendu_humain():
    """
    Besoin remonté par AUT-004 : les huit limites de LLMfit ne vivaient que dans
    `data`, que le rendu humain n'imprime pas — elles n'existaient donc qu'en
    JSON, invisibles de l'opérateur qui relit le plan.
    """
    plan = _plan(sections=(
        sc.PlanSection(
            name=sc.SECTION_RECOMMENDATION, version=1, status="ok",
            summary="conseil consultatif",
            notes=("Ce que LLMfit ignore :", "  · le coût de ctx_size × parallel",),
        ),
    ))
    texte = sc.render_human(plan)
    assert "Ce que LLMfit ignore" in texte
    assert "ctx_size × parallel" in texte


def test_une_note_vide_est_refusee():
    document = _plan().to_dict()
    document["sections"][0]["notes"] = [""]
    assert any("notes" in e for e in sc.validate_plan_dict(document))


@pytest.mark.parametrize("niveaux,attendu", [
    ((), "ok"),
    (("info",), "ok"),
    (("info", "warn"), "warn"),
    (("warn", "fail"), "fail"),
    (("fail", "warn"), "fail"),
])
def test_statut_derive_des_constats(niveaux, attendu):
    findings = tuple(sc.Finding(f"c{i}", n, "m") for i, n in enumerate(niveaux))  # type: ignore[arg-type]
    assert sc.status_from_findings(findings) == attendu


def test_un_constat_info_ne_transforme_pas_un_skip_en_ok():
    """Une section non applicable qui émet un `info` reste non applicable."""
    findings = (sc.Finding("llmfit_absent", "info", "LLMfit n'est pas installé."),)
    assert sc.status_from_findings(findings, default="skip") == "skip"
    # Contrôle positif : la même fonction sait bien produire `ok` par défaut.
    assert sc.status_from_findings(findings) == "ok"


def test_fusion_de_constats_dedoublonne_par_code_et_preserve_l_ordre():
    a = (sc.Finding("x", "warn", "premier"), sc.Finding("y", "info", "second"))
    b = (sc.Finding("x", "fail", "doublon"), sc.Finding("z", "warn", "troisième"))
    fusion = sc.merge_findings(a, b)
    assert [f.code for f in fusion] == ["x", "y", "z"]
    assert fusion[0].message == "premier"


@pytest.mark.parametrize("entrees,attendu", [
    (("ok", "ok"), "ok"),
    (("ok", "warn"), "warn"),
    (("warn", "fail"), "fail"),
    (("skip", "skip"), "skip"),
    (("skip", "ok"), "ok"),
])
def test_statut_le_plus_severe(entrees, attendu):
    assert sc.worst_status(*entrees) == attendu


def test_skip_seul_ne_devient_pas_ok():
    """Une section ignorée n'est pas une section réussie — la nuance compte au rendu."""
    assert sc.worst_status("skip") == "skip"


def test_le_vocabulaire_d_actions_est_ferme():
    """
    Contrôle positif de `test_action_hors_du_vocabulaire_ferme_est_refusee` :
    les constantes exportées appartiennent bien à l'ensemble validé.
    """
    for action in (sc.ACTION_INSTALL_RUNTIME, sc.ACTION_DOWNLOAD_MODEL,
                   sc.ACTION_WRITE_REGISTRY, sc.ACTION_SMOKE_TEST):
        assert action in sc.PLAN_ACTIONS
