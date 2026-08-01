"""
Tests d'AUT-009 (recette du premier token) et d'AUT-010 (pré-chauffage).

Règles tenues ici, toutes vérifiables en lisant le fichier :

- **aucun réseau, aucun serveur, aucune attente réelle** : le client HTTP,
  l'horloge, le sommeil et le tirage d'identité sont injectés. Le seul `sleep`
  du fichier avance une horloge factice ;
- **tout test d'ABSENCE porte un contrôle positif** (règle d'`AGENTS.md`) :
  chercher une clé dans un rapport ne prouve rien si la recherche ne sait pas
  trouver une clé qui s'y trouve. Chaque assertion « X n'apparaît pas » est
  précédée de la démonstration que la même recherche voit un X planté ;
- **le vocabulaire de readiness est recoupé contre `readiness.py`**, qui est
  importé ICI et seulement ici : `bootstrap.first_token` n'en dépend pas, mais
  un renommage de niveau doit casser la suite plutôt que de rendre la recette
  silencieusement aveugle. C'est exactement l'angle mort relevé sur `doctor` en
  vague 4.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, get_args

import pytest

from bootstrap import execution as ex
from bootstrap import first_token as ft
from bootstrap import schema
from bootstrap import warmup as wu

# ── Outillage : horloge, sommeil, transport ───────────────────────────────────


class FakeClock:
    """Horloge monotone pilotée à la main. N'avance que sur ordre explicite."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def fake_now() -> str:
    return "2026-08-01T12:00:00Z"


def make_context(clock: FakeClock, *, mode: ex.ExecutionMode) -> ex.ExecutionContext:
    return ex.ExecutionContext(mode, monotonic=clock, now=fake_now)


def make_sleep(clock: FakeClock):
    """Sommeil factice : avance l'horloge, n'attend jamais une seconde réelle."""

    async def dormir(seconds: float) -> None:
        clock.advance(seconds)

    return dormir


class FakeStream:
    """Réponse streamée factice. Le temps n'avance que par les délais déclarés."""

    def __init__(
        self,
        *,
        status: int,
        lines,
        clock: FakeClock,
        headers_delay: float = 0.05,
        line_delay: float = 0.01,
        raise_at: int | None = None,
        infinite: bool = False,
    ) -> None:
        self._status = status
        self._lines = list(lines)
        self._clock = clock
        self._headers_delay = headers_delay
        self._line_delay = line_delay
        self._raise_at = raise_at
        self._infinite = infinite

    @property
    def status(self) -> int:
        return self._status

    @property
    def headers(self):
        return {"content-type": "text/event-stream"}

    async def __aenter__(self) -> "FakeStream":
        self._clock.advance(self._headers_delay)
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False

    async def aiter_lines(self):
        index = 0
        while True:
            if self._raise_at is not None and index == self._raise_at:
                raise ConnectionResetError("le pair a coupé le flux")
            if index >= len(self._lines):
                if not self._infinite:
                    return
                ligne = self._lines[-1] if self._lines else "data: {}"
            else:
                ligne = self._lines[index]
            self._clock.advance(self._line_delay)
            index += 1
            yield ligne


def chunk(content: str | None = None, *, model: str = "tiny", usage: dict | None = None,
          object_type: str = "chat.completion.chunk", ident: str = "chatcmpl-1") -> str:
    """Une ligne `data:` d'enveloppe OpenAI conforme."""
    delta: dict[str, Any] = {"role": "assistant"} if content is None else {"content": content}
    corps: dict[str, Any] = {
        "id": ident,
        "object": object_type,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
    }
    if usage is not None:
        corps["usage"] = usage
    return "data: " + json.dumps(corps)


USAGE = {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14}

SERVED_LINES = [
    chunk(),                       # chunk de rôle : ne prouve rien
    chunk("ok"),
    chunk(" !"),
    chunk("", usage=USAGE),        # dernier chunk porteur d'usage
    "data: [DONE]",
]

# Clé factice au format réel (`llmgw-` + 32 caractères) : elle est reconnue par
# `schema.find_secret_leaks`, ce qui rend les contrôles positifs crédibles.
EPHEMERAL_KEY = "llmgw-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"
ADMIN_SECRET = "admin-secret-de-test-au-moins-32-caracteres"
SECRET_PROMPT = "phrase-de-prompt-strictement-unique-42"


class FakeGateway:
    """
    Gateway factice : plan de contrôle + flux SSE. Enregistre tous ses appels.

    Chaque comportement est un attribut, de sorte qu'un test n'ait à changer que
    la chose qu'il exerce. `calls` sert aux assertions de séquence et surtout à
    prouver qu'une simulation n'émet AUCUNE requête.
    """

    def __init__(self, clock: FakeClock, *, base_url: str = "https://llm.test",
                 admin_url: str = "http://127.0.0.1:8000") -> None:
        self.clock = clock
        self.base_url = base_url
        self.admin_url = admin_url
        self.calls: list[tuple[str, str]] = []
        self.headers_seen: list[dict] = []
        self.bodies_seen: list[Any] = []

        self.health_status = 200
        self.ready_status = 200
        self.ready_body: dict = {
            "status": "ready", "level": "structural",
            "levels": {"liveness": True, "structural": True, "serving": False},
            "models_ready": [],
        }
        self.models = [
            {"id": "tiny", "enabled": True, "vram_gb": 1.0},
            {"id": "gros", "enabled": True, "vram_gb": 42.0},
        ]
        self.models_status = 200
        self.user_status = 201
        self.key_status = 201
        self.key_body: dict = {"api_key": EPHEMERAL_KEY, "key_prefix": "llmgw-A1b2C3d4"}
        self.load_status = 200
        self.usage_pages: list[list] = [[{"model": "tiny", "total_tokens": 14}]]
        self.usage_status = 200
        self.delete_key_status = 200
        self.delete_user_status = 200
        self.delete_user_body: dict = {"status": "anonymized"}
        self.stream_status = 200
        self.stream_lines = list(SERVED_LINES)
        self.stream_kwargs: dict = {}
        self.stream_raises: Exception | None = None

    # ── plan de contrôle ─────────────────────────────────────────────────────

    async def request(self, method: str, url: str, *, json: Any = None,
                      headers=None, timeout: float) -> ft.HttpResponse:
        chemin = url.replace(self.base_url, "").replace(self.admin_url, "")
        self.calls.append((method, chemin))
        self.headers_seen.append(dict(headers or {}))
        self.bodies_seen.append(json)

        if chemin == "/health":
            return ft.HttpResponse(status=self.health_status, body={"status": "alive"})
        if chemin == "/ready":
            return ft.HttpResponse(status=self.ready_status, body=self.ready_body)
        if chemin == "/admin/models":
            return ft.HttpResponse(status=self.models_status, body=self.models)
        if chemin == "/admin/users" and method == "POST":
            return ft.HttpResponse(status=self.user_status, body={"username": (json or {}).get("username")})
        if chemin.endswith("/keys") and method == "POST":
            return ft.HttpResponse(status=self.key_status, body=dict(self.key_body))
        if chemin.endswith("/load") and method == "POST":
            return ft.HttpResponse(status=self.load_status, body={"status": "loaded"})
        if chemin.startswith("/admin/usage"):
            page = self.usage_pages.pop(0) if len(self.usage_pages) > 1 else (
                self.usage_pages[0] if self.usage_pages else []
            )
            return ft.HttpResponse(status=self.usage_status, body=page)
        if chemin.startswith("/admin/keys/") and method == "DELETE":
            return ft.HttpResponse(status=self.delete_key_status, body={"message": "révoquée"})
        if chemin.startswith("/admin/users/") and method == "DELETE":
            return ft.HttpResponse(status=self.delete_user_status, body=dict(self.delete_user_body))
        return ft.HttpResponse(status=404, body={"detail": "route factice inconnue"})

    # ── chemin public ────────────────────────────────────────────────────────

    def stream(self, method: str, url: str, *, json: Any = None,
               headers=None, timeout: float) -> FakeStream:
        chemin = url.replace(self.base_url, "")
        self.calls.append((method, chemin))
        self.headers_seen.append(dict(headers or {}))
        self.bodies_seen.append(json)
        if self.stream_raises is not None:
            raise self.stream_raises
        return FakeStream(status=self.stream_status, lines=self.stream_lines,
                          clock=self.clock, **self.stream_kwargs)


