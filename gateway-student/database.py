"""
Couche base de données de la gateway étudiante — SQLite WAL, clés stockées en
hash SHA-256 uniquement.

Le schéma est versionné par `PRAGMA user_version` et amené à niveau par le
moteur de migration de ce module (section « Migrations versionnées ») : voir
`docs/architecture.md` pour la procédure d'ajout d'une migration et le
comportement opérateur en cas d'échec.

Ce moteur est volontairement dupliqué depuis `gateway/database.py` : les deux
composants sont des frontières de sécurité indépendantes et ne partagent aucun
code. Toute évolution du mécanisme doit être reportée des deux côtés.
"""
from __future__ import annotations

import hashlib
import logging
import os
import secrets
import stat
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator, Awaitable, Callable, Sequence

import aiosqlite

from config import settings

logger = logging.getLogger(__name__)


SCHEMA = """
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
    notes TEXT,
    -- Horodatage d'anonymisation (COR-002 / DEC-001). NULL = étudiant ordinaire.
    -- Non NULL = données personnelles effacées définitivement. Distingue un
    -- compte anonymisé d'un compte simplement suspendu, et rend l'opération
    -- idempotente. Déclarée en dernière position, la place qu'un
    -- `ALTER TABLE ADD COLUMN` lui donne sur une base migrée.
    anonymized_at TEXT
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

# PRAGMAs de robustesse appliqués à CHAQUE connexion (pas seulement à l'init) :
# - busy_timeout : attend au lieu d'échouer immédiatement en « database is locked ».
# - wal_autocheckpoint / journal_size_limit : bornent la croissance du WAL.
# Réglages de session légers, compatibles WAL (aucun changement de mode).
_SESSION_PRAGMAS = (
    "PRAGMA foreign_keys = ON",
    "PRAGMA busy_timeout = 5000",
    "PRAGMA wal_autocheckpoint = 1000",
    "PRAGMA journal_size_limit = 67108864",
)


async def _apply_session_pragmas(db: aiosqlite.Connection) -> None:
    """Applique les PRAGMAs de session/robustesse sur une connexion ouverte."""
    for pragma in _SESSION_PRAGMAS:
        await db.execute(pragma)


@asynccontextmanager
async def get_db() -> AsyncGenerator[aiosqlite.Connection, None]:
    async with aiosqlite.connect(settings.db_path) as db:
        db.row_factory = aiosqlite.Row
        await _apply_session_pragmas(db)
        yield db


# ---------------------------------------------------------------------------
# Migrations versionnées (OPS-006)
# ---------------------------------------------------------------------------
#
# Un `CREATE TABLE IF NOT EXISTS` n'atteint JAMAIS une base déjà créée : tout
# changement de contrainte (clé étrangère, NOT NULL, UNIQUE…) exige une
# migration explicite. Le versionnement repose sur `PRAGMA user_version` :
#
#   user_version = 0  → base neuve, ou base déployée avant ce mécanisme ;
#   user_version = N  → les migrations 1..N ont été appliquées.
#
# Invariants du moteur :
#   - une migration s'applique entièrement ou pas du tout, et `user_version`
#     n'avance qu'avec elle (même transaction) ;
#   - une sauvegarde est prise avant la première migration réellement
#     applicable, jamais si la base est déjà à jour ;
#   - une base plus récente que le code est refusée (rollback applicatif) au
#     lieu d'être « migrée » à l'aveugle ;
#   - toute erreur remonte : fail-closed, le service ne démarre pas sur une
#     base à moitié migrée.

# PRAGMAs persistants, positionnés à l'initialisation de la base.
_PRAGMAS = (
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous = NORMAL",
)


class MigrationError(RuntimeError):
    """Échec de migration du schéma SQLite. Doit empêcher le démarrage du service."""


@dataclass(frozen=True)
class Migration:
    """
    Une étape de migration, déclarative et ordonnée.

    version
        `user_version` atteint après application. Strictement croissant.
    description
        Résumé opérationnel, journalisé. Ne doit contenir aucune donnée
        personnelle ni secret.
    statements
        Instructions SQL exécutées dans l'ordre, une par appel `execute()`.
        On n'utilise volontairement PAS `executescript()` : sqlite3 valide
        implicitement la transaction en cours avant de l'exécuter, ce qui
        casserait l'atomicité de la migration.
    apply
        Fonction Python exécutée après `statements`, dans la MÊME transaction.
        Nécessaire dès qu'une migration doit inspecter la base ou appliquer le
        motif « create table new → copy → drop old → rename » que SQLite impose
        pour changer une contrainte (cf. `_rebuild_table`).
    check_foreign_keys
        Lance `PRAGMA foreign_key_check` avant le COMMIT et échoue s'il reste
        des violations. À activer pour toute migration qui recrée une table.
        Laissé à False par défaut : une base historique peut porter des
        violations préexistantes, sans lien avec la migration en cours, et le
        démarrage du service ne doit pas en dépendre.
    """

    version: int
    description: str
    statements: tuple[str, ...] = ()
    apply: Callable[[aiosqlite.Connection], Awaitable[None]] | None = None
    check_foreign_keys: bool = False


def _split_statements(script: str) -> tuple[str, ...]:
    """
    Découpe un script DDL en instructions individuelles, exécutables une à une.

    Le schéma de ce module ne contient ni trigger ni littéral comportant un
    « ; » : le découpage naïf est donc sûr. Les lignes de commentaire sont
    retirées AVANT le découpage — un « ; » de ponctuation dans un commentaire
    français tronquerait sinon l'instruction qui l'entoure.
    """
    without_comments = "\n".join(
        line for line in script.splitlines() if not line.strip().startswith("--")
    )
    return tuple(
        chunk.strip() for chunk in without_comments.split(";") if chunk.strip()
    )


async def _rebuild_table(
    db: aiosqlite.Connection,
    table: str,
    create_new_sql: str,
    copy_columns: Sequence[str],
    index_statements: Sequence[str] = (),
) -> None:
    """
    Recrée `table` avec une nouvelle définition — seul moyen de changer une
    contrainte en SQLite (`ALTER TABLE` ne sait pas modifier une clé étrangère).

    Séquence appliquée : création de `<table>__new` → copie des `copy_columns`
    → `DROP` de l'ancienne table → renommage → recréation des index.

    `create_new_sql` doit créer la table sous le nom `<table>__new`. Les index
    de l'ancienne table disparaissent avec le `DROP` : `index_statements` doit
    les redéclarer.

    Les clés étrangères sont déjà désactivées par le moteur de migration
    (`PRAGMA foreign_keys = OFF`, positionné hors transaction) : sans cela le
    `DROP TABLE` casserait les références. Déclarer `check_foreign_keys=True`
    sur la migration pour faire vérifier l'intégrité avant le COMMIT.

    Les identifiants passés ici sont des littéraux du code, jamais des entrées
    utilisateur : l'interpolation de `table` dans le SQL est volontaire et sûre.
    """
    new_table = f"{table}__new"
    columns = ", ".join(copy_columns)
    await db.execute(create_new_sql)
    await db.execute(
        f"INSERT INTO {new_table} ({columns}) SELECT {columns} FROM {table}"
    )
    await db.execute(f"DROP TABLE {table}")
    await db.execute(f"ALTER TABLE {new_table} RENAME TO {table}")
    for statement in index_statements:
        await db.execute(statement)


async def _ensure_hourly_token_limit(db: aiosqlite.Connection) -> None:
    """
    Garantit la présence de `users.hourly_token_limit`.

    Avant OPS-006 cette évolution était un `ALTER TABLE` rejoué à chaque
    démarrage en avalant les erreurs « duplicate column ». Les bases déjà
    déployées peuvent donc porter la colonne tout en restant en
    `user_version = 0` : on inspecte l'état réel via `PRAGMA table_info` plutôt
    que de le supposer, et l'`ALTER TABLE` n'est émis que si la colonne manque.
    """
    rows = await (await db.execute("PRAGMA table_info(users)")).fetchall()
    if any(row["name"] == "hourly_token_limit" for row in rows):
        logger.debug("Colonne users.hourly_token_limit déjà présente.")
        return
    await db.execute(
        "ALTER TABLE users ADD COLUMN hourly_token_limit INTEGER NOT NULL DEFAULT 0"
    )


async def _ensure_anonymized_at(db: aiosqlite.Connection) -> None:
    """
    Garantit la présence de `users.anonymized_at` (COR-002).

    La colonne fait partie de `SCHEMA` : une base neuve la reçoit déjà par la
    migration de baseline, tandis qu'une base déployée avant COR-002 ne l'a pas.
    On inspecte donc l'état réel via `PRAGMA table_info` plutôt que de le
    supposer, et l'`ALTER TABLE` n'est émis que si la colonne manque — sans quoi
    SQLite rejetterait un « duplicate column name » sur base neuve.

    Aucune recréation de table n'est nécessaire : la politique DEC-001 conserve
    la ligne `users`, donc la clé étrangère `usage_log.user_id → users(id)`
    n'est jamais violée et sa contrainte reste inchangée.
    """
    rows = await (await db.execute("PRAGMA table_info(users)")).fetchall()
    if any(row["name"] == "anonymized_at" for row in rows):
        logger.debug("Colonne users.anonymized_at déjà présente.")
        return
    await db.execute("ALTER TABLE users ADD COLUMN anonymized_at TEXT")


# Liste ordonnée des migrations. Ajouter une entrée en fin de tuple, jamais
# réécrire une entrée déjà livrée (une base en production l'a déjà appliquée).
MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        description="Baseline : tables users/api_keys/usage_log et index",
        # Toutes les instructions sont en `IF NOT EXISTS` : sur une base
        # préexistante (user_version = 0) cette migration ne fait qu'estampiller
        # la version, sans toucher aux données.
        statements=_split_statements(SCHEMA),
    ),
    Migration(
        version=2,
        description="Colonne users.hourly_token_limit (absorption de l'ALTER historique)",
        apply=_ensure_hourly_token_limit,
    ),
    Migration(
        version=3,
        description="Colonne users.anonymized_at (politique d'anonymisation DEC-001)",
        apply=_ensure_anonymized_at,
    ),
)

# Version de schéma attendue par ce code.
SCHEMA_VERSION = MIGRATIONS[-1].version


def _target_version() -> int:
    """Version cible = version de la dernière migration déclarée."""
    return MIGRATIONS[-1].version if MIGRATIONS else 0


async def _read_user_version(db: aiosqlite.Connection) -> int:
    row = await (await db.execute("PRAGMA user_version")).fetchone()
    return int(row[0]) if row else 0


async def _write_user_version(db: aiosqlite.Connection, version: int) -> None:
    # Un PRAGMA n'accepte pas de paramètre lié ; la valeur est un entier validé
    # issu des migrations déclarées dans ce module, jamais d'une entrée externe.
    await db.execute(f"PRAGMA user_version = {int(version)}")


def _backup_path(db_path: Path, from_version: int) -> Path:
    """Chemin de sauvegarde dérivé de la base, incluant la version d'origine."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return db_path.with_name(
        f"{db_path.name}.pre-migration.v{from_version}.{stamp}.bak"
    )


