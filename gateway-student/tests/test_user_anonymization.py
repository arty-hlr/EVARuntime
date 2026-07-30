"""
Tests de régression COR-002 — anonymisation des étudiants (politique DEC-001).

Défaut corrigé : `usage_log.user_id` référence `users(id)` SANS
`ON DELETE CASCADE`, alors que `PRAGMA foreign_keys = ON` est appliqué à chaque
connexion. `delete_user()` échouait donc en `IntegrityError` dès que l'étudiant
avait servi une requête, et `cli.py delete-student` remontait une exception nue.

Politique retenue : la ligne `users` est CONSERVÉE, les données personnelles sont
effacées, le compte est suspendu et les clés `llmstu-*` sont révoquées.
L'historique `usage_log` reste intact et agrégeable.

La gateway étudiante est une frontière de sécurité séparée : elle n'expose AUCUNE
route `/admin/*`, l'opération n'est donc accessible que par le CLI serveur.

Fixtures reprises de `test_migrations.py` (temp_db, base « legacy »).
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import aiosqlite
import pytest

import database as db
from config import settings


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Base SQLite jetable sur fichier (le ':memory:' des tests ne persiste pas)."""
    db_file = tmp_path / "students_anonymization.db"
    monkeypatch.setattr(settings, "db_path", db_file)
    return db_file


@pytest.fixture
def cli_runner():
    from typer.testing import CliRunner

    return CliRunner()


# Schéma d'avant COR-002 : `hourly_token_limit` déjà présent (migration 2), mais
# pas `anonymized_at`. C'est l'état d'une base étudiante déployée avant cet item.
LEGACY_SCHEMA_WITHOUT_ANONYMIZED_AT = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    email TEXT UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    is_active INTEGER NOT NULL DEFAULT 1,
    rpm_limit INTEGER NOT NULL,
    daily_token_limit INTEGER NOT NULL,
    hourly_token_limit INTEGER NOT NULL DEFAULT 0,
    concurrent_stream_limit INTEGER NOT NULL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key_hash TEXT UNIQUE NOT NULL,
    key_prefix TEXT NOT NULL,
    name TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_used TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    api_key_id INTEGER REFERENCES api_keys(id),
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    model TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER,
    status_code INTEGER,
    request_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_keys_hash ON api_keys(key_hash);
