"""
AUT-011 — régressions du rapport d'installation (`bootstrap/install_report.py`).

Ce document est le dernier de la chaîne de bootstrap et le seul qu'un auditeur
lira des mois plus tard, sans les originaux sous la main. Ces tests verrouillent
cinq familles d'invariants :

1. **il ne prétend jamais plus que ce qui a été fait** — une condition de M2
   qu'aucune étape ne couvre reste `unproven`, une simulation n'en satisfait
   aucune, et le verdict ne peut pas être « complet » dans ces cas-là ;
2. **il distingue le constat de l'hypothèse** — un marqueur de preuve inconnu
   est traité comme une hypothèse, jamais comme un constat ;
3. **il refuse des sources qui ne vont pas ensemble** — plan invalide, plan non
   applicable, journal parlant d'un autre plan ;
4. **les champs récapitulatifs sont recalculés** — conditions, index, compteurs,
   verdict et empreintes sont reconstruits depuis les documents embarqués, et un
   rapport falsifié à la main est rejeté ;
5. **aucun secret ne sort** — ni en JSON, ni en français, ni sur disque.

Chaque test d'ABSENCE porte son contrôle positif : un test qui affirme « aucun
secret » ou « aucune condition manquante » sans prouver qu'il saurait en voir un
passerait au vert le jour où le détecteur deviendrait inerte.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from bootstrap import execution as ex
from bootstrap import install_report as ir
from bootstrap import schema as sc

# Construit à l'exécution : un littéral ressemblant à un vrai jeton n'a rien à
# faire dans un dépôt, même en fixture.
FAUX_TOKEN = "hf_" + "B" * 24

PLAN_DATE = "2026-08-01T09:00:00Z"

# Les six actions qui prouvent les six premières conditions de M2, dans l'ordre
# d'un bootstrap réel.
ACTIONS_M2 = (
    sc.ACTION_INSTALL_RUNTIME,
    sc.ACTION_VERIFY_ARTIFACT,
    sc.ACTION_DOWNLOAD_MODEL,
    sc.ACTION_ACCEPT_LICENSE,
    sc.ACTION_CALIBRATE_MODEL,
    sc.ACTION_WARMUP_MODEL,
    sc.ACTION_SMOKE_TEST,
)


# ── Fabriques ─────────────────────────────────────────────────────────────────

def horloge(valeur: str = "2026-08-01T12:00:00Z"):
    """Horloge injectable : aucun test de ce fichier ne dépend de l'heure réelle."""
    return lambda: valeur


def section_runtime(*, evidence: str = "constat-§6", note: str = "vérifié") -> sc.PlanSection:
    return sc.PlanSection(
        name=sc.SECTION_RUNTIME,
        version=1,
        status="ok",
        summary="Runtime résolu.",
        data={
            "variant": {
                "source": "local-build",
                "backend": "cuda12",
                "evidence": evidence,
                "evidence_note": note,
                "artifact_sha256": "a" * 64,
            },
            "version": "b6099",
            "commit": "c" * 40,
        },
        notes=("Le build local est exercé, pas supposé.",),
    )


def section_catalogue() -> sc.PlanSection:
    return sc.PlanSection(
        name=sc.SECTION_CATALOG,
        version=1,
        status="ok",
        summary="Catalogue approuvé lu.",
        data={
            "entries": [
                {
                    "id": "qwen2.5-0.5b",
                    "revision": "d" * 40,
                    "license": {"base_model": "apache-2.0", "fine_tune": "apache-2.0"},
                    "files": [{"name": "modele.gguf", "sha256": "e" * 64}],
                }
            ],
        },
    )


def plan_document(
    *,
    actions=ACTIONS_M2,
    sections=None,
    decisions=None,
    generated_at: str = PLAN_DATE,
) -> dict:
    """Un document de plan valide, applicable, et sans secret."""
    steps = tuple(
        sc.PlanStep(
            order=index + 1,
            action=action,
            target=f"cible-{action}",
            detail=f"Détail de {action}.",
        )
        for index, action in enumerate(actions)
    )
    if sections is None:
        sections = (section_runtime(), section_catalogue())
    if decisions is None:
        decisions = (
            sc.Decision(
                topic="runtime llama-server",
                choice="local-build/cuda12",
                rationale="aucune archive officielle CUDA n'existe pour cette plateforme",
                rejected=("official-release/cpu : backend inférieur",),
            ),
        )
    plan = sc.BootstrapPlan(
        generated_at=generated_at,
        mode="local",
        sections=tuple(sections),
        steps=steps,
        decisions=tuple(decisions),
    )
    return plan.to_dict()


def journal(
    plan: dict,
    statuses,
    *,
    mode: ex.ExecutionMode = ex.ExecutionMode.APPLY,
    evidences=None,
) -> ex.ExecutionReport:
    """Un journal d'exécution cohérent avec `plan`, statut par statut."""
    resultats = []
    for index, brut in enumerate(plan["steps"]):
        statut = statuses[index]
        etape = sc.PlanStep(
            order=brut["order"],
            action=brut["action"],
            target=brut["target"],
            detail=brut["detail"],
        )
        resultats.append(ex.StepResult.for_step(
            etape,
            status=statut,
            summary=f"{brut['action']} → {statut}",
            duration_ms=10 * (index + 1),
            evidence=(evidences or {}).get(brut["action"], {}),
            error="boum" if statut == ex.STEP_FAILED else None,
        ))
    return ex.ExecutionReport(
        started_at="2026-08-01T11:00:00Z",
        finished_at="2026-08-01T11:05:00Z",
        mode=mode,
        plan_fingerprint=ex.plan_fingerprint(plan),
        plan_generated_at=plan["generated_at"],
        results=tuple(resultats),
    )


