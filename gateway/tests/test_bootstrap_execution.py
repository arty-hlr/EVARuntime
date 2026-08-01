"""
AUT-006 → AUT-011 — régressions du contrat d'exécution (`bootstrap/execution.py`).

Ce contrat est le seul endroit du parcours M2 où une erreur modifie une machine.
Ces tests verrouillent donc quatre familles d'invariants :

1. **la porte d'entrée** — un plan relu n'est exécuté que s'il est à la bonne
   version, valide, sans secret, applicable et cohérent avec lui-même. Les
   contrôles propres à l'applicateur sont testés `schema.validate_plan_dict()`
   NEUTRALISÉ, sans quoi ils passeraient au vert grâce au travail d'un autre
   module et pourraient disparaître sans qu'un test tombe ;
2. **la distinction des statuts** — « sauté » et « non tenté » ne disent pas la
   même chose à l'opérateur, « fait » et « déjà satisfait » non plus ;
3. **les champs dérivés** — verdict, compteurs et liste d'échecs sont recalculés
   et confrontés. C'est la leçon de la vague 5, appliquée d'emblée ;
4. **la non-divulgation** — un rapport qui fuit n'est pas publié, et le journal
   expurge avant d'écrire.

Chaque test d'ABSENCE porte son contrôle positif : un test qui affirme « aucun
secret » ou « aucune étape sautée » sans prouver qu'il saurait en voir un
passerait au vert le jour où le détecteur deviendrait inerte.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from bootstrap import execution as ex
from bootstrap import schema as sc

# Construit à l'exécution : un littéral ressemblant à un vrai jeton n'a rien à
# faire dans un dépôt, même en fixture.
FAUX_TOKEN = "hf_" + "A" * 24


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


_ACTIONS: tuple[str, ...] = (
    sc.ACTION_DOWNLOAD_MODEL,
    sc.ACTION_VERIFY_ARTIFACT,
    sc.ACTION_WRITE_REGISTRY,
)


def _steps(count: int = 3) -> tuple[sc.PlanStep, ...]:
    return tuple(
        sc.PlanStep(
            order=index + 1,
            action=_ACTIONS[index % len(_ACTIONS)],
            target=f"cible-{index + 1}",
            detail=f"détail de l'étape {index + 1}",
            requires_root=False,
            reversible=True,
        )
        for index in range(count)
    )


def _plan(**kwargs) -> sc.BootstrapPlan:
    base = {
        "generated_at": "2026-08-01T09:00:00Z",
        "mode": "local",
        "sections": (_section(),),
        "steps": _steps(),
        "decisions": (
            sc.Decision(
                topic="runtime",
                choice="local-build",
                rationale="aucun artefact officiel ne couvre cette plateforme",
            ),
        ),
    }
    base.update(kwargs)
    return sc.BootstrapPlan(**base)  # type: ignore[arg-type]


def _document(**kwargs) -> dict:
    return _plan(**kwargs).to_dict()


def _text(document: dict | None = None, **kwargs) -> str:
    return json.dumps(document if document is not None else _document(**kwargs))


class _Horloge:
    """Horloge injectable : aucun test ne doit dépendre de l'heure réelle."""

    def __init__(self, pas: float = 0.25) -> None:
        self.pas = pas
        self.secondes = 0.0
        self.appels_now = 0

    def monotonic(self) -> float:
        self.secondes += self.pas
        return self.secondes

    def now(self) -> str:
        self.appels_now += 1
        return f"2026-08-01T10:00:{self.appels_now:02d}Z"


def _context(mode: ex.ExecutionMode = ex.ExecutionMode.APPLY, **kwargs) -> ex.ExecutionContext:
    horloge = _Horloge()
    options = {"monotonic": horloge.monotonic, "now": horloge.now}
    options.update(kwargs)
    return ex.ExecutionContext(mode, **options)  # type: ignore[arg-type]


def _executeur(status: str, summary: str = "action traitée", **kwargs):
    async def run(step: sc.PlanStep, context: ex.ExecutionContext) -> ex.StepResult:
        return ex.StepResult.for_step(step, status=status, summary=summary, **kwargs)  # type: ignore[arg-type]
    return run


def _registre(*, defaut: str = ex.STEP_DONE, par_action: dict | None = None) -> ex.ExecutorRegistry:
    registre = ex.ExecutorRegistry()
    for action in _ACTIONS:
        executeur = (par_action or {}).get(action)
        registre.register(action, executeur or _executeur(defaut))
    return registre


def _executer(plan: ex.LoadedPlan, registre, context) -> ex.ExecutionReport:
    return asyncio.run(ex.execute_plan(plan, registre, context))


def _resultat(order: int, status: str, action: str = sc.ACTION_DOWNLOAD_MODEL, **kwargs) -> ex.StepResult:
    champs = {
        "order": order,
        "action": action,
        "target": f"cible-{order}",
        "status": status,
        "summary": f"étape {order} en {status}",
    }
    champs.update(kwargs)
    return ex.StepResult(**champs)  # type: ignore[arg-type]


def _rapport(results: tuple[ex.StepResult, ...], mode=ex.ExecutionMode.APPLY) -> ex.ExecutionReport:
    return ex.ExecutionReport(
        started_at="2026-08-01T10:00:00Z",
        finished_at="2026-08-01T10:05:00Z",
        mode=mode,
        plan_fingerprint="sha256:" + "0" * 64,
        plan_generated_at="2026-08-01T09:00:00Z",
        results=results,
    )


# ══ 1. Relecture d'un plan ════════════════════════════════════════════════════

