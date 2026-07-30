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