def rapport_complet() -> ir.InstallReport:
    plan = plan_document()
    return ir.build_install_report(
        plan_document=plan,
        execution_report=journal(plan, [ex.STEP_DONE] * len(ACTIONS_M2)),
        now=horloge(),
    )


# ── 1. Le rapport ne prétend pas plus que ce qui a été fait ───────────────────

def test_installation_complete_satisfait_les_sept_conditions():
    rapport = rapport_complet()
    statuts = {c.code: c.status for c in rapport.conditions()}

    assert len(statuts) == 7
    assert set(statuts.values()) == {ir.CONDITION_SATISFIED}
    assert rapport.verdict() == ir.VERDICT_COMPLETE
    assert rapport.exit_code() == ir.EXIT_OK
    assert rapport.unmet_conditions() == ()


def test_simulation_ne_satisfait_aucune_condition_d_installation():
    plan = plan_document()
    rapport = ir.build_install_report(
        plan_document=plan,
        execution_report=journal(
            plan, [ex.STEP_WOULD_APPLY] * len(ACTIONS_M2), mode=ex.ExecutionMode.DRY_RUN
        ),
        now=horloge(),
    )
    statuts = {c.code: c.status for c in rapport.conditions()}

    # Contrôle positif : la septième condition, elle, est bien satisfaite —
    # le test saurait donc voir une condition satisfaite s'il y en avait.
    assert statuts["report_produced"] == ir.CONDITION_SATISFIED
    assert all(
        statut == ir.CONDITION_UNPROVEN
        for cle, statut in statuts.items()
        if cle != "report_produced"
    )
    assert rapport.verdict() == ir.VERDICT_PARTIAL
    assert rapport.exit_code() == ir.EXIT_PARTIAL


def test_une_condition_sans_etape_reste_non_prouvee():
    """L'absence de preuve n'est pas une preuve : elle ne devient jamais un succès."""
    plan = plan_document(actions=(sc.ACTION_INSTALL_RUNTIME, sc.ACTION_VERIFY_ARTIFACT))
    rapport = ir.build_install_report(
        plan_document=plan,
        execution_report=journal(plan, [ex.STEP_DONE, ex.STEP_DONE]),
        now=horloge(),
    )
    statuts = {c.code: c.status for c in rapport.conditions()}

    # Contrôle positif : la condition couverte par des étapes est satisfaite.
    assert statuts["runtime_installed"] == ir.CONDITION_SATISFIED
    assert statuts["model_downloaded"] == ir.CONDITION_UNPROVEN
    assert statuts["e2e_call"] == ir.CONDITION_UNPROVEN
    assert rapport.verdict() == ir.VERDICT_PARTIAL


def test_une_etape_sautee_rend_sa_condition_non_satisfaite():
    plan = plan_document()
    statuses = [ex.STEP_DONE] * len(ACTIONS_M2)
    statuses[ACTIONS_M2.index(sc.ACTION_SMOKE_TEST)] = ex.STEP_SKIPPED
    rapport = ir.build_install_report(
        plan_document=plan,
        execution_report=journal(plan, statuses),
        now=horloge(),
    )
    statuts = {c.code: c.status for c in rapport.conditions()}

    assert statuts["e2e_call"] == ir.CONDITION_UNSATISFIED
    assert statuts["warmed_up"] == ir.CONDITION_SATISFIED  # contrôle positif
    assert rapport.verdict() == ir.VERDICT_PARTIAL


def test_un_echec_donne_un_verdict_en_echec_et_le_code_1():
    plan = plan_document()
    statuses = [ex.STEP_DONE, ex.STEP_FAILED] + [ex.STEP_NOT_ATTEMPTED] * 5
    rapport = ir.build_install_report(
        plan_document=plan,
        execution_report=journal(plan, statuses),
        now=horloge(),
    )

    assert rapport.verdict() == ir.VERDICT_FAILED
    assert rapport.exit_code() == ir.EXIT_FAILED
    statuts = {c.code: c.status for c in rapport.conditions()}
    assert statuts["runtime_installed"] == ir.CONDITION_UNSATISFIED
    assert statuts["model_downloaded"] == ir.CONDITION_UNSATISFIED


def test_une_etape_non_tentee_dit_que_l_etat_est_inconnu():
    plan = plan_document()
    statuses = [ex.STEP_DONE] * len(ACTIONS_M2)
    statuses[-1] = ex.STEP_NOT_ATTEMPTED
    rapport = ir.build_install_report(
        plan_document=plan,
        execution_report=journal(plan, statuses),
        now=horloge(),
    )
    e2e = next(c for c in rapport.conditions() if c.code == "e2e_call")

    assert e2e.status == ir.CONDITION_UNSATISFIED
    assert "inconnu" in e2e.proof


def test_deja_satisfait_vaut_satisfait_mais_ne_compte_pas_comme_applique():
    plan = plan_document()
    rapport = ir.build_install_report(
        plan_document=plan,
        execution_report=journal(plan, [ex.STEP_ALREADY_SATISFIED] * len(ACTIONS_M2)),
        now=horloge(),
    )

    assert rapport.verdict() == ir.VERDICT_COMPLETE
    assert rapport.counts()["steps_applied"] == 0
    assert rapport.counts()["steps_total"] == 7


def test_la_partition_des_actions_est_exhaustive_et_disjointe():
    """Une action nouvelle ne peut pas disparaître de l'évaluation en silence."""
    mappees: list[str] = []
    for condition in ir.M2_CONDITIONS:
        mappees.extend(condition.actions)

    assert len(mappees) == len(set(mappees)), "une action prouve deux conditions"
    assert set(mappees) & ir.CONDITION_UNMAPPED_ACTIONS == set()
    assert set(mappees) | ir.CONDITION_UNMAPPED_ACTIONS == set(sc.PLAN_ACTIONS)


