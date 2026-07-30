"""
Tests du moteur de migration SQLite versionné de la gateway étudiante (OPS-006).

Couvre : base neuve, base « legacy » en `user_version = 0` avec et sans la
colonne `hourly_token_limit`, reprise depuis `user_version = 1`, idempotence de
`init_db()`, refus d'une base plus récente que le code, échec transactionnel
sans corruption, politique de sauvegarde préalable, et recréation de table
(motif attendu par COR-002).

Base temporaire (tmp_path) + monkeypatch de settings.db_path, comme
`test_database_hardening.py`.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import database as db
from config import settings


# Schéma tel qu'il existait AVANT la colonne `hourly_token_limit` : c'est l'état
# d'une base étudiante déployée puis jamais migrée autrement que par l'ancien
# `ALTER TABLE` rejoué au démarrage.
LEGACY_SCHEMA_WITHOUT_HOURLY = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    email TEXT UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    is_active INTEGER NOT NULL DEFAULT 1,
    rpm_limit INTEGER NOT NULL,
    daily_token_limit INTEGER NOT NULL,
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
CREATE INDEX IF NOT EXISTS idx_usage_user_time ON usage_log(user_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_usage_timestamp ON usage_log(timestamp);
"""


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Pointe settings.db_path vers un fichier SQLite jetable (schéma non créé)."""
    db_file = tmp_path / "students_test.db"
    monkeypatch.setattr(settings, "db_path", db_file)
    return db_file


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_legacy_db(path: Path, *, with_hourly_column: bool) -> None:
    """
    Fabrique une base d'avant OPS-006 (`user_version = 0`), avec ou sans la
    colonne `hourly_token_limit` déjà ajoutée par l'ancien mécanisme.
    """
    conn = sqlite3.connect(path)
    try:
        conn.executescript(LEGACY_SCHEMA_WITHOUT_HOURLY)
        if with_hourly_column:
            conn.execute(
                "ALTER TABLE users ADD COLUMN hourly_token_limit INTEGER NOT NULL DEFAULT 0"
            )
        conn.execute(
            """
            INSERT INTO users
                (username, email, rpm_limit, daily_token_limit, concurrent_stream_limit)
            VALUES ('etu-legacy', 'etu@x', 5, 1000, 2)
            """
        )
        conn.execute(
            "INSERT INTO usage_log (user_id, model, total_tokens) VALUES (1, 'm', 42)"
        )
        conn.commit()
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    finally:
        conn.close()


def _user_version(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def _set_user_version(path: Path, version: int) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()
    finally:
        conn.close()


def _columns(path: Path, table: str) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def _table_names(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    finally:
        conn.close()


def _backups(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f"{path.name}.pre-migration.*"))


# ── Déclaration des migrations ────────────────────────────────────────────────

def test_migrations_are_ordered_unique_and_start_at_one() -> None:
    """Les versions déclarées sont uniques, croissantes et démarrent à 1."""
    versions = [m.version for m in db.MIGRATIONS]
    assert versions == sorted(versions)
    assert len(set(versions)) == len(versions)
    assert versions[0] == 1
    assert db.SCHEMA_VERSION == versions[-1]


# ── Base neuve ────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_new_db_reaches_target_version_with_full_schema(temp_db) -> None:
    """Base neuve : schéma complet et user_version = version max connue."""
    await db.init_db()

    assert _user_version(temp_db) == db.SCHEMA_VERSION
    assert {"users", "api_keys", "usage_log"} <= _table_names(temp_db)
    assert "hourly_token_limit" in _columns(temp_db, "users")

    async with db.get_db() as conn:
        rows = await (await conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name LIKE 'idx_%'"
        )).fetchall()
    assert {"idx_keys_hash", "idx_keys_user", "idx_usage_user_time", "idx_usage_timestamp"} <= {
        r["name"] for r in rows
    }


@pytest.mark.anyio
async def test_new_db_is_writable_after_migration(temp_db) -> None:
    """Le schéma migré est fonctionnel (insertion réelle via les helpers)."""
    await db.init_db()
    user = await db.create_user(username="etu1", hourly_token_limit=500)
    assert user["hourly_token_limit"] == 500
    await db.log_usage(user["id"], None, "m", 10, 5, 12, 200, "req-1")
    assert await db.tokens_used_today(user["id"]) == 15


# ── Bases legacy (user_version = 0) ───────────────────────────────────────────

@pytest.mark.anyio
async def test_legacy_db_without_hourly_column_is_migrated(temp_db) -> None:
    """Base legacy sans la colonne : elle est ajoutée, données préservées."""
    _make_legacy_db(temp_db, with_hourly_column=False)
    assert "hourly_token_limit" not in _columns(temp_db, "users")

    await db.init_db()

    assert _user_version(temp_db) == db.SCHEMA_VERSION
    assert "hourly_token_limit" in _columns(temp_db, "users")

    user = await db.get_user_by_username("etu-legacy")
    assert user is not None
    assert user["email"] == "etu@x"
    assert user["daily_token_limit"] == 1000
    # La colonne ajoutée porte son DEFAULT sur les lignes existantes.
    assert user["hourly_token_limit"] == 0

    async with db.get_db() as conn:
        row = await (await conn.execute(
            "SELECT COALESCE(SUM(total_tokens), 0) AS t FROM usage_log"
        )).fetchone()
        assert row["t"] == 42


@pytest.mark.anyio
async def test_legacy_db_with_hourly_column_already_present_migrates(temp_db) -> None:
    """
    Cas critique de l'absorption : une base peut avoir DÉJÀ la colonne
    `hourly_token_limit` (ajoutée par l'ancien ALTER rejoué) tout en restant en
    `user_version = 0`. La migration doit détecter l'état réel, pas le supposer.
    """
    _make_legacy_db(temp_db, with_hourly_column=True)
    assert "hourly_token_limit" in _columns(temp_db, "users")

    await db.init_db()

    assert _user_version(temp_db) == db.SCHEMA_VERSION
    assert "hourly_token_limit" in _columns(temp_db, "users")

    user = await db.get_user_by_username("etu-legacy")
    assert user is not None
    assert user["rpm_limit"] == 5


@pytest.mark.anyio
async def test_migration_resumes_from_intermediate_version(temp_db) -> None:
    """
    Reprise depuis `user_version = 1` : seule la migration 2 est appliquée.
    Simule une base estampillée par une version antérieure du code.
    """
    _make_legacy_db(temp_db, with_hourly_column=False)
    _set_user_version(temp_db, 1)

    await db.init_db()

    assert _user_version(temp_db) == db.SCHEMA_VERSION
    assert "hourly_token_limit" in _columns(temp_db, "users")
    assert ".pre-migration.v1." in _backups(temp_db)[0].name


@pytest.mark.anyio
async def test_foreign_keys_are_on_after_migration(temp_db) -> None:
    """Les FK sont désactivées PENDANT la migration, jamais après."""
    _make_legacy_db(temp_db, with_hourly_column=False)
    await db.init_db()
    async with db.get_db() as conn:
        row = await (await conn.execute("PRAGMA foreign_keys")).fetchone()
        assert int(row[0]) == 1


# ── Idempotence ───────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_init_db_twice_migrates_once(temp_db) -> None:
    """Deux appels consécutifs : pas d'erreur, une seule migration effective."""
    _make_legacy_db(temp_db, with_hourly_column=False)

    await db.init_db()
    assert len(_backups(temp_db)) == 1

    await db.init_db()
    assert _user_version(temp_db) == db.SCHEMA_VERSION
    assert len(_backups(temp_db)) == 1


@pytest.mark.anyio
async def test_init_db_repeated_on_new_db_is_stable(temp_db) -> None:
    """init_db() répété sur une base neuve reste à la version cible.

    Le CLI étudiant appelle `init_db()` à chaque commande : cet appel doit être
    gratuit et ne produire aucune sauvegarde.
    """
    await db.init_db()
    await db.init_db()
    await db.init_db()
    assert _user_version(temp_db) == db.SCHEMA_VERSION
    assert _backups(temp_db) == []


@pytest.mark.anyio
async def test_migration_already_applied_is_skipped_under_lock(temp_db) -> None:
    """
    Relecture de user_version sous verrou : une migration déjà appliquée par un
    autre processus n'est pas rejouée (cas service + CLI démarrés en parallèle).
    """
    await db.init_db()

    executed: list[str] = []

    async def _apply(conn) -> None:  # pragma: no cover — ne doit pas être appelé
        executed.append("python")

    already_applied = db.Migration(
        version=db.SCHEMA_VERSION,
        description="migration deja appliquee",
        statements=("CREATE TABLE canary_race (x INTEGER)",),
        apply=_apply,
    )

    async with db.get_db() as conn:
        await db._apply_migration(conn, already_applied)

    assert executed == []
    assert "canary_race" not in _table_names(temp_db)
    assert _user_version(temp_db) == db.SCHEMA_VERSION


# ── Sauvegarde préalable ──────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_backup_created_when_migration_applies(temp_db) -> None:
    """Migration réelle sur base existante → sauvegarde nommée avec la version source."""
    _make_legacy_db(temp_db, with_hourly_column=False)

    await db.init_db()

    backups = _backups(temp_db)
    assert len(backups) == 1
    backup = backups[0]
    assert ".pre-migration.v0." in backup.name

    conn = sqlite3.connect(backup)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        # Image d'AVANT migration : la colonne n'y est pas encore.
        columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
        assert "hourly_token_limit" not in columns
    finally:
        conn.close()

    backup_mode = backup.stat().st_mode & 0o777
    db_mode = temp_db.stat().st_mode & 0o777
    assert backup_mode & 0o077 == 0
    assert backup_mode & ~db_mode == 0


@pytest.mark.anyio
async def test_no_backup_when_db_is_up_to_date(temp_db) -> None:
    """Aucune copie inutile : ni pour une base neuve, ni pour une base à jour."""
    await db.init_db()
    assert _backups(temp_db) == []

    await db.init_db()
    assert _backups(temp_db) == []


# ── Base plus récente que le code ─────────────────────────────────────────────

@pytest.mark.anyio
async def test_db_newer_than_code_is_refused(temp_db) -> None:
    """user_version > version max connue → erreur explicite, base intacte."""
    await db.init_db()
    future = db.SCHEMA_VERSION + 5
    _set_user_version(temp_db, future)

    with pytest.raises(db.MigrationError) as excinfo:
        await db.init_db()

    message = str(excinfo.value)
    assert str(future) in message
    assert "rollback applicatif" in message
    assert _user_version(temp_db) == future
    assert _backups(temp_db) == []


# ── Échec de migration ────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_failing_migration_rolls_back_and_keeps_version(temp_db, monkeypatch) -> None:
    """Migration qui lève → transaction annulée, user_version inchangée."""
    await db.init_db()
    before = _user_version(temp_db)

    async def _boom(conn) -> None:
        # Écriture volontaire AVANT l'échec : elle doit disparaître au rollback.
        await conn.execute("CREATE TABLE canary_python (x INTEGER)")
        raise RuntimeError("échec simulé de migration")

    failing = db.Migration(
        version=before + 1,
        description="migration factice qui echoue",
        statements=("CREATE TABLE canary_sql (x INTEGER)",),
        apply=_boom,
    )
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS + (failing,))

    with pytest.raises(db.MigrationError) as excinfo:
        await db.init_db()
    assert "échec simulé de migration" in str(excinfo.value)

    assert _user_version(temp_db) == before
    tables = _table_names(temp_db)
    assert "canary_sql" not in tables
    assert "canary_python" not in tables

    conn = sqlite3.connect(temp_db)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()

    monkeypatch.setattr(db, "MIGRATIONS", tuple(m for m in db.MIGRATIONS if m is not failing))
    await db.init_db()
    assert _user_version(temp_db) == db.SCHEMA_VERSION


