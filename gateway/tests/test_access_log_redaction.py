"""
Tests de non-journalisation des noms d'utilisateur dans les chemins de requête.

Contexte. La politique d'anonymisation RGPD (COR-002 / DEC-001) efface le
`username` de la base, mais le middleware d'accès journalisait
`request.url.path` tel quel — donc le nom réapparaissait dans les journaux pour
chaque appel à `/admin/users/{username}`. Anonymiser en base tout en conservant
une copie dans les logs ne satisfait pas la Definition of Done (§14 :
« aucune donnée sensible n'est journalisée »).

Ces tests verrouillent la rédaction du segment de nom, tout en vérifiant que la
forme de la route reste exploitable pour le diagnostic.

SEC-010 étend la couverture à deux fuites que `_redact_path` ne pouvait pas
fermer :

- la **query string** (`GET /admin/usage?username=…`, employé par
  `deploy/smoke_test.sh`), que le middleware ne journalisait pas — il ne passe
  que `request.url.path` — mais qui transitait ailleurs ;
- le **journal d'accès d'uvicorn**, qui tient sa propre ligne
  `'%s - "%s %s HTTP/%s" %d'` où le troisième argument est
  `get_path_with_query_string(scope)` : chemin ET requête, sans rédaction.
  `--access-log` est actif dans les deux unités systemd, donc en production
  `_redact_path` était contourné pour le chemin lui-même.
"""
from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

import database as db
import main
from config import settings

# Nom improbable dans un log par accident : toute occurrence est une fuite.
CANARY = "prenom.nom-canari-42"


@pytest.fixture
def client() -> TestClient:
    return TestClient(main.app)


@pytest.fixture
def admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.admin_secret}"}


@pytest.fixture
def file_db(tmp_path, monkeypatch):
    """Base sur fichier : `:memory:` ne persiste pas d'un appel HTTP à l'autre."""
    monkeypatch.setattr(settings, "db_path", tmp_path / "redaction.db")
    import asyncio

    asyncio.run(db.init_db())
    return settings.db_path


# ── Rédaction pure ────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "path, expected",
    [
        (f"/admin/users/{CANARY}", "/admin/users/<redacted>"),
        (f"/admin/users/{CANARY}/keys", "/admin/users/<redacted>/keys"),
        # Le collectif n'a pas de segment de nom : inchangé.
        ("/admin/users", "/admin/users"),
        # Routes sans nom d'utilisateur : jamais altérées.
        ("/admin/status", "/admin/status"),
        ("/v1/chat/completions", "/v1/chat/completions"),
        ("/health", "/health"),
        ("/admin/keys/llmgw-abc123", "/admin/keys/llmgw-abc123"),
    ],
)
def test_redact_path(path: str, expected: str) -> None:
    assert main._redact_path(path) == expected


def test_redact_path_preserves_trailing_slash_form() -> None:
    """Un chemin sans nom après le préfixe reste inchangé (pas de `<redacted>` fantôme)."""
    assert main._redact_path("/admin/users/") == "/admin/users/"


# ── Chemin réel, via le middleware ────────────────────────────────────────────

def test_access_log_omits_username_on_get(
    client, admin_headers, file_db, caplog
) -> None:
    with caplog.at_level(logging.INFO, logger="main"):
        resp = client.get(f"/admin/users/{CANARY}", headers=admin_headers)
    assert resp.status_code == 404  # l'utilisateur n'existe pas : peu importe ici
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert CANARY not in logged, f"nom d'utilisateur journalisé : {logged}"
    assert "/admin/users/<redacted>" in logged


def test_access_log_omits_username_on_anonymize(
    client, admin_headers, file_db, caplog
) -> None:
    """Le chemin du droit à l'effacement ne doit surtout pas journaliser le nom."""
    created = client.post(
        "/admin/users",
        headers=admin_headers,
        json={"username": CANARY, "email": "canari@example.test"},
    )
    assert created.status_code == 201

    with caplog.at_level(logging.INFO):
        resp = client.delete(f"/admin/users/{CANARY}", headers=admin_headers)
    assert resp.status_code == 200

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert CANARY not in logged, f"nom d'utilisateur journalisé : {logged}"
    assert "canari@example.test" not in logged, "e-mail journalisé"
    assert "/admin/users/<redacted>" in logged


def test_access_log_omits_username_on_key_creation(
    client, admin_headers, file_db, caplog
) -> None:
    created = client.post(
        "/admin/users", headers=admin_headers, json={"username": CANARY}
    )
    assert created.status_code == 201

    with caplog.at_level(logging.INFO, logger="main"):
        resp = client.post(
            f"/admin/users/{CANARY}/keys", headers=admin_headers, json={"name": "k"}
        )
    assert resp.status_code == 201
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert CANARY not in logged, f"nom d'utilisateur journalisé : {logged}"
    assert "/admin/users/<redacted>/keys" in logged


def test_access_log_still_records_method_status_and_route(
    client, admin_headers, file_db, caplog
) -> None:
    """La rédaction ne doit pas rendre le journal inutile au diagnostic."""
    with caplog.at_level(logging.INFO, logger="main"):
        client.get(f"/admin/users/{CANARY}", headers=admin_headers)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "GET" in logged
    assert "404" in logged
    assert "/admin/users/<redacted>" in logged