def test_les_actions_non_mappees_ne_fabriquent_aucune_condition():
    plan = plan_document(actions=(sc.ACTION_WRITE_REGISTRY, sc.ACTION_ENABLE_MODEL))
    rapport = ir.build_install_report(
        plan_document=plan,
        execution_report=journal(plan, [ex.STEP_DONE, ex.STEP_DONE]),
        now=horloge(),
    )
    statuts = {c.code: c.status for c in rapport.conditions()}

    assert statuts["report_produced"] == ir.CONDITION_SATISFIED  # contrôle positif
    assert rapport.counts()["conditions_unproven"] == 6
    assert rapport.verdict() == ir.VERDICT_PARTIAL


def test_un_statut_hors_contrat_ne_devient_jamais_un_succes():
    resultats = [{"order": 1, "action": sc.ACTION_SMOKE_TEST, "status": "presque"}]
    outcomes = {c.code: c for c in ir._evaluate_conditions(resultats, applied=True)}

    assert outcomes["e2e_call"].status == ir.CONDITION_UNSATISFIED
    assert "hors contrat" in outcomes["e2e_call"].proof
    # Contrôle positif : le même helper sait bien rendre « satisfait ».
    resultats[0]["status"] = ex.STEP_DONE
    outcomes = {c.code: c for c in ir._evaluate_conditions(resultats, applied=True)}
    assert outcomes["e2e_call"].status == ir.CONDITION_SATISFIED


# ── 2. Constat contre hypothèse ───────────────────────────────────────────────

def test_un_marqueur_de_preuve_reconnu_n_est_pas_une_hypothese():
    plan = plan_document(sections=(section_runtime(evidence="constat-§6"),))
    rapport = ir.build_install_report(
        plan_document=plan,
        execution_report=journal(plan, [ex.STEP_DONE] * len(ACTIONS_M2)),
        now=horloge(),
    )

    # Contrôle positif : l'affirmation est bien vue, elle n'est simplement pas
    # classée comme hypothèse.
    assert len(rapport.evidence_claims()) == 1
    assert rapport.hypotheses() == ()


def test_une_hypothese_declaree_reste_visible():
    plan = plan_document(sections=(
        section_runtime(evidence="hypothèse-à-confirmer", note="Archive supposée publiée."),
    ))
    rapport = ir.build_install_report(
        plan_document=plan,
        execution_report=journal(plan, [ex.STEP_DONE] * len(ACTIONS_M2)),
        now=horloge(),
    )
    hypotheses = rapport.hypotheses()

    assert len(hypotheses) == 1
    assert hypotheses[0].marker == "hypothèse-à-confirmer"
    assert hypotheses[0].note == "Archive supposée publiée."
    assert "sections[0].data.variant" in hypotheses[0].path
    assert rapport.counts()["hypotheses"] == 1


def test_un_marqueur_inconnu_est_traite_comme_une_hypothese():
    """Fail-closed : le doute penche du côté qui rend l'affirmation plus visible."""
    plan = plan_document(sections=(section_runtime(evidence="marqueur-inedit-v2"),))
    rapport = ir.build_install_report(
        plan_document=plan,
        execution_report=journal(plan, [ex.STEP_DONE] * len(ACTIONS_M2)),
        now=horloge(),
    )

    assert [h.marker for h in rapport.hypotheses()] == ["marqueur-inedit-v2"]


def test_les_hypotheses_sont_rendues_dans_une_section_qui_leur_est_propre():
    plan = plan_document(sections=(
        section_runtime(evidence="hypothèse-à-confirmer", note="Archive supposée publiée."),
    ))
    rapport = ir.build_install_report(
        plan_document=plan,
        execution_report=journal(plan, [ex.STEP_DONE] * len(ACTIONS_M2)),
        now=horloge(),
    )
    texte = ir.render_install_human(rapport)

    assert "HYPOTHÈSES NON VÉRIFIÉES" in texte
    assert "Archive supposée publiée." in texte
    assert "hypothèse-à-confirmer" in texte


def test_sans_hypothese_le_rendu_le_dit_explicitement():
    texte = ir.render_install_human(rapport_complet())

    assert "HYPOTHÈSES NON VÉRIFIÉES" in texte  # contrôle positif : la section existe
    assert "(aucune) — toute affirmation du plan porte un niveau de preuve reconnu." in texte


def test_les_marqueurs_de_la_convention_de_suivi_sont_des_constats():
    assert "🔬" in ir.EVIDENCE_VERIFIED_MARKERS
    assert "📖" in ir.EVIDENCE_VERIFIED_MARKERS
    # `🧭` est un jugement à confirmer : il ne vaut pas constat.
    assert "🧭" not in ir.EVIDENCE_VERIFIED_MARKERS


# ── 3. Refus de sources incohérentes ──────────────────────────────────────────

def test_refuse_un_plan_invalide():
    plan = plan_document()
    plan["tool"] = "autre-outil"
    with pytest.raises(ir.SourcesRefused) as exc:
        ir.build_install_report(
            plan_document=plan,
            execution_report=journal(plan, [ex.STEP_DONE] * len(ACTIONS_M2)),
            now=horloge(),
        )
    assert any("plan invalide" in r for r in exc.value.reasons)