CREATE INDEX IF NOT EXISTS idx_keys_user ON api_keys(user_id);
CREATE INDEX IF NOT EXISTS idx_usage_user_time ON usage_log(user_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_usage_timestamp ON usage_log(timestamp);
"""

_FAR_FUTURE = "2099-01-01T00:00:00+00:00"


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _seed_student(
    username: str = "alice",
    email: str | None = "alice@univ-pau.fr",
    notes: str | None = "TD3, redoublante",
    n_keys: int = 2,
) -> tuple[dict, list[str]]:
    """Étudiant complet : PII, clés utilisables, usage journalisé."""
    user = await db.create_user(username=username, email=email, notes=notes)
    raw_keys: list[str] = []
    for i in range(n_keys):
        raw, key_row = await db.create_api_key(
            user["id"], f"portable-de-{username}-{i}", _FAR_FUTURE,
        )
        raw_keys.append(raw)
        await db.log_usage(user["id"], key_row["id"], "m", 100, 50, 12, 200, f"req-{username}-{i}")
    return user, raw_keys


async def _read_user_row(user_id: int) -> dict:
    async with db.get_db() as conn:
        row = await (await conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        )).fetchone()
    return dict(row)


async def _usage_rows(user_id: int) -> list[dict]:
    async with db.get_db() as conn:
        rows = await (await conn.execute(
            "SELECT * FROM usage_log WHERE user_id = ? ORDER BY id", (user_id,)
        )).fetchall()
    return [dict(r) for r in rows]


async def _key_rows(user_id: int) -> list[dict]:
    async with db.get_db() as conn:
        rows = await (await conn.execute(
            "SELECT * FROM api_keys WHERE user_id = ? ORDER BY id", (user_id,)
        )).fetchall()
    return [dict(r) for r in rows]


def _make_legacy_db(path: Path) -> None:
    """Base d'avant COR-002, avec étudiant, clé et usage réels."""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(LEGACY_SCHEMA_WITHOUT_ANONYMIZED_AT)
        conn.execute(
            """
            INSERT INTO users
                (username, email, notes, rpm_limit, daily_token_limit,
                 concurrent_stream_limit)
            VALUES ('etu-legacy', 'etu.legacy@univ-pau.fr', 'notes historiques', 5, 1000, 2)
            """
        )
        conn.execute(
            "INSERT INTO api_keys (user_id, key_hash, key_prefix, name, expires_at) "
            f"VALUES (1, 'deadbeef', 'llmstu-legacy', 'poste-legacy', '{_FAR_FUTURE}')"
        )
        conn.execute(
            "INSERT INTO usage_log (user_id, model, total_tokens) VALUES (1, 'm', 42)"
        )
        conn.commit()
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        columns = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
        assert "anonymized_at" not in columns
    finally:
        conn.close()


def _columns(path: Path, table: str) -> dict[str, str]:
    conn = sqlite3.connect(path)
    try:
        return {row[1]: row[2] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def _foreign_keys(path: Path, table: str) -> list[tuple]:
    conn = sqlite3.connect(path)
    try:
        return [
            (row[2], row[3], row[4], row[5], row[6])
            for row in conn.execute(f"PRAGMA foreign_key_list({table})")
        ]
    finally:
        conn.close()


# ── 1. Reproduction du défaut ─────────────────────────────────────────────────

@pytest.mark.anyio
async def test_raw_delete_still_violates_foreign_key(temp_db) -> None:
    """
    Reproduction du défaut d'origine : un `DELETE FROM users` échoue toujours en
    `IntegrityError` dès qu'une ligne `usage_log` référence l'étudiant. La
    contrainte n'a PAS été relâchée en `ON DELETE CASCADE`.
    """
    await db.init_db()
    user, _ = await _seed_student()

    with pytest.raises(aiosqlite.IntegrityError, match="FOREIGN KEY"):
        async with db.get_db() as conn:
            await conn.execute("DELETE FROM users WHERE id = ?", (user["id"],))
            await conn.commit()


@pytest.mark.anyio
async def test_delete_without_usage_was_the_only_case_that_worked(temp_db) -> None:
    """Sans usage le DELETE passait — d'où un défaut invisible en test naïf."""
    await db.init_db()
    user = await db.create_user(username="sans-usage")
    async with db.get_db() as conn:
        cursor = await conn.execute("DELETE FROM users WHERE id = ?", (user["id"],))
        await conn.commit()
    assert cursor.rowcount == 1


@pytest.mark.anyio
async def test_anonymize_succeeds_where_delete_failed(temp_db) -> None:
    """Le nouveau chemin réussit exactement là où l'ancien levait IntegrityError."""
    await db.init_db()
    user, _ = await _seed_student()

    result = await db.anonymize_user(user["id"])

    assert result is not None
    assert result["already_anonymized"] is False
    assert result["anonymized_at"]


@pytest.mark.anyio
async def test_legacy_delete_user_helper_is_gone(temp_db) -> None:
    """
    `delete_user()` a été retiré : sa docstring promettait une cascade qui
    n'existait pas, et l'appeler produisait systématiquement une IntegrityError.
    """
    assert not hasattr(db, "delete_user")
    assert hasattr(db, "anonymize_user")


# ── 2. Effacement effectif des données personnelles ───────────────────────────

@pytest.mark.anyio
async def test_personal_data_is_actually_erased_in_the_row(temp_db) -> None:
    """Vérification sur la ligne relue, pas sur le code de retour."""
    await db.init_db()
    user, _ = await _seed_student(
        username="carol", email="carol@univ-pau.fr", notes="adresse personnelle",
    )

    await db.anonymize_user(user["id"])

    row = await _read_user_row(user["id"])
    assert row["username"] == f"{db.ANONYMIZED_USERNAME_PREFIX}{user['id']}"
    assert "carol" not in row["username"]
    assert row["email"] is None
    assert row["notes"] is None
    assert row["is_active"] == 0
    assert row["anonymized_at"] is not None
    assert row["id"] == user["id"]
    assert row["created_at"] == user["created_at"]
    # Les quotas propres au student restent en place (non identifiants).
    assert row["daily_token_limit"] == user["daily_token_limit"]
    assert row["hourly_token_limit"] == user["hourly_token_limit"]
    assert row["concurrent_stream_limit"] == user["concurrent_stream_limit"]


@pytest.mark.anyio
async def test_api_key_free_text_name_is_erased(temp_db) -> None:
    """`api_keys.name` est un champ libre : il peut porter un nom de personne."""
    await db.init_db()
    user, _ = await _seed_student(username="dave")

    await db.anonymize_user(user["id"])

    keys = await _key_rows(user["id"])
    assert keys
    assert all(k["name"] is None for k in keys)
    assert all(k["key_prefix"] for k in keys)


@pytest.mark.anyio
async def test_anonymized_student_is_distinguishable_from_suspended(temp_db) -> None:
    """Un compte suspendu et un compte anonymisé ne doivent pas se confondre."""
    await db.init_db()
    suspended = await db.create_user(username="eve")
    await db.set_user_active(suspended["id"], False)
    anonymized, _ = await _seed_student(username="frank")
    await db.anonymize_user(anonymized["id"])

    suspended_row = await _read_user_row(suspended["id"])
    anonymized_row = await _read_user_row(anonymized["id"])

    assert suspended_row["is_active"] == 0 and anonymized_row["is_active"] == 0
    assert suspended_row["anonymized_at"] is None
    assert anonymized_row["anonymized_at"] is not None
    assert db.is_anonymized(anonymized_row) is True
    assert db.is_anonymized(suspended_row) is False


# ── 3. Révocation des clés ────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_all_keys_are_revoked_and_unusable_for_auth(temp_db) -> None:
    """Aucune clé llmstu-* ne doit rester utilisable pour s'authentifier."""
    await db.init_db()
    user, raw_keys = await _seed_student(username="grace", n_keys=3)

    for raw in raw_keys:
        assert await db.lookup_key(raw) is not None

    result = await db.anonymize_user(user["id"])
    assert result["keys_revoked"] == 3
    assert result["keys_total"] == 3

    assert all(k["is_active"] == 0 for k in await _key_rows(user["id"]))
    for raw in raw_keys:
        assert await db.lookup_key(raw) is None


@pytest.mark.anyio
async def test_already_revoked_key_is_not_double_counted(temp_db) -> None:
    """`keys_revoked` compte les clés effectivement révoquées par cet appel."""
    await db.init_db()
    user, _ = await _seed_student(username="heidi", n_keys=2)
    keys = await _key_rows(user["id"])
    await db.revoke_key(keys[0]["key_prefix"])

    result = await db.anonymize_user(user["id"])

    assert result["keys_revoked"] == 1
    assert result["keys_total"] == 2
    assert all(k["is_active"] == 0 for k in await _key_rows(user["id"]))


# ── 4. Préservation de l'historique d'usage ───────────────────────────────────

@pytest.mark.anyio
async def test_usage_log_rows_and_aggregates_are_unchanged(temp_db) -> None:
    """L'historique agrégé survit à l'effacement — tout l'intérêt de DEC-001."""
    await db.init_db()
    user, _ = await _seed_student(username="ivan", n_keys=2)

    before_rows = await _usage_rows(user["id"])
    before_today = await db.tokens_used_today(user["id"])
    assert before_today == 300  # 2 × (100 + 50)

    await db.anonymize_user(user["id"])

    assert await _usage_rows(user["id"]) == before_rows
    assert await db.tokens_used_today(user["id"]) == before_today


@pytest.mark.anyio
async def test_usage_foreign_key_constraint_is_left_unchanged(temp_db) -> None:
    """
    La politique DEC-001 conserve la ligne `users` : la clé étrangère n'est jamais
    violée, donc `usage_log` n'a PAS été recréée et garde sa contrainte d'origine.
    """
    await db.init_db()
    users_fk = [fk for fk in _foreign_keys(temp_db, "usage_log") if fk[0] == "users"]
    assert users_fk, "usage_log doit toujours référencer users(id)"
    assert users_fk[0][1] == "user_id"
    assert users_fk[0][4] == "NO ACTION"


# ── 5. Idempotence ────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_anonymizing_twice_preserves_initial_timestamp(temp_db) -> None:
    """Deux anonymisations : aucune erreur, horodatage initial préservé."""
    await db.init_db()
    user, _ = await _seed_student(username="judy")

    first = await db.anonymize_user(user["id"])
    assert first["already_anonymized"] is False

    second = await db.anonymize_user(user["id"])
    assert second["already_anonymized"] is True
    assert second["anonymized_at"] == first["anonymized_at"]
    assert second["username"] == first["username"]
    assert second["keys_revoked"] == 0


# ── 6. Unicité et réutilisation du nom ────────────────────────────────────────

@pytest.mark.anyio
async def test_two_students_anonymized_without_unique_collision(temp_db) -> None:
    """Deux anonymisations successives : aucun conflit UNIQUE sur username/email."""
    await db.init_db()
    first, _ = await _seed_student(username="u1", email="u1@x")
    second, _ = await _seed_student(username="u2", email="u2@x")

    r1 = await db.anonymize_user(first["id"])
    r2 = await db.anonymize_user(second["id"])

    assert r1["username"] != r2["username"]
    rows = [await _read_user_row(first["id"]), await _read_user_row(second["id"])]
    # SQLite autorise plusieurs NULL dans une contrainte UNIQUE — vérifié, pas supposé.
    assert all(r["email"] is None for r in rows)
    assert len({r["username"] for r in rows}) == 2


@pytest.mark.anyio
async def test_many_students_anonymized_keep_distinct_pseudonyms(temp_db) -> None:
    """Le pseudonyme dérive de l'id (AUTOINCREMENT) : jamais deux fois le même."""
    await db.init_db()
    ids = []
    for i in range(5):
        user, _ = await _seed_student(username=f"bulk{i}", email=f"bulk{i}@x", n_keys=1)
        ids.append(user["id"])
    for uid in ids:
        assert await db.anonymize_user(uid) is not None

    async with db.get_db() as conn:
        rows = await (await conn.execute(
            "SELECT username FROM users WHERE anonymized_at IS NOT NULL"
        )).fetchall()
    names = [r["username"] for r in rows]
    assert len(names) == 5 and len(set(names)) == 5


@pytest.mark.anyio
async def test_recreating_student_with_former_username_succeeds(temp_db) -> None:
    """Cas réel : un étudiant part, est anonymisé, puis revient sous le même nom."""
    await db.init_db()
    original, _ = await _seed_student(username="returning", email="returning@univ-pau.fr")
    await db.anonymize_user(original["id"])

    recreated = await db.create_user(username="returning", email="returning@univ-pau.fr")

    assert recreated["id"] != original["id"]
    assert recreated["is_active"] == 1
    assert recreated["anonymized_at"] is None
    assert (await _read_user_row(original["id"]))["anonymized_at"] is not None
    assert len(await _usage_rows(original["id"])) == 2
    assert await _usage_rows(recreated["id"]) == []


# ── 7. Étudiant inexistant ────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_anonymize_unknown_student_returns_none(temp_db) -> None:
    """Erreur exploitable par le caller, pas d'exception non gérée."""
    await db.init_db()
    assert await db.anonymize_user(999_999) is None


# ── 8. Rapports et liste d'étudiants ──────────────────────────────────────────

@pytest.mark.anyio
async def test_usage_report_still_joins_and_shows_pseudonym(temp_db) -> None:
    """
    Décision : les rapports d'usage CONTINUENT d'inclure l'étudiant anonymisé,
    sous son pseudonyme, avec un e-mail nul. La jointure n'est pas cassée.
    """
    await db.init_db()
    user, _ = await _seed_student(username="lena", n_keys=2)

    before = await db.get_usage_report(days=7)
    assert len(before) == 1
    assert before[0]["username"] == "lena"
    assert before[0]["request_count"] == 2

    await db.anonymize_user(user["id"])

    after = await db.get_usage_report(days=7)
    assert len(after) == 1
    assert after[0]["username"] == f"{db.ANONYMIZED_USERNAME_PREFIX}{user['id']}"
    assert after[0]["email"] is None
    # Les totaux de facturation sont inchangés.
    for field in ("request_count", "prompt_tokens", "completion_tokens", "total_tokens"):
        assert after[0][field] == before[0][field]


@pytest.mark.anyio
async def test_anonymized_student_stays_listed_but_not_counted_as_active(temp_db) -> None:
    """
    Décision : un étudiant anonymisé reste VISIBLE dans `list_users()` (comme un
    compte suspendu) et n'est JAMAIS compté dans `total_active_users`.
    """
    await db.init_db()
    await db.create_user(username="nina")
    anonymized, _ = await _seed_student(username="oscar")
    await db.anonymize_user(anonymized["id"])

    users = await db.list_users()
    pseudonym = f"{db.ANONYMIZED_USERNAME_PREFIX}{anonymized['id']}"
    assert {u["username"] for u in users} == {"nina", pseudonym}
    listed = next(u for u in users if u["username"] == pseudonym)
    assert listed["is_active"] == 0
    assert listed["email"] is None
    # Aucune clé active ne subsiste sur le compte anonymisé.
    assert (listed["key_count"] or 0) == 0

    stats = await db.get_global_stats()
    assert stats["total_active_users"] == 1  # seule `nina` est active


@pytest.mark.anyio
async def test_keys_overview_hides_erased_key_names(temp_db) -> None:
    """Les vues admin sur les clés ne doivent plus exposer de nom de personne."""
    await db.init_db()
    user, _ = await _seed_student(username="paul-secret")
    await db.anonymize_user(user["id"])

    overview = await db.get_all_keys_overview()
    assert overview
    for row in overview:
        assert row["name"] is None
        assert "paul-secret" not in (row["username"] or "")
        assert row["email"] is None

    # Une clé révoquée n'apparaît plus dans les expirations à surveiller.
    assert await db.get_expiring_keys(within_days=36_500) == []


# ── 9. Aucune donnée personnelle dans les journaux ────────────────────────────

@pytest.mark.anyio
async def test_anonymization_logs_no_personal_data(temp_db, caplog) -> None:
    """
    L'opération ne doit pas recréer la donnée ailleurs : ni le nom, ni l'e-mail,
    ni les notes effacées n'apparaissent dans les journaux (§14 / CLAUDE.md).
    """
    await db.init_db()
    user, _ = await _seed_student(
        username="secret-name",
        email="secret.mail@univ-pau.fr",
        notes="note tres personnelle",
    )

    with caplog.at_level(logging.DEBUG):
        await db.anonymize_user(user["id"])

    captured = caplog.text
    assert "secret-name" not in captured
    assert "secret.mail@univ-pau.fr" not in captured
    assert "note tres personnelle" not in captured


# ── 10. Frontière de sécurité : aucune route admin côté student ───────────────

def _iter_route_paths(router):
    """
    Énumère les chemins de toutes les routes, en descendant récursivement.

    Ce parcours ne peut pas se contenter de lire `app.routes` : depuis
    FastAPI 0.141, `include_router()` n'aplatit plus les routes, il dépose un
    `_IncludedRouter` qui garde une référence via `original_router`. Une lecture
    directe deviendrait donc AVEUGLE aux routes des routeurs inclus.

    C'est critique ici : ce test asserte une *absence*. S'il devenait aveugle,
    il continuerait de passer pendant que des routes `/admin/*` seraient
    réellement exposées — un contrôle de sécurité inerte et silencieux. La
    gateway étudiante n'utilise aucun routeur inclus aujourd'hui, mais le test
    doit rester valide si cela change.
    """
    for route in getattr(router, "routes", []) or []:
        original = getattr(route, "original_router", None)
        if original is not None:
            yield from _iter_route_paths(original)
            continue
        path = getattr(route, "path", None)
        if path is not None:
            yield path


def test_student_gateway_exposes_no_admin_route() -> None:
    """
    L'anonymisation est une opération serveur (CLI). La gateway étudiante ne doit
    exposer AUCUNE route `/admin/*` ni de gestion de compte (règle CLAUDE.md).
    """
    import main

    paths = set(_iter_route_paths(main.app))

    # Témoin positif : prouve que le parcours voit réellement quelque chose.
    # Sans lui, une énumération cassée rendrait les assertions d'absence
    # ci-dessous trivialement vraies.
    assert "/v1/chat/completions" in paths

    assert not any(path.startswith("/admin") for path in paths)
    assert not any("users" in path for path in paths)
    assert not any("anonymize" in path for path in paths)


# ── 11. Commandes CLI ─────────────────────────────────────────────────────────

def test_cli_anonymize_student_with_keys_and_usage(cli_runner, temp_db) -> None:
    """Critère d'acceptation via le CLI : clés + usage, anonymisation réussie."""
    import asyncio

    import cli

    asyncio.run(db.init_db())
    user, raw_keys = asyncio.run(_seed_student(username="cli-student"))

    result = cli_runner.invoke(cli.app, ["anonymize-student", "cli-student", "--yes"])

    assert result.exit_code == 0, result.output
    assert "anonymisé" in result.output
    assert "Effacé" in result.output
    assert "Conservé" in result.output

    row = asyncio.run(_read_user_row(user["id"]))
    assert row["email"] is None and row["notes"] is None
    assert row["anonymized_at"] is not None
    assert asyncio.run(db.lookup_key(raw_keys[0])) is None
    assert len(asyncio.run(_usage_rows(user["id"]))) == 2


def test_cli_delete_student_alias_still_works_and_anonymizes(cli_runner, temp_db) -> None:
    """
    Compatibilité des scripts opérateur : `delete-student` reste invocable avec
    les mêmes arguments, mais anonymise et le dit explicitement.
    """
    import asyncio

    import cli

    asyncio.run(db.init_db())
    user, _ = asyncio.run(_seed_student(username="cli-legacy-alias"))

    result = cli_runner.invoke(cli.app, ["delete-student", "cli-legacy-alias", "--yes"])

    assert result.exit_code == 0, result.output
    assert "alias obsolète" in result.output
    assert "anonymize-student" in result.output
    row = asyncio.run(_read_user_row(user["id"]))
    assert row["anonymized_at"] is not None
    assert row["email"] is None


def test_cli_anonymize_unknown_student_exits_with_clear_message(cli_runner, temp_db) -> None:
    """Étudiant inexistant : message clair et code de sortie non nul."""
    import asyncio

    import cli

    asyncio.run(db.init_db())

    result = cli_runner.invoke(cli.app, ["anonymize-student", "ghost", "--yes"])

    assert result.exit_code != 0
    assert "introuvable" in result.output


def test_cli_anonymize_twice_reports_already_anonymized(cli_runner, temp_db) -> None:
    """Second passage : signalé explicitement, sans erreur."""
    import asyncio

    import cli

    asyncio.run(db.init_db())
    user, _ = asyncio.run(_seed_student(username="cli-twice"))
    pseudonym = f"{db.ANONYMIZED_USERNAME_PREFIX}{user['id']}"

    assert cli_runner.invoke(
        cli.app, ["anonymize-student", "cli-twice", "--yes"]
    ).exit_code == 0
    second = cli_runner.invoke(cli.app, ["anonymize-student", pseudonym, "--yes"])

    assert second.exit_code == 0
    assert "Déjà anonymisé" in second.output


def test_cli_anonymize_aborts_without_confirmation(cli_runner, temp_db) -> None:
    """Sans --yes, un refus interactif laisse le compte intact."""
    import asyncio

    import cli

    asyncio.run(db.init_db())
    user, _ = asyncio.run(_seed_student(username="cli-abort"))

    result = cli_runner.invoke(cli.app, ["anonymize-student", "cli-abort"], input="n\n")

    assert result.exit_code == 0
    assert "Annulé" in result.output
    row = asyncio.run(_read_user_row(user["id"]))
    assert row["username"] == "cli-abort"
    assert row["anonymized_at"] is None


# ── 12. Migration depuis une base legacy ──────────────────────────────────────

@pytest.mark.anyio
async def test_legacy_db_is_migrated_and_data_preserved(temp_db) -> None:
    """Base d'avant COR-002 : colonne ajoutée, données existantes intactes."""
    _make_legacy_db(temp_db)

    await db.init_db()

    assert "anonymized_at" in _columns(temp_db, "users")
    user = await db.get_user_by_username("etu-legacy")
    assert user is not None
    assert user["email"] == "etu.legacy@univ-pau.fr"
    assert user["notes"] == "notes historiques"
    assert user["rpm_limit"] == 5
    assert user["anonymized_at"] is None
    assert len(await _usage_rows(user["id"])) == 1


@pytest.mark.anyio
async def test_anonymization_works_after_migration_from_legacy(temp_db) -> None:
    """Critère d'acceptation sur une base réelle migrée, clés et usage inclus."""
    _make_legacy_db(temp_db)
    await db.init_db()

    user = await db.get_user_by_username("etu-legacy")
    result = await db.anonymize_user(user["id"])

    assert result["already_anonymized"] is False
    assert result["keys_revoked"] == 1
    row = await _read_user_row(user["id"])
    assert row["email"] is None and row["notes"] is None and row["is_active"] == 0
    assert row["username"] == f"{db.ANONYMIZED_USERNAME_PREFIX}{user['id']}"
    async with db.get_db() as conn:
        total = await (await conn.execute(
            "SELECT COALESCE(SUM(total_tokens), 0) AS t FROM usage_log"
        )).fetchone()
    assert total["t"] == 42


def test_migration_is_declared_at_version_three() -> None:
    """La migration COR-002 est la version 3 de la gateway étudiante."""
    versions = {m.version: m for m in db.MIGRATIONS}
    assert db.SCHEMA_VERSION == 3
    assert "anonymized_at" in versions[3].description
    # Ajoutée en fin de tuple : les migrations livrées ne sont pas réécrites.
    assert db.MIGRATIONS[-1].version == 3
    assert "hourly_token_limit" in versions[2].description


@pytest.mark.anyio
async def test_fresh_and_migrated_schemas_are_identical(tmp_path, monkeypatch) -> None:
    """
    Cohérence `SCHEMA` / migrations : une base neuve et une base migrée depuis
    l'ancienne version doivent aboutir au MÊME schéma.
    """
    fresh = tmp_path / "fresh.db"
    migrated = tmp_path / "migrated.db"

    monkeypatch.setattr(settings, "db_path", fresh)
    await db.init_db()

    _make_legacy_db(migrated)
    monkeypatch.setattr(settings, "db_path", migrated)
    await db.init_db()

    for table in ("users", "api_keys", "usage_log"):
        assert _columns(fresh, table) == _columns(migrated, table), table
        assert _foreign_keys(fresh, table) == _foreign_keys(migrated, table), table

    conn_fresh = sqlite3.connect(fresh)
    conn_migrated = sqlite3.connect(migrated)
    try:
        query = (
            "SELECT name FROM sqlite_master WHERE type = 'index' "
            "AND name LIKE 'idx_%' ORDER BY name"
        )
        assert (
            [r[0] for r in conn_fresh.execute(query)]
            == [r[0] for r in conn_migrated.execute(query)]
        )
        assert (
            conn_fresh.execute("PRAGMA user_version").fetchone()[0]
            == conn_migrated.execute("PRAGMA user_version").fetchone()[0]
            == db.SCHEMA_VERSION
        )
    finally:
        conn_fresh.close()
        conn_migrated.close()