def test_un_plan_valide_est_accepte_et_rend_ses_etapes():
    plan = ex.load_plan_document(_text(), origin="plan.json")

    assert plan.origin == "plan.json"
    assert plan.generated_at == "2026-08-01T09:00:00Z"
    assert plan.mode == "local"
    assert [s.order for s in plan.steps] == [1, 2, 3]
    assert [s.action for s in plan.steps] == list(_ACTIONS)
    assert plan.fingerprint.startswith("sha256:")


def test_un_json_illisible_est_refuse():
    with pytest.raises(ex.PlanRefused) as exc:
        ex.load_plan_document("{ceci n'est pas du JSON")
    assert any("JSON illisible" in raison for raison in exc.value.reasons)


def test_une_racine_qui_n_est_pas_un_objet_est_refusee():
    with pytest.raises(ex.PlanRefused) as exc:
        ex.load_plan_document("[1, 2, 3]")
    assert any("objet JSON" in raison for raison in exc.value.reasons)


def test_une_version_de_schema_absente_est_refusee():
    document = _document()
    del document["schema_version"]
    with pytest.raises(ex.PlanRefused) as exc:
        ex.load_plan_document(_text(document))
    assert any("schema_version" in raison for raison in exc.value.reasons)


def test_une_autre_version_de_schema_est_refusee_et_non_appliquee_au_mieux():
    """
    Égalité stricte, dans les DEUX sens.

    Une version plus ancienne est aussi dangereuse qu'une plus récente : ses
    champs portent peut-être les mêmes noms avec d'autres significations, et
    `schema.validate_plan_dict()` ne refuse, lui, que ce qui est plus récent
    que lui. Sans le cas « plus ancien », relâcher l'égalité en « > » ne faisait
    tomber aucun test — vérifié par mutation.
    """
    for version in (sc.PLAN_SCHEMA_VERSION + 1, sc.PLAN_SCHEMA_VERSION - 1, 0):
        document = _document()
        document["schema_version"] = version
        with pytest.raises(ex.PlanRefused) as exc:
            ex.load_plan_document(_text(document))
        assert any("version de schéma" in raison for raison in exc.value.reasons)

    # Contrôle positif : la version courante, elle, passe.
    document["schema_version"] = sc.PLAN_SCHEMA_VERSION
    assert ex.load_plan_document(_text(document)).steps


def test_une_version_de_schema_booleenne_est_refusee():
    document = _document()
    document["schema_version"] = True
    with pytest.raises(ex.PlanRefused) as exc:
        ex.load_plan_document(_text(document))
    assert any("entier" in raison for raison in exc.value.reasons)


def test_une_erreur_de_structure_refuse_le_plan():
    document = _document()
    document["tool"] = "autre-outil"
    with pytest.raises(ex.PlanRefused) as exc:
        ex.load_plan_document(_text(document))
    assert any("structure invalide" in raison for raison in exc.value.reasons)


def test_un_secret_dans_le_plan_refuse_l_execution():
    document = _document(sections=(_section(data={"note": f"export HF={FAUX_TOKEN}"}),))
    with pytest.raises(ex.PlanRefused) as exc:
        ex.load_plan_document(_text(document))
    assert any("secret exposé" in raison for raison in exc.value.reasons)

    # Contrôle positif : le même plan sans le jeton est accepté — la détection
    # ne refuse pas tout par principe.
    assert ex.load_plan_document(_text(_document(sections=(_section(data={"note": "export HF=***"}),)))).steps


def test_un_plan_bloque_n_est_jamais_execute_partiellement():
    document = _document(sections=(
        _section(status="fail", findings=(sc.Finding("gpu_absent", "fail", "aucun GPU"),)),
    ))
    assert document["status"] == "blocked"

    with pytest.raises(ex.PlanRefused) as exc:
        ex.load_plan_document(_text(document))
    assert any("pas applicable" in raison for raison in exc.value.reasons)


def test_un_plan_declare_non_applicable_est_refuse(monkeypatch):
    monkeypatch.setattr(sc, "validate_plan_dict", lambda document: ())
    document = _document()
    document["applicable"] = False
    with pytest.raises(ex.PlanRefused) as exc:
        ex.load_plan_document(_text(document))
    assert any("applicable vaut" in raison for raison in exc.value.reasons)


def test_un_plan_portant_des_bloqueurs_est_refuse(monkeypatch):
    monkeypatch.setattr(sc, "validate_plan_dict", lambda document: ())
    document = _document()
    document["blockers"] = [{"code": "runtime_absent", "level": "fail", "message": "rien"}]
    with pytest.raises(ex.PlanRefused) as exc:
        ex.load_plan_document(_text(document))
    assert any("bloqueur" in raison for raison in exc.value.reasons)


def test_un_plan_falsifie_a_la_main_est_refuse_meme_sans_le_validateur_de_schema(monkeypatch):
    """
    Le cœur du contrat : l'applicateur recoupe LUI-MÊME, il ne délègue pas.

    `schema.validate_plan_dict()` est neutralisé pour prouver que le refus vient
    du recoupement propre à l'exécution. Sans cela, ce test resterait vert même
    si l'applicateur ne vérifiait plus rien de son côté.
    """
    monkeypatch.setattr(sc, "validate_plan_dict", lambda document: ())

    document = _document(sections=(
        _section(status="fail", findings=(sc.Finding("gpu_absent", "fail", "aucun GPU"),)),
    ))
    document["status"] = "ok"
    document["applicable"] = True
    document["blockers"] = []
    document["exit_code"] = 0
    document["steps"] = [s.to_dict() for s in _steps()]

    with pytest.raises(ex.PlanRefused) as exc:
        ex.load_plan_document(_text(document))
    assert any("recalculé" in raison for raison in exc.value.reasons)

    # Contrôle positif : le même document dont la section n'est plus en échec
    # passe — le recoupement voit la différence, il ne refuse pas tout.
    document["sections"] = [_section().to_dict()]
    assert ex.load_plan_document(_text(document)).steps