def test_refuse_un_plan_non_applicable():
    section = sc.PlanSection(
        name=sc.SECTION_RUNTIME,
        version=1,
        status="fail",
        summary="Aucune variante sûre.",
        findings=(sc.Finding(code="runtime_absent", level="fail", message="Rien à installer."),),
    )
    plan = plan_document(sections=(section,))
    assert plan["applicable"] is False  # contrôle positif : la fixture est bien bloquée

    with pytest.raises(ir.SourcesRefused) as exc:
        ir.build_install_report(
            plan_document=plan,
            execution_report=journal(plan, []),
            now=horloge(),
        )
    assert any("applicable" in r for r in exc.value.reasons)


def test_refuse_un_journal_qui_parle_d_un_autre_plan():
    plan = plan_document()
    autre = plan_document(generated_at="2026-07-30T09:00:00Z")
    with pytest.raises(ir.SourcesRefused) as exc:
        ir.build_install_report(
            plan_document=plan,
            execution_report=journal(autre, [ex.STEP_DONE] * len(ACTIONS_M2)),
            now=horloge(),
        )
    raisons = " ".join(exc.value.reasons)
    assert "ne décrivent pas la même installation" in raisons
    assert "généré le" in raisons


def test_refuse_un_journal_incoherent():
    plan = plan_document()
    brut = journal(plan, [ex.STEP_DONE] * len(ACTIONS_M2))
    casse = ex.ExecutionReport(
        started_at=brut.started_at,
        finished_at=brut.finished_at,
        mode=brut.mode,
        plan_fingerprint="pas-une-empreinte",
        plan_generated_at=brut.plan_generated_at,
        results=brut.results,
    )
    with pytest.raises(ir.SourcesRefused) as exc:
        ir.build_install_report(plan_document=plan, execution_report=casse, now=horloge())
    assert any("journal invalide" in r for r in exc.value.reasons)


def test_refuse_un_plan_qui_expose_un_secret():
    fuite = sc.PlanSection(
        name=sc.SECTION_RUNTIME,
        version=1,
        status="ok",
        summary="Runtime résolu.",
        data={"note": f"télécharger avec {FAUX_TOKEN}"},
    )
    plan = plan_document(sections=(fuite,))
    with pytest.raises(ir.SourcesRefused) as exc:
        ir.build_install_report(
            plan_document=plan,
            execution_report=journal(plan, [ex.STEP_DONE] * len(ACTIONS_M2)),
            now=horloge(),
        )
    assert any("secret exposé par le plan" in r for r in exc.value.reasons)

    # Contrôle positif : la même fixture sans le jeton est acceptée.
    propre = sc.PlanSection(
        name=sc.SECTION_RUNTIME,
        version=1,
        status="ok",
        summary="Runtime résolu.",
        data={"note": "télécharger avec le jeton fourni par l'opérateur"},
    )
    sain = plan_document(sections=(propre,))
    assert ir.build_install_report(
        plan_document=sain,
        execution_report=journal(sain, [ex.STEP_DONE] * len(ACTIONS_M2)),
        now=horloge(),
    ).verdict() == ir.VERDICT_COMPLETE


def test_refuse_un_journal_qui_expose_un_secret():
    plan = plan_document()
    fuyant = journal(
        plan,
        [ex.STEP_DONE] * len(ACTIONS_M2),
        evidences={sc.ACTION_DOWNLOAD_MODEL: {"commande": f"curl -H 'Bearer {FAUX_TOKEN}'"}},
    )
    with pytest.raises(ir.SourcesRefused) as exc:
        ir.build_install_report(plan_document=plan, execution_report=fuyant, now=horloge())
    assert any("secret exposé par le journal" in r for r in exc.value.reasons)


def test_refuse_une_racine_qui_n_est_pas_un_objet():
    plan = plan_document()
    with pytest.raises(ir.SourcesRefused):
        ir.build_install_report(
            plan_document=["pas", "un", "objet"],
            execution_report=journal(plan, [ex.STEP_DONE] * len(ACTIONS_M2)),
            now=horloge(),
        )


def test_refuse_un_journal_qui_n_est_pas_un_execution_report():
    with pytest.raises(ir.SourcesRefused) as exc:
        ir.build_install_report(
            plan_document=plan_document(),
            execution_report={"pas": "un rapport"},
            now=horloge(),
        )
    assert "ExecutionReport" in str(exc.value)


def test_toutes_les_raisons_sont_rendues_ensemble():
    plan = plan_document()
    plan["tool"] = "autre-outil"
    autre = plan_document(generated_at="2026-07-30T09:00:00Z")
    with pytest.raises(ir.SourcesRefused) as exc:
        ir.build_install_report(
            plan_document=plan,
            execution_report=journal(autre, [ex.STEP_DONE] * len(ACTIONS_M2)),
            now=horloge(),
        )
    assert len(exc.value.reasons) >= 2


# ── 4. Horloge injectable ─────────────────────────────────────────────────────

def test_l_horodatage_vient_de_l_horloge_injectee():
    plan = plan_document()
    rapport = ir.build_install_report(
        plan_document=plan,
        execution_report=journal(plan, [ex.STEP_DONE] * len(ACTIONS_M2)),
        now=horloge("1999-12-31T23:59:59Z"),
    )
    assert rapport.generated_at == "1999-12-31T23:59:59Z"


def test_l_horloge_n_a_pas_de_valeur_par_defaut():
    plan = plan_document()
    with pytest.raises(TypeError):
        ir.build_install_report(
            plan_document=plan,
            execution_report=journal(plan, [ex.STEP_DONE] * len(ACTIONS_M2)),
        )