def make_settings(**overrides) -> ft.FirstTokenSettings:
    base = {
        "base_url": "https://llm.test",
        "admin_url": "http://127.0.0.1:8000",
        "model_id": None,
        "prompt": SECRET_PROMPT,
        "max_tokens": 8,
    }
    base.update(overrides)
    return ft.FirstTokenSettings(**base)


def run_recipe(gateway: FakeGateway, settings: ft.FirstTokenSettings, clock: FakeClock,
               *, mode: ex.ExecutionMode = ex.ExecutionMode.APPLY,
               admin_secret: str = ADMIN_SECRET) -> ft.RecipeReport:
    return asyncio.run(ft.run_first_token_recipe(
        settings=settings, client=gateway, admin_secret=admin_secret,
        context=make_context(clock, mode=mode), sleep=make_sleep(clock),
        identity_suffix=lambda: "deadbeef",
    ))


def analyse(lines, *, http_code: int = 200, expected_model: str = "tiny",
            clock: FakeClock | None = None, headers_delay: float = 0.05,
            line_delay: float = 0.01, infinite: bool = False,
            deadline_s: float = 60.0) -> ft.StreamOutcome:
    horloge = clock or FakeClock()
    depart = horloge()

    async def piloter() -> ft.StreamOutcome:
        flux = FakeStream(status=http_code, lines=lines, clock=horloge,
                          headers_delay=headers_delay, line_delay=line_delay,
                          infinite=infinite)
        async with flux:
            entetes = horloge()
            return await ft.analyse_sse_stream(
                flux.aiter_lines(), http_code=http_code, expected_model=expected_model,
                t_start=depart, t_headers=entetes, monotonic=horloge,
                deadline=depart + deadline_s,
            )

    return asyncio.run(piloter())


# ══ A. Le flux SSE : ce qui prouve, et ce qui ne prouve rien ═════════════════


def test_flux_avec_contenu_est_servi():
    resultat = analyse(SERVED_LINES)
    assert resultat.reason == ft.REASON_OK
    assert resultat.served is True
    assert resultat.content_chunks == 2
    assert resultat.saw_done is True
    assert resultat.prompt_tokens == 11 and resultat.completion_tokens == 3


def test_les_deux_mesures_de_temps_sont_distinctes():
    """§10 points 6 et 7 : en-têtes et premier delta UTILE sont deux mesures."""
    resultat = analyse(SERVED_LINES, headers_delay=0.5, line_delay=0.25)
    # En-têtes à 500 ms ; le premier delta utile est le DEUXIÈME chunk (le
    # premier ne porte qu'un rôle), donc 500 + 250 + 250 = 1000 ms.
    assert resultat.headers_ms == 500
    assert resultat.ttft_ms == 1000
    assert resultat.ttft_ms > resultat.headers_ms


def test_flux_vide_mais_propre_est_un_echec_cor_006():
    """Le cœur d'AUT-009 : ouvert en 200, terminé sur [DONE], zéro contenu."""
    resultat = analyse([chunk(), chunk("", usage=USAGE), "data: [DONE]"])
    assert resultat.reason == ft.REASON_NO_CONTENT
    assert resultat.served is False
    assert resultat.saw_done is True  # le flux s'est bien terminé : il ment par omission


def test_no_content_prime_sur_no_done():
    """Un flux vide ET tronqué doit nommer le défaut COR-006, pas la troncature."""
    resultat = analyse([chunk()])
    assert resultat.reason == ft.REASON_NO_CONTENT


def test_flux_interrompu_avant_done():
    resultat = analyse([chunk(), chunk("ok"), chunk("", usage=USAGE)])
    assert resultat.reason == ft.REASON_NO_DONE


def test_aucun_evenement_sse():
    resultat = analyse([": commentaire", ""])
    assert resultat.reason == ft.REASON_NO_SSE_DATA


def test_erreur_upstream_deguisee_en_200():
    """
    COR-013 : la gateway convertit une panne upstream en chunk d'erreur + [DONE].

    Le flux est syntaxiquement irréprochable — 200, enveloppe, terminaison — et
    ne sert pourtant rien. La recette le refuse.
    """
    erreur = 'data: ' + json.dumps({"error": {"type": "upstream_error", "message": "502"}})
    resultat = analyse([erreur, "data: [DONE]"])
    assert resultat.reason == ft.REASON_UPSTREAM_ERROR
    assert resultat.http_code == 200


def test_erreur_http_avant_le_premier_chunk():
    resultat = analyse(['{"error": {"message": "quota dépassé"}}'], http_code=429)
    assert resultat.reason == ft.REASON_HTTP_STATUS


def test_flux_qui_ne_se_termine_jamais_est_borne():
    """Aucune attente réelle : l'horloge factice atteint la borne, le flux sort."""
    horloge = FakeClock()
    resultat = analyse([chunk("ok")], clock=horloge, line_delay=1.0,
                       infinite=True, deadline_s=5.0)
    assert resultat.reason == ft.REASON_STREAM_TIMEOUT


def test_mauvais_modele_dans_le_flux():
    lignes = [chunk("ok", model="autre"), chunk("", usage=USAGE, model="autre"), "data: [DONE]"]
    assert analyse(lignes).reason == ft.REASON_MODEL_MISMATCH


def test_enveloppe_non_conforme():
    lignes = [chunk("ok", object_type="text_completion"), chunk("", usage=USAGE), "data: [DONE]"]
    assert analyse(lignes).reason == ft.REASON_BAD_ENVELOPE


def test_json_illisible_compte_comme_enveloppe_invalide():
    lignes = ["data: {ceci n'est pas du json", chunk("ok"), chunk("", usage=USAGE), "data: [DONE]"]
    assert analyse(lignes).reason == ft.REASON_BAD_ENVELOPE


def test_absence_de_comptage_de_tokens():
    """Une génération non comptabilisée ne serait pas facturée."""
    assert analyse([chunk("ok"), "data: [DONE]"]).reason == ft.REASON_NO_USAGE


def test_un_token_d_espaces_est_du_contenu():
    """`!= ""` et non `.strip()` : un token blanc EST du contenu généré."""
    resultat = analyse([chunk(" "), chunk("", usage=USAGE), "data: [DONE]"])
    assert resultat.reason == ft.REASON_OK
    assert resultat.content_chars == 1