def test_une_section_fail_sans_constat_fail_est_quand_meme_un_bloqueur(monkeypatch):
    monkeypatch.setattr(sc, "validate_plan_dict", lambda document: ())
    document = _document()
    document["sections"] = [_section(status="fail").to_dict()]
    with pytest.raises(ex.PlanRefused) as exc:
        ex.load_plan_document(_text(document))
    assert any("hardware_failed" in raison for raison in exc.value.reasons)


def test_une_action_hors_contrat_refuse_le_plan(monkeypatch):
    monkeypatch.setattr(sc, "validate_plan_dict", lambda document: ())
    document = _document()
    document["steps"][1]["action"] = "rm_minus_rf"
    with pytest.raises(ex.PlanRefused) as exc:
        ex.load_plan_document(_text(document))
    assert any("action inconnue" in raison for raison in exc.value.reasons)


def test_un_trou_dans_la_numerotation_refuse_le_plan(monkeypatch):
    monkeypatch.setattr(sc, "validate_plan_dict", lambda document: ())
    document = _document()
    document["steps"][1]["order"] = 3
    document["steps"][2]["order"] = 4
    with pytest.raises(ex.PlanRefused) as exc:
        ex.load_plan_document(_text(document))
    assert any("continue et strictement croissante" in raison for raison in exc.value.reasons)


def test_un_doublon_d_ordre_refuse_le_plan(monkeypatch):
    monkeypatch.setattr(sc, "validate_plan_dict", lambda document: ())
    document = _document()
    document["steps"][1]["order"] = 1
    with pytest.raises(ex.PlanRefused) as exc:
        ex.load_plan_document(_text(document))
    assert any("continue et strictement croissante" in raison for raison in exc.value.reasons)


def test_une_numerotation_ne_commencant_pas_a_un_refuse_le_plan(monkeypatch):
    monkeypatch.setattr(sc, "validate_plan_dict", lambda document: ())
    document = _document()
    for offset, step in enumerate(document["steps"]):
        step["order"] = offset + 2
    with pytest.raises(ex.PlanRefused) as exc:
        ex.load_plan_document(_text(document))
    assert any("attendu 1" in raison for raison in exc.value.reasons)


def test_le_refus_porte_toutes_les_raisons_pas_seulement_la_premiere(monkeypatch):
    monkeypatch.setattr(sc, "validate_plan_dict", lambda document: ())
    document = _document()
    document["applicable"] = False
    document["steps"][1]["action"] = "rm_minus_rf"
    with pytest.raises(ex.PlanRefused) as exc:
        ex.load_plan_document(_text(document))
    assert len(exc.value.reasons) >= 2


def test_load_plan_file_lit_le_fichier(tmp_path: Path):
    chemin = tmp_path / "plan.json"
    chemin.write_text(_text(), encoding="utf-8")
    plan = ex.load_plan_file(chemin)
    assert plan.origin == str(chemin)
    assert len(plan.steps) == 3


def test_load_plan_file_refuse_un_fichier_absent(tmp_path: Path):
    with pytest.raises(ex.PlanRefused) as exc:
        ex.load_plan_file(tmp_path / "absent.json")
    assert any("lecture impossible" in raison for raison in exc.value.reasons)


# ══ 2. Empreinte du plan ══════════════════════════════════════════════════════

def test_l_empreinte_est_stable_quel_que_soit_l_ordre_des_cles():
    document = _document()
    inverse = dict(reversed(list(document.items())))
    assert ex.plan_fingerprint(document) == ex.plan_fingerprint(inverse)


def test_l_empreinte_change_avec_la_moindre_valeur():
    document = _document()
    autre = json.loads(json.dumps(document))
    autre["steps"][0]["target"] = "cible-modifiée"
    assert ex.plan_fingerprint(document) != ex.plan_fingerprint(autre)


def test_l_empreinte_a_le_format_annonce():
    empreinte = ex.plan_fingerprint(_document())
    assert empreinte.startswith("sha256:")
    assert len(empreinte) == len("sha256:") + 64


# ══ 3. Mode d'exécution ═══════════════════════════════════════════════════════

def test_un_contexte_ne_peut_pas_etre_construit_sans_mode():
    with pytest.raises(TypeError):
        ex.ExecutionContext()  # type: ignore[call-arg]


def test_le_mode_ne_se_devine_pas_depuis_une_chaine_approximative():
    assert ex.ExecutionMode.from_value("apply") is ex.ExecutionMode.APPLY
    for valeur in ("APPLY", "apply ", True, 1, None):
        with pytest.raises(ex.ExecutionError):
            ex.ExecutionMode.from_value(valeur)


def test_le_contexte_expose_son_mode():
    assert _context(ex.ExecutionMode.DRY_RUN).dry_run is True
    assert _context(ex.ExecutionMode.DRY_RUN).applying is False
    assert _context(ex.ExecutionMode.APPLY).applying is True


# ══ 4. Invariants d'un résultat d'étape ═══════════════════════════════════════

def test_un_statut_inconnu_est_refuse():
    with pytest.raises(ex.ExecutionError):
        _resultat(1, "presque_fait")


def test_une_action_hors_contrat_est_refusee_dans_un_resultat():
    with pytest.raises(ex.ExecutionError):
        _resultat(1, ex.STEP_DONE, action="rm_minus_rf")