def test_refuse_une_horloge_qui_ne_rend_pas_de_date():
    plan = plan_document()
    with pytest.raises(ir.SourcesRefused) as exc:
        ir.build_install_report(
            plan_document=plan,
            execution_report=journal(plan, [ex.STEP_DONE] * len(ACTIONS_M2)),
            now=lambda: "",
        )
    assert "horloge" in str(exc.value)


# ── 5. Empreintes et vérifiabilité ────────────────────────────────────────────

def test_les_empreintes_viennent_du_contrat_d_execution():
    plan = plan_document()
    rapport = ir.build_install_report(
        plan_document=plan,
        execution_report=journal(plan, [ex.STEP_DONE] * len(ACTIONS_M2)),
        now=horloge(),
    )

    assert rapport.plan_fingerprint == ex.plan_fingerprint(plan)
    assert rapport.plan_fingerprint.startswith("sha256:")
    assert rapport.execution_fingerprint == ex.plan_fingerprint(
        rapport.execution.to_dict()
    )
    assert rapport.execution_fingerprint != rapport.plan_fingerprint


def test_le_rapport_embarque_ses_deux_sources_verbatim():
    plan = plan_document()
    rapport = ir.build_install_report(
        plan_document=plan,
        execution_report=journal(plan, [ex.STEP_DONE] * len(ACTIONS_M2)),
        now=horloge(),
    )
    document = rapport.to_dict()

    assert document["plan"] == plan
    assert document["execution"] == rapport.execution.to_dict()
    # Auto-suffisance : l'empreinte est recalculable depuis le document seul.
    assert ex.plan_fingerprint(document["plan"]) == document["plan_fingerprint"]


# ── 6. Le récapitulatif est recalculé, pas cru sur parole ─────────────────────

def test_un_document_intact_est_valide():
    document = json.loads(ir.render_install_json(rapport_complet()))
    assert ir.validate_install_document(document) == ()


def test_rejette_des_conditions_falsifiees():
    plan = plan_document(actions=(sc.ACTION_INSTALL_RUNTIME,))
    rapport = ir.build_install_report(
        plan_document=plan,
        execution_report=journal(plan, [ex.STEP_DONE]),
        now=horloge(),
    )
    document = rapport.to_dict()
    assert ir.validate_install_document(document) == ()  # contrôle positif

    for condition in document["conditions"]:
        condition["status"] = ir.CONDITION_SATISFIED
    erreurs = ir.validate_install_document(document)
    assert any("conditions[" in e for e in erreurs)


def test_rejette_un_verdict_falsifie():
    plan = plan_document(actions=(sc.ACTION_INSTALL_RUNTIME,))
    rapport = ir.build_install_report(
        plan_document=plan,
        execution_report=journal(plan, [ex.STEP_DONE]),
        now=horloge(),
    )
    document = rapport.to_dict()
    document["verdict"] = ir.VERDICT_COMPLETE
    document["exit_code"] = ir.EXIT_OK

    erreurs = ir.validate_install_document(document)
    assert any("verdict annonce" in e for e in erreurs)
    assert any("exit_code annonce" in e for e in erreurs)


def test_rejette_des_compteurs_entierement_booleens():
    """`True == 1` : une égalité de dictionnaires ne suffit pas en Python."""
    document = rapport_complet().to_dict()
    document["counts"] = {cle: True for cle in document["counts"]}

    erreurs = ir.validate_install_document(document)
    assert any("doit être un entier" in e for e in erreurs)


def test_rejette_un_compteur_manquant_ou_inconnu():
    document = rapport_complet().to_dict()
    document["counts"].pop("hypotheses")
    assert any("counts.hypotheses est obligatoire" in e
               for e in ir.validate_install_document(document))

    document = rapport_complet().to_dict()
    document["counts"]["inventé"] = 3
    assert any("clé inconnue" in e for e in ir.validate_install_document(document))


def test_rejette_un_compteur_falsifie():
    """Un compteur du bon TYPE mais de la mauvaise valeur doit tomber lui aussi."""
    document = rapport_complet().to_dict()
    assert ir.validate_install_document(document) == ()  # contrôle positif
    document["counts"]["steps_applied"] = 99

    erreurs = ir.validate_install_document(document)
    assert any("counts annonce" in e for e in erreurs)


def test_rejette_une_liste_d_hypotheses_videe():
    plan = plan_document(sections=(section_runtime(evidence="hypothèse-à-confirmer"),))
    rapport = ir.build_install_report(
        plan_document=plan,
        execution_report=journal(plan, [ex.STEP_DONE] * len(ACTIONS_M2)),
        now=horloge(),
    )
    document = rapport.to_dict()
    assert ir.validate_install_document(document) == ()  # contrôle positif

    document["hypotheses"] = []
    document["counts"]["hypotheses"] = 0
    erreurs = ir.validate_install_document(document)
    assert any("hypotheses en contient 0" in e for e in erreurs)


def test_rejette_une_liste_inventee_de_la_bonne_taille():
    """Comparer les longueurs ne suffit pas : c'est cette liste qu'on lit en diagonale."""
    document = rapport_complet().to_dict()
    document["licenses"] = [
        {"path": "inventé", "field": "license", "value": "proprietary"}
        for _ in document["licenses"]
    ]
    assert document["licenses"], "la fixture doit porter au moins une licence"

    erreurs = ir.validate_install_document(document)
    assert any("licenses[0]" in e for e in erreurs)


def test_rejette_un_plan_echange():
    document = rapport_complet().to_dict()
    document["plan"] = plan_document(generated_at="2026-07-01T00:00:00Z")

    erreurs = ir.validate_install_document(document)
    assert any("plan_fingerprint annonce" in e for e in erreurs)