def test_sse_bufferise_est_signale():
    """Premier delta utile tardif, puis tous les deltas d'un bloc."""
    horloge = FakeClock()

    async def piloter():
        depart = horloge()
        horloge.advance(0.05)          # en-têtes tôt : nginx les laisse passer
        entetes = horloge()
        horloge.advance(2.0)           # puis le corps est retenu

        async def lignes():
            for ligne in [chunk("a"), chunk("b"), chunk("", usage=USAGE), "data: [DONE]"]:
                yield ligne             # tout arrive sans avancer l'horloge

        return await ft.analyse_sse_stream(
            lignes(), http_code=200, expected_model="tiny", t_start=depart,
            t_headers=entetes, monotonic=horloge, deadline=depart + 60.0,
        )

    resultat = asyncio.run(piloter())
    assert resultat.reason == ft.REASON_OK
    assert resultat.max_inter_delta_gap_ms == 0
    assert resultat.buffering_suspected is True


def test_flux_normal_n_est_pas_signale_comme_bufferise():
    """Contrôle négatif du test précédent : des deltas espacés ne déclenchent rien."""
    resultat = analyse(SERVED_LINES, headers_delay=0.05, line_delay=0.3)
    assert resultat.reason == ft.REASON_OK
    assert resultat.buffering_suspected is False


# ══ B. La recette complète ══════════════════════════════════════════════════


def test_recette_complete_produit_une_preuve_exploitable():
    horloge = FakeClock()
    passerelle = FakeGateway(horloge)
    rapport = run_recipe(passerelle, make_settings(), horloge)

    assert rapport.reason == ft.REASON_OK
    assert rapport.served is True
    assert rapport.model == "tiny"           # le plus petit modèle ACTIVÉ
    preuve = rapport.proof()
    assert preuve["kind"] == ft.PROOF_KIND
    assert preuve["verdict"] == ft.PROOF_SERVED
    assert preuve["usage_logged"] is True
    assert preuve["identity_cleaned"] is True
    assert ft.proof_authorizes(preuve, "tiny") is True


def test_la_sequence_traverse_le_chemin_public_puis_le_plan_de_controle():
    """§10 : /health sur le public, /ready en direct, génération sur le public."""
    horloge = FakeClock()
    passerelle = FakeGateway(horloge)
    run_recipe(passerelle, make_settings(), horloge)
    chemins = [c for _, c in passerelle.calls]
    assert chemins[0] == "/health"
    assert chemins[1] == "/ready"
    assert "/v1/chat/completions" in chemins
    assert any(c.startswith("/admin/usage") for c in chemins)
    assert any(c.startswith("/admin/users/") for c in chemins)


def test_la_generation_presente_la_cle_ephemere_et_pas_l_admin_secret():
    """La recette exerce vraiment l'authentification utilisateur, pas l'admin."""
    horloge = FakeClock()
    passerelle = FakeGateway(horloge)
    run_recipe(passerelle, make_settings(), horloge)
    entetes_generation = [
        h for (m, c), h in zip(passerelle.calls, passerelle.headers_seen)
        if c == "/v1/chat/completions"
    ]
    assert entetes_generation
    assert entetes_generation[0]["Authorization"] == f"Bearer {EPHEMERAL_KEY}"
    # /health est publique : y présenter l'ADMIN_SECRET l'exposerait pour rien.
    entetes_health = [
        h for (m, c), h in zip(passerelle.calls, passerelle.headers_seen) if c == "/health"
    ]
    assert entetes_health[0] == {}


def test_le_nom_ephemere_est_toujours_genere():
    horloge = FakeClock()
    passerelle = FakeGateway(horloge)
    run_recipe(passerelle, make_settings(), horloge)
    cree = [
        b for (m, c), b in zip(passerelle.calls, passerelle.bodies_seen)
        if c == "/admin/users" and m == "POST"
    ]
    assert cree[0]["username"] == f"{ft.IDENTITY_PREFIX}-deadbeef"
    assert not hasattr(ft.FirstTokenSettings, "username")


def test_liveness_en_echec_arrete_avant_toute_identite():
    horloge = FakeClock()
    passerelle = FakeGateway(horloge)
    passerelle.health_status = 503
    rapport = run_recipe(passerelle, make_settings(), horloge)
    assert rapport.reason == ft.REASON_LIVENESS
    assert rapport.identity_created is False
    assert not any(m == "POST" and c == "/admin/users" for m, c in passerelle.calls)


def test_readiness_structurelle_en_echec_arrete_la_recette():
    horloge = FakeClock()
    passerelle = FakeGateway(horloge)
    passerelle.ready_status = 503
    passerelle.ready_body = {"status": "not_ready", "level": "none", "reason": "llama_binary_missing"}
    rapport = run_recipe(passerelle, make_settings(), horloge)
    assert rapport.reason == ft.REASON_STRUCTURAL
    assert rapport.readiness_levels["structural"] is False


def test_ready_200_sans_readiness_structurelle_est_refuse():
    """Un 200 ne suffit pas : c'est le niveau annoncé qui décide."""
    horloge = FakeClock()
    passerelle = FakeGateway(horloge)
    passerelle.ready_body = {
        "status": "not_ready", "level": "none",
        "levels": {"liveness": True, "structural": False, "serving": False},
    }
    assert run_recipe(passerelle, make_settings(), horloge).reason == ft.REASON_STRUCTURAL


def test_les_trois_niveaux_de_readiness_sont_visibles_dans_la_preuve():
    horloge = FakeClock()
    passerelle = FakeGateway(horloge)
    rapport = run_recipe(passerelle, make_settings(), horloge)
    # /ready n'annonçait PAS la serving readiness ; c'est la génération qui l'a
    # prouvée. §10 : « le feu vert doit exiger la serving readiness OU un smoke
    # test explicite ».
    assert passerelle.ready_body["levels"]["serving"] is False
    assert rapport.proof()["readiness"] == {"liveness": True, "structural": True, "serving": True}


def test_modele_non_derivable_arrete_avant_l_identite():
    horloge = FakeClock()
    passerelle = FakeGateway(horloge)
    passerelle.models = [{"id": "tiny", "enabled": False, "vram_gb": 1.0}]
    rapport = run_recipe(passerelle, make_settings(), horloge)
    assert rapport.reason == ft.REASON_MODEL_UNRESOLVED
    assert rapport.identity_created is False


def test_chargement_de_modele_en_echec():
    horloge = FakeClock()
    passerelle = FakeGateway(horloge)
    passerelle.load_status = 504
    rapport = run_recipe(passerelle, make_settings(), horloge)
    assert rapport.reason == ft.REASON_MODEL_LOAD
    assert any("COR-009" in s.message for s in rapport.stages)


def test_flux_sans_contenu_fait_echouer_la_recette_entiere():
    horloge = FakeClock()
    passerelle = FakeGateway(horloge)
    passerelle.stream_lines = [chunk(), chunk("", usage=USAGE), "data: [DONE]"]
    rapport = run_recipe(passerelle, make_settings(), horloge)
    assert rapport.reason == ft.REASON_NO_CONTENT
    assert rapport.served is False
    assert ft.proof_authorizes(rapport.proof(), "tiny") is False


def test_coupure_de_transport_pendant_le_flux():
    horloge = FakeClock()
    passerelle = FakeGateway(horloge)
    passerelle.stream_kwargs = {"raise_at": 1}
    rapport = run_recipe(passerelle, make_settings(), horloge)
    assert rapport.reason == ft.REASON_TRANSPORT
    assert rapport.served is False


def test_ouverture_de_flux_impossible():
    horloge = FakeClock()
    passerelle = FakeGateway(horloge)
    passerelle.stream_raises = OSError("connexion refusée")
    rapport = run_recipe(passerelle, make_settings(), horloge)
    assert rapport.reason == ft.REASON_TRANSPORT