def test_un_echec_sans_message_d_erreur_est_refuse():
    with pytest.raises(ex.ExecutionError):
        _resultat(1, ex.STEP_FAILED)
    # Contrôle positif : avec le message, la construction passe.
    assert _resultat(1, ex.STEP_FAILED, error="disque plein").failed


def test_un_succes_portant_une_erreur_est_refuse():
    with pytest.raises(ex.ExecutionError):
        _resultat(1, ex.STEP_DONE, error="pourtant réussi")


def test_une_duree_booleenne_ou_negative_est_refusee():
    with pytest.raises(ex.ExecutionError):
        _resultat(1, ex.STEP_DONE, duration_ms=True)
    with pytest.raises(ex.ExecutionError):
        _resultat(1, ex.STEP_DONE, duration_ms=-1)
    assert _resultat(1, ex.STEP_DONE, duration_ms=12).duration_ms == 12


def test_un_ordre_invalide_est_refuse():
    with pytest.raises(ex.ExecutionError):
        _resultat(0, ex.STEP_DONE)
    with pytest.raises(ex.ExecutionError):
        _resultat(True, ex.STEP_DONE)


def test_un_resume_vide_est_refuse():
    with pytest.raises(ex.ExecutionError):
        _resultat(1, ex.STEP_DONE, summary="")


def test_une_preuve_non_serialisable_est_refusee_en_nommant_l_etape():
    with pytest.raises(ex.ExecutionError) as exc:
        _resultat(2, ex.STEP_DONE, evidence={"chemin": Path("/models")})
    assert "étape 2" in str(exc.value)


def test_for_step_recopie_l_identite_de_l_etape():
    step = _steps()[1]
    resultat = ex.StepResult.for_step(step, status=ex.STEP_DONE, summary="fait")
    assert (resultat.order, resultat.action, resultat.target) == (
        step.order, step.action, step.target
    )


# ══ 5. Registre fail-closed ═══════════════════════════════════════════════════

def test_le_registre_refuse_une_action_hors_du_contrat():
    with pytest.raises(ex.ExecutionError):
        ex.ExecutorRegistry().register("rm_minus_rf", _executeur(ex.STEP_DONE))


def test_le_registre_refuse_d_ecraser_un_executeur():
    registre = ex.ExecutorRegistry()
    registre.register(sc.ACTION_DOWNLOAD_MODEL, _executeur(ex.STEP_DONE))
    with pytest.raises(ex.ExecutionError):
        registre.register(sc.ACTION_DOWNLOAD_MODEL, _executeur(ex.STEP_SKIPPED))


def test_le_registre_dit_ce_qui_lui_manque():
    registre = ex.ExecutorRegistry()
    registre.register(sc.ACTION_DOWNLOAD_MODEL, _executeur(ex.STEP_DONE))
    assert registre.registered_actions() == (sc.ACTION_DOWNLOAD_MODEL,)
    assert sc.ACTION_DOWNLOAD_MODEL in registre
    assert registre.get(sc.ACTION_WRITE_REGISTRY) is None
    assert registre.missing_actions(_steps()) == (
        sc.ACTION_VERIFY_ARTIFACT, sc.ACTION_WRITE_REGISTRY,
    )
    # Contrôle positif : un registre complet ne manque de rien.
    assert _registre().missing_actions(_steps()) == ()


# ══ 6. Lanceur ════════════════════════════════════════════════════════════════

def test_les_etapes_sont_executees_dans_l_ordre_et_journalisees():
    vus: list[int] = []

    async def run(step, context):
        vus.append(step.order)
        return ex.StepResult.for_step(step, status=ex.STEP_DONE, summary="fait")

    registre = _registre(par_action={action: run for action in _ACTIONS})
    rapport = _executer(ex.load_plan_document(_text()), registre, _context())

    assert vus == [1, 2, 3]
    assert [r.order for r in rapport.results] == [1, 2, 3]
    assert rapport.verdict() == ex.VERDICT_OK
    assert rapport.exit_code() == ex.EXIT_OK
    assert rapport.changed() is True


def test_les_horodatages_viennent_de_l_horloge_injectee():
    horloge = _Horloge()
    context = ex.ExecutionContext(
        ex.ExecutionMode.APPLY, monotonic=horloge.monotonic, now=horloge.now
    )
    rapport = _executer(ex.load_plan_document(_text()), _registre(), context)
    assert rapport.started_at == "2026-08-01T10:00:01Z"
    assert rapport.finished_at == "2026-08-01T10:00:02Z"
    assert all(r.duration_ms == 250 for r in rapport.results)


def test_une_duree_fournie_par_l_executeur_n_est_pas_ecrasee():
    registre = _registre(par_action={
        action: _executeur(ex.STEP_DONE, duration_ms=42) for action in _ACTIONS
    })
    rapport = _executer(ex.load_plan_document(_text()), registre, _context())
    assert [r.duration_ms for r in rapport.results] == [42, 42, 42]


def test_rejouer_une_application_deja_satisfaite_ne_refait_pas_le_travail():
    registre = _registre(defaut=ex.STEP_ALREADY_SATISFIED)
    rapport = _executer(ex.load_plan_document(_text()), registre, _context())

    assert rapport.counts()[ex.STEP_ALREADY_SATISFIED] == 3
    assert rapport.counts()[ex.STEP_DONE] == 0
    assert rapport.changed() is False
    assert rapport.verdict() == ex.VERDICT_OK