@pytest.mark.anyio
async def test_failing_migration_keeps_previous_migrations_applied(temp_db, monkeypatch) -> None:
    """Une migration en échec n'annule pas celles qui ont déjà été validées."""
    _make_legacy_db(temp_db, with_hourly_column=False)

    async def _boom(conn) -> None:
        raise RuntimeError("derniere etape en echec")

    failing = db.Migration(
        version=db.SCHEMA_VERSION + 1,
        description="migration factice finale",
        apply=_boom,
    )
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS + (failing,))

    with pytest.raises(db.MigrationError):
        await db.init_db()

    # Les migrations 1 et 2 sont validées, la troisième non.
    assert _user_version(temp_db) == db.SCHEMA_VERSION
    assert "hourly_token_limit" in _columns(temp_db, "users")


# ── Recréation de table (ergonomie attendue par COR-002) ──────────────────────

@pytest.mark.anyio
async def test_rebuild_table_migration_changes_foreign_key(temp_db, monkeypatch) -> None:
    """
    Une migration peut recréer `usage_log` pour changer sa contrainte de clé
    étrangère : données préservées, index recréés, nouvelle contrainte active.

    C'est exactement le cas d'usage de COR-002 ; ce test verrouille l'ergonomie.
    """
    await db.init_db()
    user = await db.create_user(username="etu1")
    await db.log_usage(user["id"], None, "m", 10, 5, 12, 200, "req-1")

    async def _apply(conn) -> None:
        await db._rebuild_table(
            conn,
            table="usage_log",
            create_new_sql="""
                CREATE TABLE usage_log__new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    api_key_id INTEGER REFERENCES api_keys(id) ON DELETE SET NULL,
                    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                    model TEXT NOT NULL,
                    prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    duration_ms INTEGER,
                    status_code INTEGER,
                    request_id TEXT
                )
            """,
            copy_columns=(
                "id", "user_id", "api_key_id", "timestamp", "model",
                "prompt_tokens", "completion_tokens", "total_tokens",
                "duration_ms", "status_code", "request_id",
            ),
            index_statements=(
                "CREATE INDEX idx_usage_user_time ON usage_log(user_id, timestamp)",
                "CREATE INDEX idx_usage_timestamp ON usage_log(timestamp)",
            ),
        )

    migration = db.Migration(
        version=db.SCHEMA_VERSION + 1,
        description="recreation de usage_log avec ON DELETE CASCADE",
        apply=_apply,
        check_foreign_keys=True,
    )
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS + (migration,))

    await db.init_db()
    assert _user_version(temp_db) == migration.version

    async with db.get_db() as conn:
        row = await (await conn.execute("SELECT COUNT(*) AS n FROM usage_log")).fetchone()
        assert row["n"] == 1

        indexes = await (await conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'usage_log'"
        )).fetchall()
        assert {"idx_usage_user_time", "idx_usage_timestamp"} <= {r["name"] for r in indexes}

        await conn.execute("DELETE FROM users WHERE id = ?", (user["id"],))
        await conn.commit()
        row = await (await conn.execute("SELECT COUNT(*) AS n FROM usage_log")).fetchone()
        assert row["n"] == 0