def test_log_d_usage_absent_est_un_echec_de_facturation():
    horloge = FakeClock()
    passerelle = FakeGateway(horloge)
    passerelle.usage_pages = [[]]
    rapport = run_recipe(passerelle, make_settings(), horloge)
    assert rapport.reason == ft.REASON_NO_USAGE_LOG
    assert rapport.served is False


def test_log_d_usage_ecrit_en_differe_est_attendu_sans_dormir_reellement():
    horloge = FakeClock()
    passerelle = FakeGateway(horloge)
    passerelle.usage_pages = [[], [], [{"model": "tiny", "total_tokens": 14}]]
    rapport = run_recipe(passerelle, make_settings(), horloge)
    assert rapport.reason == ft.REASON_OK
    assert rapport.usage_entries == 1


def test_une_entree_d_usage_d_un_autre_modele_ne_compte_pas():
    horloge = FakeClock()
    passerelle = FakeGateway(horloge)
    passerelle.usage_pages = [[{"model": "gros", "total_tokens": 14}]]
    assert run_recipe(passerelle, make_settings(), horloge).reason == ft.REASON_NO_USAGE_LOG


def test_le_verdict_derive_refuse_une_generation_non_facturee():
    """
    Défense en profondeur du verdict, indépendante du chemin qui l'a produit.

    La recette n'atteint normalement jamais cet état — `_await_usage_log` sort
    déjà en `usage_log_missing`. Mais `served` est un verdict DÉRIVÉ, et un
    rapport reconstruit à la main (relu depuis un JSON, assemblé par un
    orchestrateur) doit rester refusé s'il annonce `ok` sans facturation. Ce
    test exerce le verdict directement, faute de quoi la condition serait morte
    et personne ne le saurait.
    """
    commun = dict(
        reason=ft.REASON_OK,
        stages=(),
        model="tiny",
        settings_view={"base_url": "https://llm.test"},
        stream=ft.StreamOutcome(reason=ft.REASON_OK, http_code=200, content_chunks=2),
        identity_created=True,
        identity_cleaned=True,
        readiness_levels={"liveness": True, "structural": True, "serving": True},
        observed_at="2026-08-01T12:00:00Z",
    )
    facture = ft.RecipeReport(usage_entries=1, **commun)
    non_facture = ft.RecipeReport(usage_entries=0, **commun)

    assert facture.served is True            # contrôle positif : le reste suffit
    assert non_facture.served is False       # seule la facturation change
    assert ft.proof_authorizes(non_facture.proof(), "tiny") is False


def test_une_entree_d_usage_a_zero_token_ne_compte_pas():
    horloge = FakeClock()
    passerelle = FakeGateway(horloge)
    passerelle.usage_pages = [[{"model": "tiny", "total_tokens": 0}]]
    assert run_recipe(passerelle, make_settings(), horloge).reason == ft.REASON_NO_USAGE_LOG


def test_seuil_ttft_depasse_reste_une_alerte_par_defaut():
    horloge = FakeClock()
    passerelle = FakeGateway(horloge)
    passerelle.stream_kwargs = {"line_delay": 1.0}
    rapport = run_recipe(passerelle, make_settings(ttft_threshold_ms=100), horloge)
    assert rapport.served is True
    assert any(s.code == "ttft_threshold_exceeded" and s.status == ft.STAGE_WARN
               for s in rapport.stages)


def test_seuil_ttft_depasse_est_bloquant_si_demande():
    horloge = FakeClock()
    passerelle = FakeGateway(horloge)
    passerelle.stream_kwargs = {"line_delay": 1.0}
    rapport = run_recipe(
        passerelle, make_settings(ttft_threshold_ms=100, fail_on_ttft=True), horloge
    )
    assert rapport.reason == "ttft_threshold_exceeded"
    assert rapport.served is False


# ── Identité éphémère : DEC-001 et nettoyage inconditionnel ──────────────────


def test_l_identite_est_nettoyee_meme_quand_la_recette_echoue():
    horloge = FakeClock()
    passerelle = FakeGateway(horloge)
    passerelle.load_status = 500          # échec APRÈS création de l'identité
    rapport = run_recipe(passerelle, make_settings(), horloge)
    assert rapport.reason == ft.REASON_MODEL_LOAD
    assert rapport.identity_created is True
    assert rapport.identity_cleaned is True
    assert ("DELETE", "/admin/users/smoke-test-deadbeef") in passerelle.calls
    assert ("DELETE", "/admin/keys/llmgw-A1b2C3d4") in passerelle.calls


def test_le_nettoyage_tolere_une_cle_deja_revoquee():
    horloge = FakeClock()
    passerelle = FakeGateway(horloge)
    passerelle.delete_key_status = 404
    assert run_recipe(passerelle, make_settings(), horloge).identity_cleaned is True


def test_already_anonymized_est_accepte():
    horloge = FakeClock()
    passerelle = FakeGateway(horloge)
    passerelle.delete_user_body = {"status": "already_anonymized"}
    assert run_recipe(passerelle, make_settings(), horloge).identity_cleaned is True


def test_un_204_destructeur_est_refuse_dec_001():
    """
    DEC-001 : la route ANONYMISE et répond 200 avec un statut explicite.

    Un 204 « supprimé » signalerait que le contrat a changé sous nos pieds. La
    recette ne l'accepte pas — l'historique d'usage doit rester consultable
    sous pseudonyme pour que la facturation et l'audit restent cohérents.
    """
    horloge = FakeClock()
    passerelle = FakeGateway(horloge)
    passerelle.delete_user_status = 204
    passerelle.delete_user_body = {}
    rapport = run_recipe(passerelle, make_settings(), horloge)
    assert rapport.identity_cleaned is False
    assert rapport.reason == "identity_residual"


def test_identite_residuelle_invalide_la_recette_meme_si_le_service_sert():
    horloge = FakeClock()
    passerelle = FakeGateway(horloge)
    passerelle.delete_user_status = 500
    rapport = run_recipe(passerelle, make_settings(), horloge)
    assert rapport.stream is not None and rapport.stream.served is True
    assert rapport.served is False
    assert ft.proof_authorizes(rapport.proof(), "tiny") is False
    assert any("à la main" in s.message for s in rapport.stages)


def test_creation_d_identite_en_echec_declenche_quand_meme_le_nettoyage():
    """Un timeout réseau peut avoir créé la ligne malgré un statut inattendu."""
    horloge = FakeClock()
    passerelle = FakeGateway(horloge)
    passerelle.user_status = 500
    rapport = run_recipe(passerelle, make_settings(), horloge)
    assert rapport.reason == ft.REASON_IDENTITY_CREATE
    assert ("DELETE", "/admin/users/smoke-test-deadbeef") in passerelle.calls


def test_cle_inexploitable_arrete_la_recette():
    horloge = FakeClock()
    passerelle = FakeGateway(horloge)
    passerelle.key_body = {"key_prefix": "llmgw-A1b2C3d4"}   # pas d'api_key
    assert run_recipe(passerelle, make_settings(), horloge).reason == ft.REASON_IDENTITY_KEY


# ══ C. Simulation : rien n'est touché, rien n'est autorisé ══════════════════


def test_la_simulation_n_emet_aucune_requete():
    horloge = FakeClock()
    passerelle = FakeGateway(horloge)
    rapport = run_recipe(passerelle, make_settings(), horloge, mode=ex.ExecutionMode.DRY_RUN)
    assert passerelle.calls == []
    assert rapport.dry_run is True
    assert rapport.identity_created is False
    assert all(s.status == ft.STAGE_SKIP for s in rapport.stages)