async def _backup_database(
    db: aiosqlite.Connection, db_path: Path, from_version: int
) -> Path:
    """
    Sauvegarde cohérente de la base via l'API de sauvegarde SQLite.

    On n'utilise pas une copie de fichier : en mode WAL, le fichier principal
    seul est incomplet. `Connection.backup()` produit une image cohérente,
    WAL inclus.

    Le fichier est créé en 0600 AVANT toute écriture (O_EXCL : on n'écrase
    jamais une sauvegarde existante), puis restreint à ce que la base autorise
    déjà — une sauvegarde ne doit jamais être plus largement accessible que
    l'original.
    """
    target = _backup_path(db_path, from_version)
    os.close(os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    try:
        async with aiosqlite.connect(target) as dest:
            await db.backup(dest)
        # Restriction appliquée après l'écriture : un mode source en lecture
        # seule (0400) empêcherait sinon d'écrire la sauvegarde.
        os.chmod(target, stat.S_IMODE(db_path.stat().st_mode) & 0o600)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


async def _apply_migration(db: aiosqlite.Connection, migration: Migration) -> None:
    """
    Applique une migration de façon atomique : SQL, hook Python et
    `user_version` sont validés ensemble ou annulés ensemble.
    """
    logger.info("Migration SQLite %d — %s", migration.version, migration.description)
    await db.execute("BEGIN IMMEDIATE")
    try:
        # Relecture sous verrou d'écriture : un autre processus (service et CLI
        # démarrés en parallèle) a pu appliquer la migration entre la lecture
        # initiale de `user_version` et l'obtention du verrou. Sans ce contrôle,
        # une migration de recréation de table serait rejouée.
        if await _read_user_version(db) >= migration.version:
            await db.execute("COMMIT")
            logger.info(
                "Migration SQLite %d déjà appliquée par un autre processus, ignorée.",
                migration.version,
            )
            return
        for statement in migration.statements:
            await db.execute(statement)
        if migration.apply is not None:
            await migration.apply(db)
        if migration.check_foreign_keys:
            violations = await (
                await db.execute("PRAGMA foreign_key_check")
            ).fetchall()
            if violations:
                raise MigrationError(
                    f"Migration {migration.version} : {len(violations)} violation(s) "
                    "de clé étrangère détectée(s) avant validation."
                )
        await _write_user_version(db, migration.version)
        await db.execute("COMMIT")
    except BaseException as exc:
        try:
            await db.execute("ROLLBACK")
        except Exception:  # pragma: no cover — transaction déjà refermée
            logger.warning("ROLLBACK impossible après échec de migration.")
        logger.error(
            "Migration SQLite %d échouée, transaction annulée : %s",
            migration.version,
            exc,
        )
        if isinstance(exc, MigrationError):
            raise
        raise MigrationError(
            f"Migration {migration.version} ({migration.description}) échouée : {exc}"
        ) from exc
    logger.info(
        "Migration SQLite %d appliquée (user_version = %d).",
        migration.version,
        migration.version,
    )


async def _migrate(db_path: Path) -> None:
    """
    Amène la base au niveau `SCHEMA_VERSION`.

    Idempotent : si la base est déjà à jour, aucune transaction de migration
    n'est ouverte et aucune sauvegarde n'est produite.
    """
    # L'état d'existence est évalué AVANT la connexion : aiosqlite crée le
    # fichier à l'ouverture, ce qui rendrait le test inopérant ensuite.
    pre_existing = db_path.is_file() and db_path.stat().st_size > 0

    # isolation_level=None : contrôle manuel des transactions. Par défaut
    # sqlite3 ouvre et valide des transactions implicites qui interfèrent avec
    # les BEGIN/COMMIT explicites du moteur de migration.
    async with aiosqlite.connect(db_path, isolation_level=None) as db:
        db.row_factory = aiosqlite.Row
        for pragma in _PRAGMAS:
            await db.execute(pragma)
        await _apply_session_pragmas(db)

        current = await _read_user_version(db)
        target = _target_version()

        if current > target:
            raise MigrationError(
                f"Base SQLite en version de schéma {current}, code compatible "
                f"jusqu'à {target} : rollback applicatif détecté. Redéployer une "
                "version du code au moins égale au schéma, ou restaurer une "
                "sauvegarde antérieure. Aucune modification effectuée."
            )

        pending = [m for m in MIGRATIONS if m.version > current]
        if not pending:
            logger.debug(
                "Schéma SQLite déjà en version %d, aucune migration à appliquer.",
                current,
            )
            return

        if pre_existing:
            backup = await _backup_database(db, db_path, current)
            logger.info(
                "Sauvegarde avant migration (version %d) : %s", current, backup
            )
        else:
            logger.info(
                "Base SQLite absente ou vide : création du schéma en version %d.",
                target,
            )

        # `PRAGMA foreign_keys` est silencieusement IGNORÉ dans une transaction :
        # il doit donc être basculé ici, hors transaction, pour toute la série de
        # migrations. C'est une nécessité du motif de recréation de table
        # (`_rebuild_table`), dont le `DROP TABLE` casserait sinon les
        # références. L'état est restauré à ON quoi qu'il arrive.
        await db.execute("PRAGMA foreign_keys = OFF")
        try:
            for migration in pending:
                await _apply_migration(db, migration)
        finally:
            await db.execute("PRAGMA foreign_keys = ON")


async def init_db() -> None:
    """
    Crée ou migre le schéma et applique les pragmas. Idempotent.

    Fail-closed : toute erreur de migration remonte sous forme de
    `MigrationError` et empêche le démarrage plutôt que de laisser le service
    tourner sur une base à moitié migrée.
    """
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    await _migrate(settings.db_path)


# ---------------------------------------------------------------------------
# Clés API
# ---------------------------------------------------------------------------

def generate_api_key() -> tuple[str, str, str]:
    raw = "llmstu-" + secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(raw.encode()).hexdigest()
    key_prefix = raw[:15]
    return raw, key_hash, key_prefix


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


# ---------------------------------------------------------------------------
# CRUD utilisateurs
# ---------------------------------------------------------------------------

# Préfixe du pseudonyme attribué par `anonymize_user()`. Le « : » n'apparaît dans
# aucun nom d'utilisateur institutionnel : un compte anonymisé est identifiable
# d'un coup d'œil et ne peut pas entrer en collision avec un compte réel.
ANONYMIZED_USERNAME_PREFIX = "anonymized-user:"


def is_anonymized(user: dict) -> bool:
    """True si la ligne étudiante porte un horodatage d'anonymisation."""
    return bool(user.get("anonymized_at"))


async def create_user(
    username: str,
    email: str | None = None,
    rpm_limit: int | None = None,
    daily_token_limit: int | None = None,
    hourly_token_limit: int | None = None,
    concurrent_stream_limit: int | None = None,
    notes: str | None = None,
) -> dict:
    async with get_db() as db:
        cursor = await db.execute(
            """
            INSERT INTO users
                (username, email, rpm_limit, daily_token_limit, hourly_token_limit,
                 concurrent_stream_limit, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                username,
                email,
                rpm_limit or settings.default_rpm_limit,
                daily_token_limit or settings.default_daily_token_limit,
                hourly_token_limit if hourly_token_limit is not None else settings.default_hourly_token_limit,
                concurrent_stream_limit or settings.default_concurrent_stream_limit,
                notes,
            ),
        )
        await db.commit()
        return await get_user(cursor.lastrowid)


async def get_user(user_id: int) -> dict:
    async with get_db() as db:
        row = await (await db.execute("SELECT * FROM users WHERE id = ?", (user_id,))).fetchone()
        if not row:
            raise LookupError(f"Étudiant introuvable: {user_id}")
        return dict(row)


async def get_user_by_username(username: str) -> dict | None:
    async with get_db() as db:
        row = await (await db.execute("SELECT * FROM users WHERE username = ?", (username,))).fetchone()
        return dict(row) if row else None


async def list_users() -> list[dict]:
    async with get_db() as db:
        rows = await (await db.execute(
            """
            SELECT u.*,
                   MAX(k.last_used) AS last_api_call,
                   COUNT(k.id) AS key_count
            FROM users u
            LEFT JOIN api_keys k ON k.user_id = u.id AND k.is_active = 1
            GROUP BY u.id
            ORDER BY u.username
            """
        )).fetchall()
        return [dict(row) for row in rows]


async def update_user_quotas(
    user_id: int,
    rpm_limit: int | None = None,
    daily_token_limit: int | None = None,
    hourly_token_limit: int | None = None,
    concurrent_stream_limit: int | None = None,
) -> dict:
    updates: dict[str, int] = {}
    if rpm_limit is not None:
        updates["rpm_limit"] = rpm_limit
    if daily_token_limit is not None:
        updates["daily_token_limit"] = daily_token_limit
    if hourly_token_limit is not None:
        updates["hourly_token_limit"] = hourly_token_limit
    if concurrent_stream_limit is not None:
        updates["concurrent_stream_limit"] = concurrent_stream_limit
    if not updates:
        return await get_user(user_id)
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [user_id]
    async with get_db() as db:
        await db.execute(f"UPDATE users SET {set_clause} WHERE id = ?", values)
        await db.commit()
    return await get_user(user_id)


async def set_user_active(user_id: int, is_active: bool) -> None:
    async with get_db() as db:
        await db.execute("UPDATE users SET is_active = ? WHERE id = ?", (int(is_active), user_id))
        await db.commit()


async def anonymize_user(user_id: int) -> dict | None:
    """
    Anonymise un étudiant — chemin du droit à l'effacement RGPD (COR-002).

    Politique DEC-001 : la ligne `users` est CONSERVÉE, ses données personnelles
    sont effacées, le compte est suspendu et toutes ses clés `llmstu-*` sont
    révoquées.

    Pourquoi pas un `DELETE` : `usage_log.user_id` référence `users(id)` sans
    `ON DELETE CASCADE`, et `PRAGMA foreign_keys = ON` est appliqué à chaque
    connexion — un `DELETE FROM users` échouait donc en `IntegrityError` dès que
    l'étudiant avait servi une seule requête. `ON DELETE CASCADE` ferait perdre
    la traçabilité de facturation et `SET NULL` casserait les jointures des
    rapports : les deux ont été écartés.

    Effacé      : `username` (remplacé par un pseudonyme stable), `email`,
                  `notes`, et le champ libre `api_keys.name`.
    Conservé    : `id`, `created_at`, les lignes `usage_log` et les métadonnées
                  non identifiantes des clés (préfixe, dates, hash).
    Irréversible: aucune donnée effacée n'est récupérable.

    Idempotent : un second appel ne réécrit pas `anonymized_at` et n'échoue pas.
    Retourne None si l'étudiant n'existe pas (le caller décide du message).

    Le pseudonyme dérive de l'`id` (clé primaire AUTOINCREMENT, jamais
    réattribuée) : deux anonymisations ne peuvent pas entrer en collision sur la
    contrainte UNIQUE, et l'ancien nom redevient disponible pour un étudiant
    recréé plus tard (cas réel d'un étudiant qui revient).
    """
    async with get_db() as db:
        row = await (await db.execute(
            "SELECT id, anonymized_at FROM users WHERE id = ?", (user_id,)
        )).fetchone()
        if row is None:
            return None

        # `WHERE anonymized_at IS NULL` : le premier horodatage est préservé sans
        # relecture préalable, donc sans fenêtre de course avec un second appel.
        cursor = await db.execute(
            """
            UPDATE users
               SET username      = ?,
                   email         = NULL,
                   notes         = NULL,
                   is_active     = 0,
                   anonymized_at = datetime('now')
             WHERE id = ?
               AND anonymized_at IS NULL
            """,
            (f"{ANONYMIZED_USERNAME_PREFIX}{user_id}", user_id),
        )
        already_anonymized = cursor.rowcount == 0

        # Révocation des clés : inconditionnelle et idempotente. Le champ libre
        # `name` est effacé dans tous les cas (il peut porter un nom de personne).
        revoked = await db.execute(
            "UPDATE api_keys SET is_active = 0 WHERE user_id = ? AND is_active = 1",
            (user_id,),
        )
        revoked_now = revoked.rowcount
        await db.execute(
            "UPDATE api_keys SET name = NULL WHERE user_id = ? AND name IS NOT NULL",
            (user_id,),
        )
        await db.commit()

        final = await (await db.execute(
            "SELECT username, anonymized_at FROM users WHERE id = ?", (user_id,)
        )).fetchone()
        keys_total = await (await db.execute(
            "SELECT COUNT(*) AS n FROM api_keys WHERE user_id = ?", (user_id,)
        )).fetchone()

    return {
        "user_id": user_id,
        "username": final["username"],
        "anonymized_at": final["anonymized_at"],
        "already_anonymized": already_anonymized,
        "keys_revoked": revoked_now,
        "keys_total": int(keys_total["n"] or 0),
    }


# ---------------------------------------------------------------------------
# Clés API — CRUD
# ---------------------------------------------------------------------------

async def create_api_key(user_id: int, name: str | None, expires_at: str) -> tuple[str, dict]:
    raw, key_hash, key_prefix = generate_api_key()
    async with get_db() as db:
        cursor = await db.execute(
            "INSERT INTO api_keys (user_id, key_hash, key_prefix, name, expires_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, key_hash, key_prefix, name, expires_at),
        )
        await db.commit()
        row = await (await db.execute("SELECT * FROM api_keys WHERE id = ?", (cursor.lastrowid,))).fetchone()
        return raw, dict(row)


async def lookup_key(raw_key: str) -> dict | None:
    key_hash = hash_key(raw_key)
    async with get_db() as db:
        row = await (await db.execute(
            """
            SELECT
                u.id AS user_id, u.username, u.email, u.is_active AS user_active,
                u.rpm_limit, u.daily_token_limit, u.hourly_token_limit,
                u.concurrent_stream_limit,
                k.id AS key_id, k.key_prefix, k.name AS key_name, k.expires_at
            FROM api_keys k
            JOIN users u ON u.id = k.user_id
            WHERE k.key_hash = ? AND k.is_active = 1 AND u.is_active = 1
            """,
            (key_hash,),
        )).fetchone()

    if not row:
        return None
    result = dict(row)
    try:
        expires_str = result["expires_at"]
        expires_dt = datetime.fromisoformat(expires_str)
        if expires_dt.tzinfo is None:
            expires_dt = expires_dt.replace(tzinfo=timezone.utc)
        if expires_dt < datetime.now(timezone.utc):
            return None
    except (ValueError, TypeError):
        return None
    return result


async def touch_key_last_used(key_id: int) -> None:
    async with get_db() as db:
        await db.execute("UPDATE api_keys SET last_used = datetime('now') WHERE id = ?", (key_id,))
        await db.commit()


async def revoke_key(key_prefix: str) -> bool:
    async with get_db() as db:
        cursor = await db.execute(
            "UPDATE api_keys SET is_active = 0 WHERE key_prefix LIKE ?",
            (key_prefix + "%",),
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_user_keys(user_id: int) -> list[dict]:
    async with get_db() as db:
        rows = await (await db.execute(
            "SELECT * FROM api_keys WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        )).fetchall()
        return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Usage & quotas
# ---------------------------------------------------------------------------

async def log_usage(
    user_id: int,
    key_id: int | None,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    duration_ms: int,
    status_code: int,
    request_id: str,
) -> None:
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO usage_log
                (user_id, api_key_id, model, prompt_tokens, completion_tokens,
                 total_tokens, duration_ms, status_code, request_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id, key_id, model, prompt_tokens, completion_tokens,
                prompt_tokens + completion_tokens, duration_ms, status_code, request_id,
            ),
        )
        await db.commit()


async def tokens_used_today(user_id: int) -> int:
    async with get_db() as db:
        row = await (await db.execute(
            """
            SELECT COALESCE(SUM(total_tokens), 0) AS total
            FROM usage_log
            WHERE user_id = ? AND date(timestamp) = date('now')
            """,
            (user_id,),
        )).fetchone()
        return int(row["total"] or 0)


async def tokens_used_last_minutes(user_id: int, minutes: int) -> int:
    async with get_db() as db:
        row = await (await db.execute(
            """
            SELECT COALESCE(SUM(total_tokens), 0) AS total
            FROM usage_log
            WHERE user_id = ? AND timestamp >= datetime('now', ? || ' minutes')
            """,
            (user_id, f"-{minutes}"),
        )).fetchone()
        return int(row["total"] or 0)


async def purge_usage_older_than(days: int) -> int:
    """
    Purge de rétention MANUELLE (opt-in) des entrées `usage_log` plus anciennes
    que `days` jours, puis `VACUUM` complet pour restituer l'espace disque.

    Le `timestamp` est stocké en UTC au format SQLite (`datetime('now')`), donc la
    comparaison à `datetime('now', '-N days')` est correcte. Retourne le nombre de
    lignes supprimées. Aucune suppression automatique n'est déclenchée ailleurs ;
    à exécuter hors ligne (VACUUM verrouille la base).
    """
    if days < 0:
        raise ValueError("days doit être >= 0")
    async with get_db() as db:
        cursor = await db.execute(
            "DELETE FROM usage_log WHERE timestamp < datetime('now', ? || ' days')",
            (f"-{days}",),
        )
        deleted = cursor.rowcount
        await db.commit()
        # VACUUM complet (pas de PRAGMA incremental_vacuum : auto_vacuum n'est pas
        # activé, et on ne le change pas sur une base existante).
        await db.execute("VACUUM")
        return deleted


# ---------------------------------------------------------------------------
# Admin : stats et rapports
# ---------------------------------------------------------------------------

async def get_global_stats() -> dict:
    """Stats du jour et des 7 derniers jours."""
    async with get_db() as db:
        today = dict(await (await db.execute(
            """
            SELECT
                COUNT(*) AS requests,
                COALESCE(SUM(prompt_tokens), 0)      AS prompt_tokens,
                COALESCE(SUM(completion_tokens), 0)  AS completion_tokens,
                COALESCE(SUM(total_tokens), 0)       AS total_tokens,
                COUNT(DISTINCT user_id)              AS active_users,
                COALESCE(ROUND(AVG(duration_ms)), 0) AS avg_duration_ms
            FROM usage_log
            WHERE date(timestamp) = date('now')
            """
        )).fetchone())

        week = dict(await (await db.execute(
            """
            SELECT
                COUNT(*) AS requests,
                COALESCE(SUM(total_tokens), 0)  AS total_tokens,
                COUNT(DISTINCT user_id)         AS active_users
            FROM usage_log
            WHERE timestamp >= datetime('now', '-7 days')
            """
        )).fetchone())

        models = [
            dict(r) for r in await (await db.execute(
                """
                SELECT model,
                       COUNT(*) AS requests,
                       COALESCE(SUM(total_tokens), 0) AS total_tokens
                FROM usage_log
                WHERE date(timestamp) = date('now')
                GROUP BY model
                ORDER BY requests DESC
                """
            )).fetchall()
        ]

        total_active = dict(await (await db.execute(
            "SELECT COUNT(*) AS n FROM users WHERE is_active = 1"
        )).fetchone())

        return {
            "today": today,
            "week": week,
            "models_today": models,
            "total_active_users": int(total_active["n"] or 0),
        }


async def get_usage_report(days: int = 7, user_id: int | None = None) -> list[dict]:
    """Classement par tokens sur N jours, filtrable par utilisateur."""
    where_user = "AND u.id = ?" if user_id is not None else ""
    params: list = [f"-{days} days"]
    if user_id is not None:
        params.append(user_id)
    async with get_db() as db:
        rows = await (await db.execute(
            f"""
            SELECT
                u.id           AS user_id,
                u.username,
                u.email,
                COUNT(*)                             AS request_count,
                COALESCE(SUM(l.prompt_tokens), 0)    AS prompt_tokens,
                COALESCE(SUM(l.completion_tokens), 0) AS completion_tokens,
                COALESCE(SUM(l.total_tokens), 0)     AS total_tokens,
                COALESCE(ROUND(AVG(l.duration_ms)), 0) AS avg_duration_ms,
                MAX(l.timestamp)                     AS last_seen
            FROM usage_log l
            JOIN users u ON u.id = l.user_id
            WHERE l.timestamp >= datetime('now', ?)
              {where_user}
            GROUP BY u.id, u.username
            ORDER BY total_tokens DESC
            """,
            params,
        )).fetchall()
        return [dict(row) for row in rows]


async def get_expiring_keys(within_days: int = 30) -> list[dict]:
    """Clés actives qui expirent dans les N prochains jours."""
    async with get_db() as db:
        rows = await (await db.execute(
            """
            SELECT k.*, u.username, u.email
            FROM api_keys k
            JOIN users u ON u.id = k.user_id
            WHERE k.is_active = 1
              AND datetime(k.expires_at) >= datetime('now')
              AND datetime(k.expires_at) <= datetime('now', ? || ' days')
            ORDER BY k.expires_at
            """,
            (f"+{within_days}",),
        )).fetchall()
        return [dict(row) for row in rows]


async def get_all_keys_overview() -> list[dict]:
    """Vue admin : toutes les clés avec état et étudiant."""
    async with get_db() as db:
        rows = await (await db.execute(
            """
            SELECT k.*, u.username, u.email
            FROM api_keys k
            JOIN users u ON u.id = k.user_id
            ORDER BY u.username, k.expires_at
            """
        )).fetchall()
        return [dict(row) for row in rows]