def test_apres_un_echec_les_etapes_suivantes_sont_non_tentees_pas_sautees():
    registre = _registre(par_action={
        sc.ACTION_VERIFY_ARTIFACT: _executeur(
            ex.STEP_FAILED, summary="empreinte incorrecte", error="sha256 divergent"
        ),
    })
    rapport = _executer(ex.load_plan_document(_text()), registre, _context())

    assert [r.status for r in rapport.results] == [
        ex.STEP_DONE, ex.STEP_FAILED, ex.STEP_NOT_ATTEMPTED,
    ]
    assert rapport.counts()[ex.STEP_SKIPPED] == 0
    assert rapport.verdict() == ex.VERDICT_FAILED
    assert rapport.exit_code() == ex.EXIT_FAILED


def test_controle_positif_une_etape_volontairement_sautee_est_bien_rapportee_sautee():
    registre = _registre(par_action={
        sc.ACTION_VERIFY_ARTIFACT: _executeur(ex.STEP_SKIPPED, summary="déjà vérifié hier"),
    })
    rapport = _executer(ex.load_plan_document(_text()), registre, _context())

    assert [r.status for r in rapport.results] == [
        ex.STEP_DONE, ex.STEP_SKIPPED, ex.STEP_DONE,
    ]
    assert rapport.verdict() == ex.VERDICT_PARTIAL
    assert rapport.exit_code() == ex.EXIT_PARTIAL


def test_une_action_sans_executeur_echoue_explicitement_et_ne_reussit_jamais():
    registre = ex.ExecutorRegistry()
    registre.register(sc.ACTION_DOWNLOAD_MODEL, _executeur(ex.STEP_DONE))
    rapport = _executer(ex.load_plan_document(_text()), registre, _context())

    assert [r.status for r in rapport.results] == [
        ex.STEP_DONE, ex.STEP_FAILED, ex.STEP_NOT_ATTEMPTED,
    ]
    echec = rapport.results[1]
    assert [f.code for f in echec.findings] == ["executeur_absent"]
    assert sc.ACTION_VERIFY_ARTIFACT in (echec.error or "")


def test_un_executeur_qui_leve_devient_un_echec_consigne():
    async def run(step, context):
        raise RuntimeError("le disque est plein")

    registre = _registre(par_action={sc.ACTION_DOWNLOAD_MODEL: run})
    rapport = _executer(ex.load_plan_document(_text()), registre, _context())

    assert rapport.results[0].status == ex.STEP_FAILED
    assert "RuntimeError" in (rapport.results[0].error or "")
    assert [r.status for r in rapport.results[1:]] == [ex.STEP_NOT_ATTEMPTED] * 2


def test_un_message_d_exception_portant_un_secret_est_expurge():
    async def run(step, context):
        raise RuntimeError(f"échec de l'appel avec {FAUX_TOKEN}")

    registre = _registre(par_action={sc.ACTION_DOWNLOAD_MODEL: run})
    rapport = _executer(ex.load_plan_document(_text()), registre, _context())

    assert FAUX_TOKEN not in (rapport.results[0].error or "")
    assert "expurgé" in (rapport.results[0].error or "")


def test_un_resultat_qui_designe_une_autre_etape_est_refuse():
    async def run(step, context):
        return ex.StepResult(
            order=step.order,
            action=step.action,
            target="une-autre-cible",
            status=ex.STEP_DONE,
            summary="fait, mais ailleurs",
        )

    registre = _registre(par_action={sc.ACTION_DOWNLOAD_MODEL: run})
    rapport = _executer(ex.load_plan_document(_text()), registre, _context())

    assert rapport.results[0].status == ex.STEP_FAILED
    assert [f.code for f in rapport.results[0].findings] == ["resultat_incoherent"]
    assert rapport.results[0].target == "cible-1"


def test_un_executeur_qui_ne_rend_pas_un_resultat_est_refuse():
    async def run(step, context):
        return {"status": "done"}

    registre = _registre(par_action={sc.ACTION_DOWNLOAD_MODEL: run})
    rapport = _executer(ex.load_plan_document(_text()), registre, _context())
    assert rapport.results[0].status == ex.STEP_FAILED
    assert "dict" in (rapport.results[0].error or "")


def test_une_mutation_pendant_une_simulation_est_refusee():
    registre = _registre(defaut=ex.STEP_DONE)
    rapport = _executer(
        ex.load_plan_document(_text()), registre, _context(ex.ExecutionMode.DRY_RUN)
    )

    assert rapport.results[0].status == ex.STEP_FAILED
    assert [f.code for f in rapport.results[0].findings] == ["mutation_en_simulation"]
    assert rapport.counts()[ex.STEP_DONE] == 0


def test_controle_positif_une_simulation_qui_simule_est_acceptee():
    registre = _registre(defaut=ex.STEP_WOULD_APPLY)
    rapport = _executer(
        ex.load_plan_document(_text()), registre, _context(ex.ExecutionMode.DRY_RUN)
    )

    assert rapport.counts()[ex.STEP_WOULD_APPLY] == 3
    assert rapport.verdict() == ex.VERDICT_PARTIAL
    assert rapport.changed() is False
    assert rapport.applied is False


def test_une_simulation_en_mode_application_est_refusee():
    registre = _registre(defaut=ex.STEP_WOULD_APPLY)
    rapport = _executer(ex.load_plan_document(_text()), registre, _context())

    assert rapport.results[0].status == ex.STEP_FAILED
    assert [f.code for f in rapport.results[0].findings] == ["simulation_en_application"]