def test_une_simulation_n_autorise_jamais_l_activation():
    horloge = FakeClock()
    rapport = run_recipe(FakeGateway(horloge), make_settings(model_id="tiny"), horloge,
                         mode=ex.ExecutionMode.DRY_RUN)
    preuve = rapport.proof()
    assert preuve["dry_run"] is True
    assert preuve["verdict"] == ft.PROOF_NOT_SERVED
    assert ft.proof_authorizes(preuve, "tiny") is False


def test_la_simulation_dit_ce_qui_serait_exerce():
    horloge = FakeClock()
    rapport = run_recipe(FakeGateway(horloge), make_settings(), horloge,
                         mode=ex.ExecutionMode.DRY_RUN)
    cibles = " ".join(s.message for s in rapport.stages)
    for attendu in ("/health", "/ready", "/v1/chat/completions", "/admin/usage", "/admin/users"):
        assert attendu in cibles


def test_la_simulation_ne_reclame_pas_de_secret():
    """Une simulation ne joint personne : exiger l'ADMIN_SECRET n'aurait pas de sens."""
    horloge = FakeClock()
    rapport = run_recipe(FakeGateway(horloge), make_settings(), horloge,
                         mode=ex.ExecutionMode.DRY_RUN, admin_secret="")
    assert rapport.dry_run is True


def test_l_application_reelle_exige_un_admin_secret():
    horloge = FakeClock()
    with pytest.raises(ft.FirstTokenError, match="ADMIN_SECRET"):
        run_recipe(FakeGateway(horloge), make_settings(), horloge, admin_secret="")


# ══ D. La preuve de recette : la seule règle admise pour autoriser ══════════


def _preuve_valide() -> dict:
    horloge = FakeClock()
    return run_recipe(FakeGateway(horloge), make_settings(), horloge).proof()


def test_preuve_valide_autorise_le_modele_exerce():
    assert ft.proof_authorizes(_preuve_valide(), "tiny") is True


@pytest.mark.parametrize("cle,valeur", [
    ("kind", "autre.preuve"),
    ("version", 2),
    ("verdict", "peut-etre"),
    ("verdict", ft.PROOF_NOT_SERVED),
    ("dry_run", True),
    ("model_id", "gros"),
    ("usage_logged", False),
    ("reason", ft.REASON_NO_CONTENT),
])
def test_une_preuve_alteree_n_autorise_rien(cle, valeur):
    """Chaque condition est exprimée en égalité positive : elle refuse seule."""
    preuve = _preuve_valide()
    preuve[cle] = valeur
    assert ft.proof_authorizes(preuve, "tiny") is False


def test_une_preuve_tronquee_ou_absurde_n_autorise_rien():
    for document in (None, {}, [], "served", {"kind": ft.PROOF_KIND}):
        assert ft.proof_authorizes(document, "tiny") is False


def test_un_modele_vide_n_est_jamais_autorise():
    assert ft.proof_authorizes(_preuve_valide(), "") is False


# ══ E. Secrets — chaque absence porte son contrôle positif ══════════════════


def test_la_cle_ephemere_ne_ressort_nulle_part():
    horloge = FakeClock()
    passerelle = FakeGateway(horloge)
    rapport = run_recipe(passerelle, make_settings(), horloge)
    rendu = json.dumps(rapport.to_dict(), ensure_ascii=False)

    # CONTRÔLE POSITIF : la recherche sait trouver la clé quand elle est là.
    assert EPHEMERAL_KEY in json.dumps({"fuite": EPHEMERAL_KEY})
    # ... et elle n'est pas là.
    assert EPHEMERAL_KEY not in rendu
    assert "llmgw-A1b2C3d4E5" not in rendu


def test_l_admin_secret_ne_ressort_nulle_part():
    horloge = FakeClock()
    rapport = run_recipe(FakeGateway(horloge), make_settings(), horloge)
    rendu = json.dumps(rapport.to_dict(), ensure_ascii=False)

    assert ADMIN_SECRET in json.dumps({"fuite": ADMIN_SECRET})     # contrôle positif
    assert ADMIN_SECRET not in rendu


def test_ni_le_prompt_ni_le_contenu_genere_ne_ressortent():
    horloge = FakeClock()
    passerelle = FakeGateway(horloge)
    passerelle.stream_lines = [
        chunk(), chunk("TEXTE-GENERE-UNIQUE-99"), chunk("", usage=USAGE), "data: [DONE]",
    ]
    rapport = run_recipe(passerelle, make_settings(), horloge)
    rendu = json.dumps(rapport.to_dict(), ensure_ascii=False)

    # CONTRÔLE POSITIF : les deux chaînes sont détectables par cette recherche.
    assert SECRET_PROMPT in json.dumps({"x": SECRET_PROMPT})
    assert "TEXTE-GENERE-UNIQUE-99" in json.dumps({"x": "TEXTE-GENERE-UNIQUE-99"})

    assert SECRET_PROMPT not in rendu
    assert "TEXTE-GENERE-UNIQUE-99" not in rendu
    # Seuls des compteurs subsistent, ce qui prouve que le contenu a bien été vu.
    assert rapport.stream is not None and rapport.stream.content_chars == 22


def test_un_rapport_qui_fuirait_n_est_pas_publie():
    """Le dernier filet : `_guard_no_leak` échoue au lieu de publier."""
    horloge = FakeClock()
    passerelle = FakeGateway(horloge)
    # Le modèle est repris tel quel dans le rapport : on y plante un secret.
    passerelle.models = [{"id": "hf_" + "z" * 24, "enabled": True, "vram_gb": 1.0}]
    rapport = run_recipe(passerelle, make_settings(), horloge)
    assert rapport.reason == ft.REASON_LEAK
    assert rapport.served is False
    assert schema.find_secret_leaks(rapport.to_dict()) == ()


def test_le_filet_a_secrets_voit_bien_une_fuite_controle_positif():
    """Contrôle positif du test précédent : le détecteur n'est pas inerte."""
    assert schema.find_secret_leaks({"m": "hf_" + "z" * 24}) != ()


def test_les_compteurs_portent_les_noms_de_l_api_et_restent_publiables():
    """
    Le contournement de nommage est retiré, et rien ne casse — c'est la preuve.

    Les compteurs se sont appelés `prompt_units` / `completion_units` parce que
    `schema._SECRET_KEY_RE` frappait `token` en sous-chaîne et rendait le rapport
    ENTIER impubliable. Le motif est désormais ancré. Ce test verrouille les deux
    faces : les noms sont bien ceux de l'API, ET le document reste publiable.
    """
    horloge = FakeClock()
    rapport = run_recipe(FakeGateway(horloge), make_settings(), horloge)

    def cles(noeud):
        if isinstance(noeud, dict):
            for cle, valeur in noeud.items():
                yield str(cle)
                yield from cles(valeur)
        elif isinstance(noeud, list):
            for valeur in noeud:
                yield from cles(valeur)

    document = rapport.to_dict()
    assert list(cles(document))                                    # contrôle positif
    assert "completion_tokens" in list(cles(document))
    # Contrôle positif du détecteur : un nom de porteur, lui, fuit toujours.
    assert schema.find_secret_leaks({"hf_token": "x"}) != ()
    assert schema.find_secret_leaks(document) == ()
    assert rapport.proof()["prompt_tokens"] == 11
    assert rapport.proof()["completion_tokens"] == 3


def test_une_url_avec_identifiants_est_refusee_a_la_construction():
    with pytest.raises(ft.FirstTokenError, match="identifiants"):
        make_settings(base_url="https://user:motdepasse@llm.test")
    with pytest.raises(ft.FirstTokenError, match="identifiants"):
        make_settings(admin_url="http://admin:secret@127.0.0.1:8000")


