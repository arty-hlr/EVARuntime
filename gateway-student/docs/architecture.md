# Architecture cible

## Principe

La gateway etudiante est une passerelle edge bi-reseau. Elle ne remplace pas la
gateway admin : elle la consomme comme backend unique et controle tout ce qui
vient du reseau etudiant.

```text
Etudiants
  |
  | HTTPS TLS 1.3
  v
nginx edge sur VM gw-student, NIC etudiante
  |
  | HTTP local 127.0.0.1:8001
  v
FastAPI gateway-student
  |  auth + quotas + policy + audit
  |
  | HTTPS mTLS, URL fixe, bearer interne
  v
nginx interne gateway admin
  |
  v
gateway/ existante -> llama-server -> GPU L40S
```

## Invariants

1. Aucun paquet du VLAN etudiant n'atteint directement le serveur GPU.
2. Aucune route `/admin/*` n'existe dans la gateway etudiante.
3. Le code applicatif ne construit jamais une URL upstream depuis la requete
   entrante.
4. La base etudiante est separee de la base admin.
5. Une cle etudiante compromise reste bornee par expiration, RPM, tokens/jour et
   concurrence.
6. Le contenu des prompts et generations n'est jamais logge.

## Surface d'API exposee

```text
GET  /health
GET  /v1/models
POST /v1/chat/completions
```

Endpoints explicitement exclus du MVP :

- `/admin/*`
- `/v1/completions`
- `/completion`
- `/v1/tokenize`
- `/v1/detokenize`
- websocket ou proxy generique

## Flux applicatif

1. nginx rejette les gros bodies, les connexions lentes et les chemins inconnus.
2. FastAPI verifie le bearer `llmstu-*`.
3. Le rate limiter applique successivement burst, RPM, tokens/heure,
   tokens/jour, puis la limite de concurrence par etudiant.
4. `policy.py` normalise le JSON et supprime les champs hors allowlist.
5. `upstream.py` relaie vers `UPSTREAM_BASE_URL/v1/chat/completions`.
6. `audit.py` emet une ligne JSON sans contenu sensible.

## Schema SQLite et migrations versionnees

La base etudiante est separee de la base admin (invariant 4). Son schema est
versionne par `PRAGMA user_version` et amene a niveau par le moteur de migration
de `database.py`. Un `CREATE TABLE IF NOT EXISTS` n'atteint jamais une base deja
creee : sans ce moteur, aucun changement de contrainte ne toucherait une base
deployee.

### Versionnement

| `user_version` | Signification |
|---|---|
| `0` | base neuve, ou base deployee avant l'introduction du mecanisme |
| `1` | baseline : tables `users`, `api_keys`, `usage_log` et index |
| `2` | colonne `users.hourly_token_limit` garantie |

`SCHEMA_VERSION` est derive de la derniere entree du tuple `MIGRATIONS`.
`init_db()` amene la base a cette version puis n'a plus aucun effet : il est
idempotent, ce qui est indispensable ici car le CLI l'appelle a chaque commande.

### Absorption de l'ALTER historique

Avant ce mecanisme, `hourly_token_limit` etait ajoutee par un `ALTER TABLE`
rejoue a chaque demarrage en avalant les erreurs « duplicate column ». Une base
deja deployee peut donc porter la colonne tout en restant en `user_version = 0`.
La migration 2 inspecte l'etat reel via `PRAGMA table_info(users)` et n'emet
l'`ALTER TABLE` que si la colonne manque : les deux etats de depart convergent
vers `user_version = 2` sans echec.

### Structure d'une migration

```python
@dataclass(frozen=True)
class Migration:
    version: int                  # user_version atteint apres application
    description: str              # journalisee ; jamais de donnee personnelle
    statements: tuple[str, ...]   # SQL execute instruction par instruction
    apply: Callable | None        # hook Python, meme transaction
    check_foreign_keys: bool      # PRAGMA foreign_key_check avant COMMIT
```

Regles :

- Ajouter une entree en fin de tuple `MIGRATIONS`, `version` = precedente + 1.
  Ne jamais reecrire une entree deja livree : une base en production l'a deja
  appliquee et ne la rejouera pas.
- `statements` est un tuple d'instructions, pas un script : `executescript()` de
  `sqlite3` valide implicitement la transaction en cours et romprait
  l'atomicite.
- `apply` est necessaire des que la migration doit inspecter l'etat reel de la
  base ou recreer une table.
- `check_foreign_keys=True` pour toute migration qui recree une table. Valeur par
  defaut `False` : une base historique peut porter des violations preexistantes,
  et le demarrage du service ne doit pas en dependre.

### Transactionnalite et cles etrangeres