def test_rejette_un_journal_retouche_dans_le_document():
    document = rapport_complet().to_dict()
    document["execution"]["results"][0]["summary"] = "retouché"

    erreurs = ir.validate_install_document(document)
    assert any("execution_fingerprint annonce" in e for e in erreurs)


def test_rejette_un_mode_falsifie():
    plan = plan_document()
    rapport = ir.build_install_report(
        plan_document=plan,
        execution_report=journal(
            plan, [ex.STEP_WOULD_APPLY] * len(ACTIONS_M2), mode=ex.ExecutionMode.DRY_RUN
        ),
        now=horloge(),
    )
    document = rapport.to_dict()
    document["applied"] = True

    erreurs = ir.validate_install_document(document)
    assert any("applied annonce" in e for e in erreurs)


def test_rejette_une_performance_falsifiee():
    document = rapport_complet().to_dict()
    document["performance"]["total_duration_ms"] = 0

    erreurs = ir.validate_install_document(document)
    assert any("total_duration_ms" in e for e in erreurs)


def test_rejette_un_outil_ou_une_version_inattendus():
    document = rapport_complet().to_dict()
    document["tool"] = "autre"
    assert any("tool doit valoir" in e for e in ir.validate_install_document(document))

    document = rapport_complet().to_dict()
    document["schema_version"] = ir.REPORT_SCHEMA_VERSION + 1
    assert any("schema_version" in e for e in ir.validate_install_document(document))


def test_rejette_une_cle_racine_hors_contrat():
    """Ajouter un champ que rien ne recoupe est la falsification la plus simple."""
    document = rapport_complet().to_dict()
    assert ir.validate_install_document(document) == ()  # contrôle positif
    document["conclusion"] = "installation validée par l'exploitation"

    erreurs = ir.validate_install_document(document)
    assert any("clés hors contrat" in e for e in erreurs)


def test_rejette_une_cle_obligatoire_absente():
    document = rapport_complet().to_dict()
    document.pop("execution_fingerprint")

    erreurs = ir.validate_install_document(document)
    assert any("clés obligatoires absentes" in e for e in erreurs)


def test_controle_les_versions_des_deux_contrats_amont():
    document = rapport_complet().to_dict()
    document["plan_schema_version"] = sc.PLAN_SCHEMA_VERSION + 1
    assert any("plan_schema_version" in e for e in ir.validate_install_document(document))

    document = rapport_complet().to_dict()
    document["execution_schema_version"] = ex.EXECUTION_SCHEMA_VERSION + 1
    assert any("execution_schema_version" in e for e in ir.validate_install_document(document))


def test_rejette_une_racine_qui_n_est_pas_un_objet():
    assert ir.validate_install_document(["liste"])[0].startswith("le rapport doit être")


def test_assert_valid_leve_sur_document_incoherent():
    document = rapport_complet().to_dict()
    document["verdict"] = "inventé"
    with pytest.raises(ir.InstallReportError):
        ir.assert_valid_install_document(document)
    # Contrôle positif : le document intact ne lève pas.
    ir.assert_valid_install_document(rapport_complet().to_dict())


# ── 7. Non-divulgation au rendu ───────────────────────────────────────────────

def rapport_qui_fuit() -> ir.InstallReport:
    """
    Un rapport PARFAITEMENT cohérent, qui ne pèche que par un secret.

    Construit sans passer par `build_install_report()`, qui refuserait — et
    surtout construit de sorte que l'empreinte, les compteurs et le verdict
    soient justes. Une fixture incohérente ferait lever la validation, pas le
    détecteur de fuite, et le test resterait vert le jour où la non-divulgation
    disparaîtrait du rendu.
    """
    plan = plan_document()
    return ir.InstallReport(
        generated_at="2026-08-01T12:00:00Z",
        plan=plan,
        execution=journal(
            plan,
            [ex.STEP_DONE] * len(ACTIONS_M2),
            evidences={sc.ACTION_DOWNLOAD_MODEL: {"commande": f"curl -H 'Bearer {FAUX_TOKEN}'"}},
        ),
    )


def test_le_rendu_json_refuse_un_document_qui_fuit():
    fuyant = rapport_qui_fuit()
    # Contrôle négatif de la fixture : hors le secret, le document est cohérent.
    assert ir.validate_install_document(fuyant.to_dict()) == ()

    with pytest.raises(sc.PlanError) as exc:
        ir.render_install_json(fuyant)
    assert "valeurs sensibles" in str(exc.value)

    # Contrôle positif : sans le jeton, le même rapport se rend.
    assert "eva-bootstrap-install-report" in ir.render_install_json(rapport_complet())


def test_le_rendu_humain_refuse_un_document_qui_fuit():
    with pytest.raises(sc.PlanError) as exc:
        ir.render_install_human(rapport_qui_fuit())
    assert "valeurs sensibles" in str(exc.value)

    # Contrôle positif : le rendu humain fonctionne sur un rapport sain.
    assert "RAPPORT D'INSTALLATION" in ir.render_install_human(rapport_complet())


def test_le_rendu_refuse_un_document_incoherent():
    plan = plan_document()
    incoherent = ir.InstallReport(
        generated_at="2026-08-01T12:00:00Z",
        plan={**plan, "generated_at": "2020-01-01T00:00:00Z"},
        execution=journal(plan, [ex.STEP_DONE] * len(ACTIONS_M2)),
    )
    with pytest.raises(ir.InstallReportError):
        ir.render_install_json(incoherent)


def test_aucun_secret_ne_traverse_le_rendu_humain():
    texte = ir.render_install_human(rapport_complet())

    assert FAUX_TOKEN not in texte
    # Contrôle positif : la recherche saurait voir le motif si le texte le portait.
    assert FAUX_TOKEN in (texte + FAUX_TOKEN)
    assert sc.find_secret_leaks(texte) == ()
    assert sc.find_secret_leaks(texte + " " + FAUX_TOKEN) != ()