@pytest.mark.parametrize("kwargs", [
    {"base_url": ""},
    {"max_tokens": 0},
    {"max_tokens": True},
    {"ttft_threshold_ms": -1},
    {"fail_on_ttft": True},
    {"stream_timeout_s": 0},
    {"usage_timeout_s": -3},
])
def test_des_reglages_inexploitables_sont_refuses(kwargs):
    with pytest.raises(ft.FirstTokenError):
        make_settings(**kwargs)


# ══ F. Vocabulaire de readiness : recoupé contre la source ══════════════════


def test_les_niveaux_de_readiness_correspondent_a_readiness_py():
    """
    Le module n'importe pas `readiness` — ce test, si.

    Sans ce recoupement, un renommage de niveau dans `readiness.py` rendrait la
    recette silencieusement aveugle : elle lirait `levels.serving` sur un corps
    qui ne le porte plus, conclurait « pas encore servi », et personne ne
    verrait rien. C'est la classe d'angle mort relevée sur `doctor` en vague 4.
    """
    import readiness

    assert set(ft.READINESS_LEVELS) == set(get_args(readiness.ReadinessLevel))
    assert ft.LEVEL_SERVING in get_args(readiness.ReadinessLevel)
    assert ft.LEVEL_STRUCTURAL in get_args(readiness.ReadinessLevel)


def test_le_corps_public_de_ready_porte_bien_les_cles_consommees():
    """La recette lit `levels`, `models_ready` et `level` : ils doivent exister."""
    import readiness

    rapport = readiness.ReadinessReport(
        mode="local", checks=(), models_ready=("tiny",), vram_available_gb=10.0,
    )
    corps = rapport.public_body()
    assert set(corps["levels"]) == {"liveness", "structural", "serving"}
    assert corps["models_ready"] == ["tiny"]
    assert corps["level"] == ft.LEVEL_SERVING


# ══ G. Branchement dans le contrat d'exécution ══════════════════════════════


def plan_document(actions) -> str:
    sections = [{
        "name": schema.SECTION_MODELS, "section_version": 1, "status": "ok",
        "summary": "registre sain", "data": {}, "findings": [], "notes": [],
    }]
    steps = [
        {
            "order": index + 1, "action": action, "target": "tiny",
            "detail": "étape de test", "requires_root": False, "reversible": True,
            "estimated_bytes": None,
        }
        for index, action in enumerate(actions)
    ]
    document = {
        "tool": schema.PLAN_TOOL_NAME, "schema_version": schema.PLAN_SCHEMA_VERSION,
        "generated_at": "2026-08-01T11:00:00Z", "mode": "local", "strict": False,
        "status": "ok", "applicable": True, "exit_code": schema.EXIT_OK,
        "estimated_download_bytes": 0, "sections": sections, "steps": steps,
        "decisions": [], "blockers": [], "warnings": [],
        "counts": {"ok": 1, "warn": 0, "fail": 0, "skip": 0,
                   "steps": len(steps), "decisions": 0},
    }
    return json.dumps(document)


def test_le_plan_de_test_est_accepte_par_le_contrat():
    """Contrôle positif du harnais : sans lui, un plan refusé passerait pour un bug."""
    charge = ex.load_plan_document(plan_document([schema.ACTION_SMOKE_TEST]))
    assert len(charge.steps) == 1


def test_execution_du_plan_avec_les_deux_executeurs():
    horloge = FakeClock()
    passerelle = FakeGateway(horloge)
    registre = ex.ExecutorRegistry()
    ft.register_executors(registre, settings=make_settings(model_id="tiny"),
                          client=passerelle, admin_secret=ADMIN_SECRET,
                          sleep=make_sleep(horloge), identity_suffix=lambda: "deadbeef")
    passerelle.ready_body = {
        "status": "ready", "level": "serving",
        "levels": {"liveness": True, "structural": True, "serving": True},
        "models_ready": ["tiny"],
    }
    wu.register_executors(
        registre,
        settings=wu.WarmupSettings(admin_url=passerelle.admin_url, model_id="tiny",
                                   timeout_seconds=190),
        client=passerelle, admin_secret=ADMIN_SECRET,
        generation_probe=lambda: _sonde_servie(), sleep=make_sleep(horloge),
    )
    assert set(registre.registered_actions()) == {
        schema.ACTION_SMOKE_TEST, schema.ACTION_WARMUP_MODEL,
    }

    charge = ex.load_plan_document(
        plan_document([schema.ACTION_WARMUP_MODEL, schema.ACTION_SMOKE_TEST])
    )
    rapport = asyncio.run(ex.execute_plan(
        charge, registre, make_context(horloge, mode=ex.ExecutionMode.APPLY)
    ))
    assert rapport.verdict() == ex.VERDICT_OK
    assert rapport.exit_code() == ex.EXIT_OK
    # Le rendu applique `assert_no_secrets` et `assert_valid_execution_document`.
    rendu = ex.render_execution_json(rapport)
    assert ADMIN_SECRET not in rendu and EPHEMERAL_KEY not in rendu


async def _sonde_servie() -> wu.ProbeOutcome:
    return wu.ProbeOutcome(served=True, ttft_ms=120)


def test_une_recette_en_echec_donne_un_rapport_d_execution_en_echec():
    horloge = FakeClock()
    passerelle = FakeGateway(horloge)
    passerelle.stream_lines = [chunk(), chunk("", usage=USAGE), "data: [DONE]"]
    registre = ex.ExecutorRegistry()
    ft.register_executors(registre, settings=make_settings(model_id="tiny"),
                          client=passerelle, admin_secret=ADMIN_SECRET,
                          sleep=make_sleep(horloge), identity_suffix=lambda: "deadbeef")
    charge = ex.load_plan_document(plan_document([schema.ACTION_SMOKE_TEST]))
    rapport = asyncio.run(ex.execute_plan(
        charge, registre, make_context(horloge, mode=ex.ExecutionMode.APPLY)
    ))
    assert rapport.verdict() == ex.VERDICT_FAILED
    resultat = rapport.result(1)
    assert resultat is not None and resultat.status == ex.STEP_FAILED
    assert ft.REASON_NO_CONTENT in (resultat.error or "")
    ex.render_execution_json(rapport)     # le rapport reste publiable


def test_la_simulation_produit_would_apply_et_un_code_partiel():
    horloge = FakeClock()
    passerelle = FakeGateway(horloge)
    registre = ex.ExecutorRegistry()
    ft.register_executors(registre, settings=make_settings(model_id="tiny"),
                          client=passerelle, admin_secret=ADMIN_SECRET)
    charge = ex.load_plan_document(plan_document([schema.ACTION_SMOKE_TEST]))
    rapport = asyncio.run(ex.execute_plan(
        charge, registre, make_context(horloge, mode=ex.ExecutionMode.DRY_RUN)
    ))
    assert rapport.result(1).status == ex.STEP_WOULD_APPLY
    assert rapport.exit_code() == ex.EXIT_PARTIAL
    assert passerelle.calls == []


def test_un_second_enregistrement_est_refuse():
    registre = ex.ExecutorRegistry()
    horloge = FakeClock()
    for _ in range(1):
        ft.register_executors(registre, settings=make_settings(model_id="tiny"),
                              client=FakeGateway(horloge), admin_secret=ADMIN_SECRET)
    with pytest.raises(ex.ExecutionError, match="déjà enregistré"):
        ft.register_executors(registre, settings=make_settings(model_id="tiny"),
                              client=FakeGateway(horloge), admin_secret=ADMIN_SECRET)