Chaque migration s'execute dans un `BEGIN IMMEDIATE` qui englobe son SQL, son
hook Python et l'ecriture de `user_version` : la version n'avance qu'avec la
migration, et un echec annule tout. Les migrations deja validees restent
acquises.

`PRAGMA foreign_keys` est silencieusement ignore a l'interieur d'une transaction.
Le moteur le positionne donc a `OFF` avant d'ouvrir la premiere transaction,
pour toute la serie, et le restaure a `ON` dans un `finally`. C'est une
necessite du motif de recreation de table, dont le `DROP TABLE` casserait sinon
les references. Les connexions applicatives (`get_db()`) retablissent
`foreign_keys = ON` a chaque ouverture.

`user_version` est relue sous le verrou d'ecriture, apres le `BEGIN IMMEDIATE` :
si un autre processus (service et CLI demarres en parallele) a applique la
migration entre-temps, elle est ignoree au lieu d'etre rejouee. Une migration de
recreation de table n'est donc jamais executee deux fois.

### Recreer une table pour changer une contrainte

SQLite ne sait pas modifier une cle etrangere par `ALTER TABLE`. Le seul motif
possible est *create new -> copy -> drop old -> rename*, encapsule par le helper
`_rebuild_table()` :

```python
async def _migration_usage_log_cascade(db: aiosqlite.Connection) -> None:
    """Recree usage_log pour supprimer les lignes d'un etudiant supprime."""
    await _rebuild_table(
        db,
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


MIGRATIONS: tuple[Migration, ...] = (
    # ... migrations 1 et 2, inchangees ...
    Migration(
        version=3,
        description="usage_log.user_id en ON DELETE CASCADE",
        apply=_migration_usage_log_cascade,
        check_foreign_keys=True,
    ),
)
```

Points d'attention : `create_new_sql` cree la table sous le nom `<table>__new` ;
les index de l'ancienne table disparaissent avec le `DROP` et doivent etre
redeclares dans `index_statements` ; des lignes orphelines preexistantes feront
echouer `check_foreign_keys` et doivent etre nettoyees dans la meme migration.

### Sauvegarde prealable

Avant la premiere migration reellement applicable, le moteur produit une
sauvegarde. Aucune copie si la base est deja a jour, ni si elle est neuve.

- Chemin : `<db_path>.pre-migration.v<version_origine>.<horodatage UTC>.bak`.
- Methode : API de sauvegarde SQLite (`Connection.backup()`), pas une copie de
  fichier — en mode WAL le fichier principal seul est incomplet.
- Permissions : cree en `0600` avant toute ecriture, puis restreint a
  l'intersection avec le mode de la base. La sauvegarde d'une base etudiante
  n'est jamais plus largement accessible que la base elle-meme.
- `O_EXCL` : une sauvegarde existante n'est jamais ecrasee.

### Echec — fail-closed

Toute erreur remonte en `MigrationError` depuis `init_db()`, donc depuis le
lifespan FastAPI : le service ne demarre pas sur une base a moitie migree.

| Situation | Comportement |
|---|---|
| Migration qui echoue | `ROLLBACK`, `user_version` inchangee, `MigrationError` |
| `user_version` > `SCHEMA_VERSION` | refus immediat, aucune ecriture, message designant le rollback applicatif |

Les journaux tracent version d'origine, version cible, description de chaque
migration et chemin de sauvegarde — jamais de donnee personnelle ni de secret,
conformement a l'invariant 6.

### Procedure operateur

Deploiement avec migration :

1. `systemctl stop gateway-student` — la migration s'execute au demarrage et ne
   doit pas concurrencer un service actif.
2. Deployer le nouveau code, puis demarrer le service.
3. `journalctl -u gateway-student | grep "Migration SQLite"`.
4. `sqlite3 <db_path> "PRAGMA user_version;"` pour confirmer la version.

En cas d'echec :

1. Le service ne demarre pas ; lire la `MigrationError` dans le journal.
2. La base est restee dans son etat d'avant la migration fautive.
3. Si necessaire, restaurer la sauvegarde produite juste avant :
   ```bash
   systemctl stop gateway-student
   cp students.db.pre-migration.v1.20260730T101500Z.bak students.db
   rm -f students.db-wal students.db-shm
   ```
4. Redeployer la version de code correspondant au schema restaure.

Le moteur est volontairement duplique depuis `gateway/database.py` : les deux
composants sont des frontieres de securite independantes et ne partagent aucun
code. Toute evolution du mecanisme doit etre reportee des deux cotes.

## Choix important

Le sous-projet ne duplique pas `model_manager` et ne connait pas les ports
`llama-server`. La selection, le chargement et l'eviction GPU restent uniquement
dans la gateway admin existante.