# ── SEC-010 : la query string ─────────────────────────────────────────────────

@pytest.mark.parametrize(
    "target, expected",
    [
        # Le paramètre visé par SEC-010 : `GET /admin/usage?username=…`.
        (f"/admin/usage?username={CANARY}", "/admin/usage?username=<redacted>"),
        # Les paramètres structurels restent lisibles : le journal doit rester utile.
        (
            f"/admin/usage?username={CANARY}&from_date=2026-01-01&limit=50",
            "/admin/usage?username=<redacted>&from_date=2026-01-01&limit=50",
        ),
        # Chemin ET requête, tous deux porteurs du nom.
        (
            f"/admin/users/{CANARY}/keys?username={CANARY}",
            "/admin/users/<redacted>/keys?username=<redacted>",
        ),
        # Autorisation explicite : un paramètre inconnu est rédigé par défaut.
        ("/admin/usage?email=a@b.test", "/admin/usage?email=<redacted>"),
        ("/admin/models/m/unload?force=true", "/admin/models/m/unload?force=true"),
        # Paramètre sans valeur : le nom seul ne porte rien.
        ("/admin/usage?verbose", "/admin/usage?verbose"),
        # Sans requête, le comportement historique est inchangé.
        ("/v1/chat/completions", "/v1/chat/completions"),
    ],
)
def test_redact_target(target: str, expected: str) -> None:
    assert main._redact_target(target) == expected


def test_redact_query_keeps_parameter_names_but_not_their_values() -> None:
    """Le nom décrit la forme de l'appel ; la valeur seule est une donnée."""
    assert main._redact_query(f"username={CANARY}").startswith("username=")
    assert CANARY not in main._redact_query(f"username={CANARY}")


# ── SEC-010 : le journal d'accès d'uvicorn ────────────────────────────────────

def _emit_uvicorn_access(target: str) -> None:
    """Reproduit exactement l'appel d'uvicorn (h11_impl / httptools_impl)."""
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', "127.0.0.1:5000", "GET", target, "1.1", 200,
    )


def test_uvicorn_access_log_redacts_the_query_string(caplog) -> None:
    with caplog.at_level(logging.INFO, logger="uvicorn.access"):
        _emit_uvicorn_access(f"/admin/usage?username={CANARY}")
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert CANARY not in logged, f"nom d'utilisateur journalisé par uvicorn : {logged}"
    assert "/admin/usage?username=<redacted>" in logged


def test_uvicorn_access_log_redacts_the_path_segment_too(caplog) -> None:
    """
    Le contournement le plus grave : uvicorn journalise le chemin en clair.

    `_redact_path` protégeait la ligne du middleware ; celle d'uvicorn passait à
    côté, et c'est elle que journald conserve en production (`--access-log` dans
    les deux unités systemd).
    """
    with caplog.at_level(logging.INFO, logger="uvicorn.access"):
        _emit_uvicorn_access(f"/admin/users/{CANARY}/keys")
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert CANARY not in logged
    assert "/admin/users/<redacted>/keys" in logged


def test_uvicorn_access_log_still_records_method_status_and_route(caplog) -> None:
    """Contrôle positif : le filtre rédige, il ne vide pas la ligne."""
    with caplog.at_level(logging.INFO, logger="uvicorn.access"):
        _emit_uvicorn_access("/v1/chat/completions")
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "127.0.0.1:5000" in logged
    assert "GET" in logged
    assert "/v1/chat/completions" in logged
    assert "200" in logged


def test_access_redaction_filter_is_installed_at_import() -> None:
    """
    Sans ce test, retirer l'appel d'installation laisserait tous les tests
    ci-dessus verts uniquement s'ils installaient le filtre eux-mêmes. Ici on
    vérifie l'état du logger réel, tel qu'il existe après l'import de `main`.
    """
    filtres = logging.getLogger("uvicorn.access").filters
    assert any(isinstance(f, main._UvicornAccessRedactor) for f in filtres)


def test_installing_the_filter_twice_does_not_duplicate_it() -> None:
    avant = len(logging.getLogger("uvicorn.access").filters)
    main.install_access_log_redaction()
    assert len(logging.getLogger("uvicorn.access").filters) == avant


def test_access_filter_never_raises_on_an_unexpected_record() -> None:
    """Un journal ne doit pas casser une requête : le filtre tolère tout."""
    filtre = main._UvicornAccessRedactor()
    for args in ((), ("a",), ("a", "b", None, "d", 200), "pas-un-tuple", None):
        record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, "%s", None, None)
        record.args = args
        assert filtre.filter(record) is True


def test_unhandled_error_handler_redacts_path(caplog) -> None:
    """
    Le gestionnaire d'erreurs global journalise aussi le chemin : il doit rédiger.

    On l'appelle directement — provoquer une vraie 500 sur cette route
    demanderait de casser la base, ce qui n'est pas l'objet du test.
    """
    import asyncio

    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "PATCH",
        "path": f"/admin/users/{CANARY}",
        "headers": [],
        "query_string": b"",
    }
    request = Request(scope)
    with caplog.at_level(logging.ERROR, logger="main"):
        asyncio.run(main.global_exception_handler(request, RuntimeError("boom")))
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert CANARY not in logged
    assert "/admin/users/<redacted>" in logged