# ── 8. Rendus ─────────────────────────────────────────────────────────────────

def test_le_rendu_humain_annonce_le_verdict_en_tete_et_en_pied():
    plan = plan_document(actions=(sc.ACTION_INSTALL_RUNTIME,))
    rapport = ir.build_install_report(
        plan_document=plan,
        execution_report=journal(plan, [ex.STEP_DONE]),
        now=horloge(),
    )
    lignes = ir.render_install_human(rapport).splitlines()

    assert "INSTALLATION PARTIELLE" in "\n".join(lignes[:6])
    assert "INSTALLATION PARTIELLE" in lignes[-2]
    assert lignes[-1] == f"Sortie : {ir.EXIT_PARTIAL}"


def test_le_rendu_humain_liste_ce_qui_reste_a_faire():
    plan = plan_document(actions=(sc.ACTION_INSTALL_RUNTIME,))
    rapport = ir.build_install_report(
        plan_document=plan,
        execution_report=journal(plan, [ex.STEP_DONE]),
        now=horloge(),
    )
    texte = ir.render_install_human(rapport)

    assert "CE QUI RESTE À FAIRE" in texte
    assert "Appel E2E réussi" in texte


def test_une_installation_complete_ne_liste_rien_a_faire():
    texte = ir.render_install_human(rapport_complet())

    # Contrôle positif : la section des conditions est bien présente.
    assert "CONDITIONS DE SORTIE DU JALON M2 (7/7)" in texte
    assert "CE QUI RESTE À FAIRE" not in texte


def test_le_rendu_humain_porte_les_decisions_et_leur_justification():
    texte = ir.render_install_human(rapport_complet())

    assert "DÉCISIONS ET LEUR JUSTIFICATION" in texte
    assert "parce que aucune archive officielle CUDA" in texte
    assert "écarté : official-release/cpu : backend inférieur" in texte


def test_le_rendu_humain_porte_licences_versions_et_empreintes():
    texte = ir.render_install_human(rapport_complet())

    assert "LICENCES" in texte and "apache-2.0" in texte
    assert "VERSIONS" in texte
    assert "EMPREINTES ET RÉVISIONS" in texte
    assert "a" * 64 in texte


def test_le_rendu_humain_ne_porte_aucun_balisage_rich():
    texte = ir.render_install_human(rapport_complet())

    assert "[/" not in texte
    assert "\x1b[" not in texte
    # Contrôle positif : le rendu utilise bien des marqueurs entre crochets.
    assert "[ok]" in texte


def test_le_rendu_json_est_relisible_et_valide():
    document = json.loads(ir.render_install_json(rapport_complet()))

    assert document["tool"] == ir.REPORT_TOOL_NAME
    assert document["schema_version"] == ir.REPORT_SCHEMA_VERSION
    assert document["plan_schema_version"] == sc.PLAN_SCHEMA_VERSION
    assert document["execution_schema_version"] == ex.EXECUTION_SCHEMA_VERSION
    assert ir.validate_install_document(document) == ()


# ── 9. Index et performances ──────────────────────────────────────────────────

def test_l_index_des_licences_releve_les_champs_par_leur_nom():
    rapport = rapport_complet()
    valeurs = {ref.value for ref in rapport.licenses()}

    assert "apache-2.0" in valeurs
    # La correspondance est héritée du champ `license`, qui porte un objet :
    # aucun des champs terminaux ne contient le mot lui-même.
    assert all("license" in ref.path for ref in rapport.licenses())
    assert {ref.field for ref in rapport.licenses()} == {"base_model", "fine_tune"}


def test_l_index_des_empreintes_releve_sha_et_revisions():
    rapport = rapport_complet()
    champs = {ref.field for ref in rapport.fingerprints()}

    assert "artifact_sha256" in champs
    assert "revision" in champs
    assert "sha256" in champs
    assert "commit" in champs


def test_un_plan_sans_licence_donne_un_index_vide_annonce_comme_tel():
    plan = plan_document(sections=())
    rapport = ir.build_install_report(
        plan_document=plan,
        execution_report=journal(plan, [ex.STEP_DONE] * len(ACTIONS_M2)),
        now=horloge(),
    )

    assert rapport.licenses() == ()
    assert "(aucune) — le plan ne porte aucun champ de ce type." in ir.render_install_human(rapport)
    # Contrôle positif : avec une section, l'index n'est pas vide.
    assert rapport_complet().licenses() != ()


def test_les_mesures_de_performance_excluent_les_booleens():
    plan = plan_document()
    rapport = ir.build_install_report(
        plan_document=plan,
        execution_report=journal(
            plan,
            [ex.STEP_DONE] * len(ACTIONS_M2),
            evidences={sc.ACTION_SMOKE_TEST: {"ttft_ms": 412, "stream_ok": True}},
        ),
        now=horloge(),
    )
    smoke = next(p for p in rapport.performance() if p.action == sc.ACTION_SMOKE_TEST)

    assert smoke.metrics == {"ttft_ms": 412}
    assert "stream_ok" not in smoke.metrics


def test_la_duree_totale_somme_les_etapes():
    rapport = rapport_complet()
    attendu = sum(10 * (i + 1) for i in range(len(ACTIONS_M2)))

    assert rapport.total_duration_ms() == attendu
    assert f"Durée totale des étapes : {attendu} ms" in ir.render_install_human(rapport)