# ══════════════════════════════════════════════════════════════════════════════
# AUT-010 — pré-chauffage
# ══════════════════════════════════════════════════════════════════════════════


def make_warmup_settings(**overrides) -> wu.WarmupSettings:
    base = {"admin_url": "http://127.0.0.1:8000", "model_id": "tiny", "timeout_seconds": 190}
    base.update(overrides)
    return wu.WarmupSettings(**base)


def run_warmup(gateway: FakeGateway, clock: FakeClock, *, probe=None,
               settings: wu.WarmupSettings | None = None,
               mode: ex.ExecutionMode = ex.ExecutionMode.APPLY) -> wu.WarmupOutcome:
    return asyncio.run(wu.run_warmup(
        settings=settings or make_warmup_settings(), client=gateway,
        admin_secret=ADMIN_SECRET, context=make_context(clock, mode=mode),
        generation_probe=probe, sleep=make_sleep(clock),
    ))


def serving_body(models=("tiny",)) -> dict:
    return {
        "status": "ready", "level": "serving",
        "levels": {"liveness": True, "structural": True, "serving": True},
        "models_ready": list(models),
    }


# ── La borne est dérivée du registre, jamais inventée ────────────────────────


def test_la_borne_vient_du_modele_en_priorite():
    assert wu.derive_warmup_timeout_seconds(
        model_load_timeout_seconds=300, default_load_timeout_seconds=180
    ) == 310


def test_la_borne_retombe_sur_le_reglage_global():
    assert wu.derive_warmup_timeout_seconds(
        model_load_timeout_seconds=None, default_load_timeout_seconds=180
    ) == 190


def test_sans_aucune_valeur_le_prechauffage_refuse_de_deviner():
    with pytest.raises(wu.WarmupError, match="constante inventée"):
        wu.derive_warmup_timeout_seconds(
            model_load_timeout_seconds=None, default_load_timeout_seconds=None
        )


@pytest.mark.parametrize("valeur", [0, -1, True, 1.5, "300"])
def test_une_borne_non_entiere_ou_nulle_est_refusee(valeur):
    with pytest.raises(wu.WarmupError):
        wu.derive_warmup_timeout_seconds(
            model_load_timeout_seconds=valeur, default_load_timeout_seconds=180
        )


def test_la_grace_est_celle_de_la_gateway_pas_une_invention():
    """
    Recoupé contre `doctor`, qui applique déjà la formule d'`ensure_loaded`.

    Si l'un des deux change sans l'autre, le pré-chauffage bornerait le
    chargement plus court que ce que la gateway autorise — et tuerait un
    déploiement sain (COR-009 à l'envers).
    """
    import doctor

    assert wu.LOAD_TIMEOUT_GRACE_SECONDS == doctor._LOAD_TIMEOUT_GRACE_SECONDS


def test_une_borne_posee_a_la_main_est_refusee():
    """Une valeur qui n'excède pas la seule grâce n'a pas pu être dérivée."""
    with pytest.raises(wu.WarmupError, match="dérivée"):
        make_warmup_settings(timeout_seconds=wu.LOAD_TIMEOUT_GRACE_SECONDS)


@pytest.mark.parametrize("kwargs", [
    {"admin_url": ""},
    {"admin_url": "http://admin:secret@127.0.0.1:8000"},
    {"model_id": ""},
    {"timeout_seconds": 190.0},
    {"timeout_seconds": True},
    {"poll_interval_s": 0},
])
def test_des_reglages_de_prechauffage_inexploitables_sont_refuses(kwargs):
    with pytest.raises(wu.WarmupError):
        make_warmup_settings(**kwargs)


# ── Le pré-chauffage lui-même ───────────────────────────────────────────────


def test_prechauffage_reussi_autorise_le_trafic():
    horloge = FakeClock()
    passerelle = FakeGateway(horloge)
    passerelle.ready_body = serving_body()
    resultat = run_warmup(passerelle, horloge, probe=_sonde_servie)
    assert resultat.reason == wu.REASON_OK
    assert resultat.traffic_authorized is True
    assert resultat.model_loaded is True and resultat.serving_ready is True
    decision = resultat.release_decision()
    assert decision.declare_new_version is True
    assert decision.retain_previous_version is False


def test_le_chargement_precede_l_attente_de_health():
    horloge = FakeClock()
    passerelle = FakeGateway(horloge)
    passerelle.ready_body = serving_body()
    run_warmup(passerelle, horloge, probe=_sonde_servie)
    chemins = [c for _, c in passerelle.calls]
    assert chemins[0] == "/admin/models/tiny/load"
    assert chemins[1] == "/ready"


def test_chargement_en_echec_conserve_l_ancienne_version():
    horloge = FakeClock()
    passerelle = FakeGateway(horloge)
    passerelle.load_status = 500
    resultat = run_warmup(passerelle, horloge, probe=_sonde_servie)
    assert resultat.reason == wu.REASON_LOAD_FAILED
    assert resultat.traffic_authorized is False
    decision = resultat.release_decision()
    assert decision.declare_new_version is False
    assert decision.retain_previous_version is True


def test_un_504_sur_le_chargement_designe_cor_009():
    horloge = FakeClock()
    passerelle = FakeGateway(horloge)
    passerelle.load_status = 504
    resultat = run_warmup(passerelle, horloge, probe=_sonde_servie)
    assert any("COR-009" in s.message for s in resultat.stages)


def test_health_jamais_atteinte_expire_sur_la_borne_derivee():
    """Aucune attente réelle : le sommeil factice avance l'horloge jusqu'à la borne."""
    horloge = FakeClock()
    passerelle = FakeGateway(horloge)
    passerelle.ready_body = {
        "status": "ready", "level": "structural",
        "levels": {"liveness": True, "structural": True, "serving": False},
        "models_ready": [],
    }
    resultat = run_warmup(passerelle, horloge, probe=_sonde_servie)
    assert resultat.reason == wu.REASON_WARMUP_TIMEOUT
    assert resultat.traffic_authorized is False
    assert any("190 s" in s.message for s in resultat.stages)


def test_un_autre_modele_pret_ne_vaut_pas_pour_le_notre():
    horloge = FakeClock()
    passerelle = FakeGateway(horloge)
    passerelle.ready_body = serving_body(models=("gros",))
    assert run_warmup(passerelle, horloge, probe=_sonde_servie).reason == wu.REASON_WARMUP_TIMEOUT


def test_ready_injoignable_est_distingue_d_un_depassement():
    horloge = FakeClock()
    passerelle = FakeGateway(horloge)
    passerelle.ready_status = 503
    passerelle.ready_body = {"status": "not_ready", "reason": "db_unreachable"}
    resultat = run_warmup(passerelle, horloge, probe=_sonde_servie)
    assert resultat.reason == wu.REASON_READY_UNREACHABLE


def test_health_atteinte_au_troisieme_sondage():
    horloge = FakeClock()
    tentatives = {"n": 0}
    non_pret = {
        "status": "ready", "level": "structural",
        "levels": {"liveness": True, "structural": True, "serving": False},
        "models_ready": [],
    }

    class Progressive(FakeGateway):
        async def request(self, method, url, *, json=None, headers=None, timeout):
            if url.endswith("/ready"):
                tentatives["n"] += 1
                self.ready_body = serving_body() if tentatives["n"] >= 3 else non_pret
            return await super().request(method, url, json=json, headers=headers, timeout=timeout)

    progressive = Progressive(horloge)
    resultat = run_warmup(progressive, horloge, probe=_sonde_servie)
    assert resultat.reason == wu.REASON_OK
    assert tentatives["n"] == 3
    assert resultat.ready_wait_ms > 0