def test_le_journal_est_expurge_avant_ecriture():
    ecrit: list[str] = []

    async def run(step, context):
        context.journaliser(f"téléchargement avec {FAUX_TOKEN}")
        context.journaliser("téléchargement démarré")
        return ex.StepResult.for_step(step, status=ex.STEP_DONE, summary="fait")

    registre = _registre(par_action={sc.ACTION_DOWNLOAD_MODEL: run})
    _executer(ex.load_plan_document(_text()), registre, _context(log=ecrit.append))

    assert ecrit, "le journal doit avoir reçu des lignes"
    assert not any(FAUX_TOKEN in ligne for ligne in ecrit)
    # Contrôle positif : le journal n'est pas muet, il transmet le reste intact.
    assert "téléchargement démarré" in ecrit
    assert any("expurgé" in ligne for ligne in ecrit)


def test_un_plan_sans_etape_produit_un_rapport_vide_et_coherent():
    document = _document(steps=())
    rapport = _executer(ex.load_plan_document(_text(document)), _registre(), _context())
    assert rapport.results == ()
    assert rapport.verdict() == ex.VERDICT_OK
    assert rapport.counts()["total"] == 0


def test_l_empreinte_du_plan_execute_est_reportee_dans_le_rapport():
    plan = ex.load_plan_document(_text())
    rapport = _executer(plan, _registre(), _context())
    assert rapport.plan_fingerprint == plan.fingerprint
    assert rapport.plan_generated_at == plan.generated_at


# ══ 7. Verdict et compteurs dérivés ═══════════════════════════════════════════

def test_le_verdict_hierarchise_echec_puis_non_tente_puis_partiel():
    assert _rapport((_resultat(1, ex.STEP_DONE),)).verdict() == ex.VERDICT_OK
    assert _rapport((
        _resultat(1, ex.STEP_DONE), _resultat(2, ex.STEP_SKIPPED),
    )).verdict() == ex.VERDICT_PARTIAL
    assert _rapport((
        _resultat(1, ex.STEP_SKIPPED), _resultat(2, ex.STEP_NOT_ATTEMPTED),
    )).verdict() == ex.VERDICT_INCOMPLETE
    assert _rapport((
        _resultat(1, ex.STEP_FAILED, error="cassé"),
        _resultat(2, ex.STEP_NOT_ATTEMPTED),
    )).verdict() == ex.VERDICT_FAILED


def test_les_codes_de_sortie_suivent_le_verdict():
    assert _rapport((_resultat(1, ex.STEP_DONE),)).exit_code() == ex.EXIT_OK
    assert _rapport((_resultat(1, ex.STEP_SKIPPED),)).exit_code() == ex.EXIT_PARTIAL
    assert _rapport((_resultat(1, ex.STEP_NOT_ATTEMPTED),)).exit_code() == ex.EXIT_FAILED
    assert _rapport((
        _resultat(1, ex.STEP_FAILED, error="cassé"),
    )).exit_code() == ex.EXIT_FAILED


def test_les_compteurs_couvrent_tous_les_statuts_et_le_total():
    rapport = _rapport((
        _resultat(1, ex.STEP_DONE),
        _resultat(2, ex.STEP_ALREADY_SATISFIED),
        _resultat(3, ex.STEP_SKIPPED),
        _resultat(4, ex.STEP_FAILED, error="cassé"),
        _resultat(5, ex.STEP_NOT_ATTEMPTED),
    ))
    counts = rapport.counts()
    assert counts["total"] == 5
    assert set(counts) == set(ex.STEP_STATUS_ORDER) | {"total"}
    assert counts[ex.STEP_WOULD_APPLY] == 0
    assert [r.order for r in rapport.failures()] == [4]
    assert rapport.result(3).status == ex.STEP_SKIPPED
    assert rapport.result(99) is None


# ══ 8. Validation du rapport rendu ════════════════════════════════════════════

def _rendu(results=None, mode=ex.ExecutionMode.APPLY) -> dict:
    results = results if results is not None else (
        _resultat(1, ex.STEP_DONE),
        _resultat(2, ex.STEP_FAILED, error="sha256 divergent"),
        _resultat(3, ex.STEP_NOT_ATTEMPTED),
    )
    return _rapport(results, mode=mode).to_dict()


def test_un_rapport_produit_par_le_contrat_est_valide():
    assert ex.validate_execution_document(_rendu()) == ()


def test_un_rapport_qui_n_est_pas_un_objet_est_refuse():
    assert ex.validate_execution_document([1, 2]) != ()


def test_un_verdict_falsifie_est_rejete():
    document = _rendu()
    document["verdict"] = ex.VERDICT_OK
    erreurs = ex.validate_execution_document(document)
    assert any("verdict annonce" in e for e in erreurs)


def test_un_code_de_sortie_falsifie_est_rejete():
    document = _rendu()
    document["exit_code"] = 0
    assert any("exit_code annonce" in e for e in ex.validate_execution_document(document))


def test_des_compteurs_falsifies_sont_rejetes():
    document = _rendu()
    document["counts"][ex.STEP_FAILED] = 0
    assert any("counts annonce" in e for e in ex.validate_execution_document(document))


def test_des_compteurs_booleens_ne_passent_pas_pour_des_entiers():
    document = _rendu(results=(_resultat(1, ex.STEP_DONE),))
    document["counts"] = {key: (key == ex.STEP_DONE) for key in ex.STEP_STATUS_ORDER}
    document["counts"]["total"] = True
    erreurs = ex.validate_execution_document(document)
    assert any("entier >= 0" in e for e in erreurs)


def test_un_compteur_manquant_ou_inconnu_est_rejete():
    document = _rendu()
    del document["counts"]["total"]
    assert any("counts.total est obligatoire" in e for e in ex.validate_execution_document(document))

    document = _rendu()
    document["counts"]["inventé"] = 1
    assert any("clé inconnue" in e for e in ex.validate_execution_document(document))