def test_les_echecs_sont_rendus_avec_leur_message():
    plan = plan_document()
    statuses = [ex.STEP_FAILED] + [ex.STEP_NOT_ATTEMPTED] * 6
    rapport = ir.build_install_report(
        plan_document=plan,
        execution_report=journal(plan, statuses),
        now=horloge(),
    )
    texte = ir.render_install_human(rapport)

    assert "ÉCHECS DE L'EXÉCUTION" in texte
    assert "boum" in texte
    assert rapport.counts()["failures"] == 1


# ── 10. Persistance ───────────────────────────────────────────────────────────

def test_ecrit_le_rapport_json_a_l_emplacement_fourni(tmp_path):
    cible = tmp_path / "rapport.json"
    ecrit = ir.write_install_report(rapport_complet(), cible)

    assert ecrit == cible
    document = json.loads(cible.read_text(encoding="utf-8"))
    assert ir.validate_install_document(document) == ()


def test_ecrit_le_rapport_humain(tmp_path):
    cible = tmp_path / "rapport.txt"
    ir.write_install_report(rapport_complet(), cible, fmt=ir.FORMAT_TEXT)

    assert "RAPPORT D'INSTALLATION EVARUNTIME" in cible.read_text(encoding="utf-8")


def test_n_invente_jamais_l_emplacement(tmp_path):
    cible = tmp_path / "absent" / "rapport.json"
    with pytest.raises(ir.InstallReportError) as exc:
        ir.write_install_report(rapport_complet(), cible)

    assert "n'est pas un répertoire existant" in str(exc.value)
    assert not cible.exists()
    assert not (tmp_path / "absent").exists()


def test_refuse_un_format_inconnu(tmp_path):
    with pytest.raises(ir.InstallReportError):
        ir.write_install_report(rapport_complet(), tmp_path / "r.yaml", fmt="yaml")
    assert list(tmp_path.iterdir()) == []


def test_ne_laisse_aucun_fichier_temporaire(tmp_path):
    ir.write_install_report(rapport_complet(), tmp_path / "rapport.json")

    assert [p.name for p in tmp_path.iterdir()] == ["rapport.json"]


def test_l_ecriture_remplace_atomiquement_l_ancien_rapport(tmp_path):
    cible = tmp_path / "rapport.json"
    cible.write_text("ancien contenu", encoding="utf-8")
    ir.write_install_report(rapport_complet(), cible)

    assert "ancien contenu" not in cible.read_text(encoding="utf-8")
    assert [p.name for p in tmp_path.iterdir()] == ["rapport.json"]


def test_l_ecriture_est_atomique_meme_si_le_remplacement_echoue(tmp_path, monkeypatch):
    """Un rapport à moitié écrit ne remplace jamais un rapport antérieur complet."""
    cible = tmp_path / "rapport.json"
    cible.write_text("ancien contenu", encoding="utf-8")

    def refuse(source, destination):
        raise OSError("disque plein")

    monkeypatch.setattr(ir.os, "replace", refuse)
    with pytest.raises(OSError):
        ir.write_install_report(rapport_complet(), cible)

    assert cible.read_text(encoding="utf-8") == "ancien contenu"
    assert [p.name for p in tmp_path.iterdir()] == ["rapport.json"]


def test_un_rapport_qui_fuit_n_est_jamais_ecrit(tmp_path):
    cible = tmp_path / "rapport.json"
    with pytest.raises(sc.PlanError) as exc:
        ir.write_install_report(rapport_qui_fuit(), cible)
    assert "valeurs sensibles" in str(exc.value)

    assert not cible.exists()
    assert list(tmp_path.iterdir()) == []
    # Contrôle positif : un rapport sain s'écrit bien au même endroit.
    ir.write_install_report(rapport_complet(), cible)
    assert cible.exists()


# ── 11. Frontières du module ──────────────────────────────────────────────────

def test_le_module_n_importe_que_les_deux_contrats():
    """
    La règle qui a rendu la parallélisation possible : aucun producteur importé.

    Vérifié sur l'AST du module, pas par `grep` : un commentaire mentionnant
    `catalog` ne doit pas faire échouer le test, et un import caché derrière une
    mise en forme inhabituelle ne doit pas lui échapper.
    """
    arbre = ast.parse(Path(ir.__file__).read_text(encoding="utf-8"))
    importes: set[str] = set()
    for noeud in ast.walk(arbre):
        if isinstance(noeud, ast.Import):
            importes.update(alias.name.split(".")[0] for alias in noeud.names)
        elif isinstance(noeud, ast.ImportFrom):
            if noeud.level:
                importes.update(alias.name for alias in noeud.names)
            else:
                importes.add((noeud.module or "").split(".")[0])

    # Contrôle positif : l'introspection voit bien des imports.
    assert {"schema", "execution"} <= importes
    assert importes & {
        "inventory", "runtime_resolver", "catalog", "llmfit", "gguf_meta", "planner",
        "doctor", "config", "database", "main",
    } == set()


def test_les_codes_de_sortie_sont_ceux_de_la_famille():
    assert ir.EXIT_OK == sc.EXIT_OK == 0
    assert ir.EXIT_FAILED == sc.EXIT_BLOCKED == 1
    assert ir.EXIT_PARTIAL == sc.EXIT_WARNINGS == 3
    assert set(ir.INSTALL_VERDICTS) == {
        ir.VERDICT_COMPLETE, ir.VERDICT_PARTIAL, ir.VERDICT_FAILED,
    }
    assert set(ir.CONDITION_STATUSES) == {
        ir.CONDITION_SATISFIED, ir.CONDITION_UNSATISFIED, ir.CONDITION_UNPROVEN,
    }
    assert {c.status for c in rapport_complet().conditions()} <= ir.CONDITION_STATUSES