# ── Fail-closed sur la génération courte ────────────────────────────────────


def test_sans_sonde_le_trafic_reste_ferme():
    """
    §10 exige une génération courte. Un chargement réussi ne la remplace pas.

    C'est le fail-closed le plus important du module : sans lui, un
    pré-chauffage qui charge un modèle mort ouvrirait le trafic.
    """
    horloge = FakeClock()
    passerelle = FakeGateway(horloge)
    passerelle.ready_body = serving_body()
    resultat = run_warmup(passerelle, horloge, probe=None)
    assert resultat.reason == wu.REASON_NO_PROBE
    assert resultat.model_loaded is True and resultat.serving_ready is True
    assert resultat.traffic_authorized is False
    assert resultat.release_decision().retain_previous_version is True


def test_une_sonde_en_echec_ferme_le_trafic():
    horloge = FakeClock()
    passerelle = FakeGateway(horloge)
    passerelle.ready_body = serving_body()

    async def sonde_muette() -> wu.ProbeOutcome:
        return wu.ProbeOutcome(served=False, reason=ft.REASON_NO_CONTENT)

    resultat = run_warmup(passerelle, horloge, probe=sonde_muette)
    assert resultat.reason == wu.REASON_PROBE_FAILED
    assert resultat.traffic_authorized is False


def test_une_sonde_qui_leve_ne_fait_pas_tomber_le_prechauffage():
    horloge = FakeClock()
    passerelle = FakeGateway(horloge)
    passerelle.ready_body = serving_body()

    async def sonde_cassee() -> wu.ProbeOutcome:
        raise RuntimeError("le harnais de sonde est cassé")

    resultat = run_warmup(passerelle, horloge, probe=sonde_cassee)
    assert resultat.reason == wu.REASON_PROBE_FAILED
    assert resultat.probe is not None and resultat.probe.served is False


def test_une_sonde_qui_rend_n_importe_quoi_est_refusee():
    horloge = FakeClock()
    passerelle = FakeGateway(horloge)
    passerelle.ready_body = serving_body()

    async def sonde_menteuse():
        return True          # « oui » n'est pas un ProbeOutcome

    resultat = run_warmup(passerelle, horloge, probe=sonde_menteuse)
    assert resultat.reason == wu.REASON_PROBE_FAILED
    assert resultat.traffic_authorized is False


def _resultat_prechauffage(**overrides) -> wu.WarmupOutcome:
    base = dict(
        reason=wu.REASON_OK, stages=(), model="tiny",
        settings_view={"model": "tiny"}, model_loaded=True, serving_ready=True,
        probe=wu.ProbeOutcome(served=True, ttft_ms=90),
        observed_at="2026-08-01T12:00:00Z", dry_run=False,
    )
    base.update(overrides)
    return wu.WarmupOutcome(**base)


@pytest.mark.parametrize("champ,valeur", [
    ("reason", wu.REASON_WARMUP_TIMEOUT),
    ("model_loaded", False),
    ("serving_ready", False),
    ("probe", None),
    ("probe", wu.ProbeOutcome(served=False, reason=wu.REASON_PROBE_FAILED)),
    ("dry_run", True),
])
def test_chaque_condition_du_feu_vert_refuse_seule(champ, valeur):
    """
    `traffic_authorized` est un verdict DÉRIVÉ : chaque condition est exercée ici.

    Les chemins normaux masquent plusieurs de ces conditions — une simulation
    n'a de toute façon rien chargé, une sonde absente sort déjà en
    `generation_not_probed`. Sans ce test direct, ces clauses seraient mortes,
    et un rapport reconstruit à la main (relu depuis un JSON, assemblé par un
    orchestrateur) pourrait ouvrir le trafic sans preuve.
    """
    assert _resultat_prechauffage().traffic_authorized is True      # contrôle positif
    altere = _resultat_prechauffage(**{champ: valeur})
    assert altere.traffic_authorized is False
    assert altere.release_decision().declare_new_version is False
    assert altere.release_decision().retain_previous_version is True


def test_la_simulation_de_prechauffage_n_emet_rien_et_n_autorise_rien():
    horloge = FakeClock()
    passerelle = FakeGateway(horloge)
    resultat = run_warmup(passerelle, horloge, probe=_sonde_servie,
                          mode=ex.ExecutionMode.DRY_RUN)
    assert passerelle.calls == []
    assert resultat.dry_run is True
    assert resultat.traffic_authorized is False
    assert resultat.release_decision().retain_previous_version is True


def test_la_simulation_sans_sonde_le_dit():
    horloge = FakeClock()
    resultat = run_warmup(FakeGateway(horloge), horloge, probe=None,
                          mode=ex.ExecutionMode.DRY_RUN)
    assert any(s.code == wu.REASON_NO_PROBE for s in resultat.stages)


def test_le_prechauffage_reel_exige_un_admin_secret():
    horloge = FakeClock()
    with pytest.raises(wu.WarmupError, match="ADMIN_SECRET"):
        asyncio.run(wu.run_warmup(
            settings=make_warmup_settings(), client=FakeGateway(horloge),
            admin_secret="", context=make_context(horloge, mode=ex.ExecutionMode.APPLY),
            generation_probe=_sonde_servie, sleep=make_sleep(horloge),
        ))


def test_l_admin_secret_ne_ressort_pas_du_prechauffage():
    horloge = FakeClock()
    passerelle = FakeGateway(horloge)
    passerelle.ready_body = serving_body()
    resultat = run_warmup(passerelle, horloge, probe=_sonde_servie)
    rendu = json.dumps(resultat.to_dict(), ensure_ascii=False)

    assert ADMIN_SECRET in json.dumps({"fuite": ADMIN_SECRET})    # contrôle positif
    assert ADMIN_SECRET not in rendu


def test_un_resultat_de_prechauffage_qui_fuirait_n_est_pas_publie():
    horloge = FakeClock()
    passerelle = FakeGateway(horloge)
    passerelle.ready_body = serving_body(models=("hf_" + "z" * 24,))
    resultat = run_warmup(
        passerelle, horloge, probe=_sonde_servie,
        settings=make_warmup_settings(model_id="hf_" + "z" * 24),
    )
    assert resultat.reason == "report_leak"
    assert resultat.traffic_authorized is False
    assert schema.find_secret_leaks(resultat.to_dict()) == ()


def test_l_executeur_de_prechauffage_echoue_proprement_dans_le_plan():
    horloge = FakeClock()
    passerelle = FakeGateway(horloge)
    passerelle.load_status = 500
    registre = ex.ExecutorRegistry()
    wu.register_executors(registre, settings=make_warmup_settings(), client=passerelle,
                          admin_secret=ADMIN_SECRET, generation_probe=_sonde_servie,
                          sleep=make_sleep(horloge))
    charge = ex.load_plan_document(plan_document([schema.ACTION_WARMUP_MODEL]))
    rapport = asyncio.run(ex.execute_plan(
        charge, registre, make_context(horloge, mode=ex.ExecutionMode.APPLY)
    ))
    assert rapport.verdict() == ex.VERDICT_FAILED
    resultat = rapport.result(1)
    assert resultat is not None
    assert resultat.evidence["release"]["retain_previous_version"] is True
    ex.render_execution_json(rapport)