def test_une_liste_d_echecs_inventee_de_la_bonne_taille_est_rejetee():
    document = _rendu()
    document["failures"] = [{
        "order": 2,
        "action": sc.ACTION_DOWNLOAD_MODEL,
        "target": "cible-2",
        "error": "rien de grave",
    }]
    erreurs = ex.validate_execution_document(document)
    assert any("failures[0].error" in e for e in erreurs)


def test_une_liste_d_echecs_vidée_est_rejetee():
    document = _rendu()
    document["failures"] = []
    assert any("failures en contient 0" in e for e in ex.validate_execution_document(document))


def test_un_mode_de_simulation_qui_declare_avoir_agi_est_rejete():
    document = _rendu(results=(_resultat(1, ex.STEP_WOULD_APPLY),), mode=ex.ExecutionMode.DRY_RUN)
    assert ex.validate_execution_document(document) == ()

    document["results"][0]["status"] = ex.STEP_DONE
    document["counts"][ex.STEP_WOULD_APPLY] = 0
    document["counts"][ex.STEP_DONE] = 1
    document["verdict"] = ex.VERDICT_OK
    document["exit_code"] = ex.EXIT_OK
    erreurs = ex.validate_execution_document(document)
    assert any("simulation ne modifie rien" in e for e in erreurs)


def test_un_mode_application_qui_n_a_que_simule_est_rejete():
    document = _rendu(results=(_resultat(1, ex.STEP_WOULD_APPLY),), mode=ex.ExecutionMode.APPLY)
    assert any(ex.STEP_WOULD_APPLY in e for e in ex.validate_execution_document(document))


def test_le_drapeau_applied_doit_correspondre_au_mode():
    document = _rendu()
    document["applied"] = False
    assert any("applied annonce" in e for e in ex.validate_execution_document(document))


def test_une_empreinte_mal_formee_est_rejetee():
    for empreinte in ("", "sha256:zz", "0" * 64, "sha256:" + "z" * 64, 42):
        document = _rendu()
        document["plan_fingerprint"] = empreinte
        assert any("plan_fingerprint" in e for e in ex.validate_execution_document(document))


def test_une_version_de_rapport_etrangere_est_rejetee():
    document = _rendu()
    document["schema_version"] = ex.EXECUTION_SCHEMA_VERSION + 1
    assert any("schema_version" in e for e in ex.validate_execution_document(document))


def test_un_outil_etranger_est_rejete():
    document = _rendu()
    document["tool"] = "autre-chose"
    assert any("tool doit valoir" in e for e in ex.validate_execution_document(document))


def test_des_resultats_mal_numerotes_sont_rejetes():
    document = _rendu()
    document["results"][2]["order"] = 5
    assert any("results[2].order" in e for e in ex.validate_execution_document(document))


def test_un_statut_ou_une_action_inconnus_dans_un_rapport_sont_rejetes():
    document = _rendu()
    document["results"][0]["status"] = "presque"
    assert any("status invalide" in e for e in ex.validate_execution_document(document))

    document = _rendu()
    document["results"][0]["action"] = "rm_minus_rf"
    assert any("action inconnue" in e for e in ex.validate_execution_document(document))


def test_une_erreur_sur_un_succes_ou_absente_sur_un_echec_est_rejetee():
    document = _rendu()
    document["results"][0]["error"] = "pourtant réussi"
    assert any("seul un échec porte une erreur" in e for e in ex.validate_execution_document(document))

    document = _rendu()
    document["results"][1]["error"] = None
    assert any("obligatoire pour un échec" in e for e in ex.validate_execution_document(document))

    document = _rendu()
    del document["results"][0]["error"]
    assert any("error est obligatoire" in e for e in ex.validate_execution_document(document))


def test_une_duree_ou_une_preuve_mal_typee_est_rejetee():
    document = _rendu()
    document["results"][0]["duration_ms"] = True
    assert any("duration_ms" in e for e in ex.validate_execution_document(document))

    document = _rendu()
    document["results"][0]["evidence"] = ["pas un objet"]
    assert any("evidence" in e for e in ex.validate_execution_document(document))


def test_un_constat_mal_forme_dans_un_resultat_est_rejete():
    document = _rendu()
    document["results"][0]["findings"] = [{"code": "x", "level": "grave", "message": "m"}]
    assert any("level invalide" in e for e in ex.validate_execution_document(document))


def test_assert_valid_execution_document_leve_sur_incoherence():
    document = _rendu()
    document["verdict"] = ex.VERDICT_OK
    with pytest.raises(ex.ExecutionError):
        ex.assert_valid_execution_document(document)
    # Contrôle positif : un rapport sain ne lève pas.
    ex.assert_valid_execution_document(_rendu())


# ══ 9. Rendu ══════════════════════════════════════════════════════════════════

def test_le_rendu_json_est_relisible_et_porte_l_empreinte():
    rapport = _rapport((_resultat(1, ex.STEP_DONE),))
    document = json.loads(ex.render_execution_json(rapport))
    assert document["tool"] == ex.EXECUTION_TOOL_NAME
    assert document["plan_fingerprint"] == rapport.plan_fingerprint
    assert document["verdict"] == ex.VERDICT_OK
    assert ex.validate_execution_document(document) == ()


def test_le_rendu_refuse_de_publier_un_rapport_qui_fuit():
    fuyant = _rapport((
        _resultat(1, ex.STEP_DONE, evidence={"cmd": f"curl -H 'Authorization: Bearer {FAUX_TOKEN}'"}),
    ))
    with pytest.raises(sc.PlanError):
        ex.render_execution_json(fuyant)
    with pytest.raises(sc.PlanError):
        ex.render_execution_human(fuyant)

    # Contrôle positif : le même rapport sans le jeton est publié, et le rendu
    # imprime bien la preuve — l'assertion d'absence sait donc voir quelque chose.
    propre = _rapport((_resultat(1, ex.STEP_DONE, evidence={"cmd": "curl -H 'Authorization: ***'"}),))
    assert "curl" in ex.render_execution_json(propre)


def test_le_rendu_refuse_un_champ_au_nom_sensible_portant_une_valeur():
    fuyant = _rapport((_resultat(1, ex.STEP_DONE, evidence={"api_key": "valeur-anodine"}),))
    with pytest.raises(sc.PlanError):
        ex.render_execution_json(fuyant)
    # Contrôle positif : le booléen de présence, lui, est la façon recommandée.
    propre = _rapport((_resultat(1, ex.STEP_DONE, evidence={"api_key": True}),))
    assert "api_key" in ex.render_execution_json(propre)


def test_le_rendu_refuse_un_rapport_incoherent():
    import dataclasses

    rapport = dataclasses.replace(
        _rapport((_resultat(1, ex.STEP_DONE),)),
        schema_version=ex.EXECUTION_SCHEMA_VERSION + 1,
    )
    with pytest.raises(ex.ExecutionError):
        ex.render_execution_json(rapport)


def test_le_rendu_humain_ordonne_fait_saute_echoue_non_tente():
    rapport = _rapport((
        _resultat(1, ex.STEP_DONE, summary="artefact téléchargé"),
        _resultat(2, ex.STEP_SKIPPED, summary="vérification déjà faite hier"),
        _resultat(3, ex.STEP_FAILED, error="registre en lecture seule", summary="écriture refusée"),
        _resultat(4, ex.STEP_NOT_ATTEMPTED, summary="non tentée : étape précédente en échec"),
    ))
    texte = ex.render_execution_human(rapport)

    positions = [
        texte.index("CE QUI A ÉTÉ FAIT"),
        texte.index("CE QUI A ÉTÉ SAUTÉ"),
        texte.index("CE QUI A ÉCHOUÉ"),
        texte.index("CE QUI N'A PAS ÉTÉ TENTÉ"),
    ]
    assert positions == sorted(positions)
    assert "vérification déjà faite hier" in texte
    assert "registre en lecture seule" in texte
    assert "ÉCHEC" in texte
    assert f"Sortie : {ex.EXIT_FAILED}" in texte


def test_le_rendu_humain_dit_qu_une_simulation_n_a_rien_applique():
    rapport = _rapport((_resultat(1, ex.STEP_WOULD_APPLY),), mode=ex.ExecutionMode.DRY_RUN)
    texte = ex.render_execution_human(rapport)
    assert "SIMULATION" in texte
    assert "rien n'a été appliqué" in texte
    assert "CE QUI SERAIT FAIT" in texte


def test_le_rendu_humain_ne_porte_aucun_balisage_de_couleur():
    texte = ex.render_execution_human(_rapport((_resultat(1, ex.STEP_DONE),)))
    assert "\x1b[" not in texte
    assert "[/" not in texte
    # Contrôle positif : le rendu n'est pas vide pour autant.
    assert "RAPPORT D'EXÉCUTION EVARUNTIME" in texte


def test_le_rendu_humain_signale_un_plan_sans_etape():
    texte = ex.render_execution_human(_rapport(()))
    assert "AUCUNE ÉTAPE" in texte


# ══ 10. Racines autorisées et journalisation ══════════════════════════════════

def test_aucune_racine_declaree_n_autorise_aucun_chemin(tmp_path: Path):
    with pytest.raises(ex.ExecutionError) as exc:
        ex.ensure_within_allowed_roots(tmp_path / "modele.gguf", ())
    assert "aucune racine autorisée" in str(exc.value)


def test_un_chemin_dans_une_racine_autorisee_est_accepte(tmp_path: Path):
    cible = tmp_path / "models" / "modele.gguf"
    cible.parent.mkdir()
    assert ex.ensure_within_allowed_roots(cible, (tmp_path,)) == cible.resolve()


def test_un_chemin_hors_des_racines_est_refuse(tmp_path: Path):
    racine = tmp_path / "models"
    racine.mkdir()
    with pytest.raises(ex.ExecutionError):
        ex.ensure_within_allowed_roots(racine / ".." / ".." / "etc" / "passwd", (racine,))


def test_un_lien_symbolique_sortant_des_racines_est_refuse(tmp_path: Path):
    racine = tmp_path / "models"
    racine.mkdir()
    dehors = tmp_path / "dehors"
    dehors.mkdir()
    lien = racine / "evasion"
    lien.symlink_to(dehors, target_is_directory=True)

    with pytest.raises(ex.ExecutionError):
        ex.ensure_within_allowed_roots(lien / "fichier.gguf", (racine,))


def test_le_contexte_delegue_la_resolution_de_chemin(tmp_path: Path):
    context = _context(allowed_roots=(tmp_path,))
    assert context.resolve_path(tmp_path / "a" / "b") == (tmp_path / "a" / "b").resolve()
    with pytest.raises(ex.ExecutionError):
        _context().resolve_path(tmp_path / "a")


def test_redact_for_log_expurge_les_formes_de_secret_connues():
    for valeur in (FAUX_TOKEN, "Authorization: Bearer " + "a" * 20, "https://u:p@exemple.fr"):
        assert valeur not in ex.redact_for_log(f"trace : {valeur}")
    # Contrôle positif : un message anodin traverse intact.
    assert ex.redact_for_log("téléchargement à 42 %") == "téléchargement à 42 %"
