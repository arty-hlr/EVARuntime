# EVARuntime — Analyse et suivi d'implémentation

> **Archive historique — ce document n'est plus la source de vérité.** Chaque item porte un ID
> stable (`EVA-NNN`) : on coche, on date, on référence le commit. La source active est
> [codex-analyse.md](../../codex-analyse.md). Ne pas renuméroter — un item abandonné passe en
> `~~barré~~` avec la raison.
>
> **Dernière mise à jour :** 2026-07-30
> **Branche auditée :** `dev` @ `49f8d59`
> **État des suites :** 354 tests au vert (309 gateway / 45 node_agent) — couverture 83 % (≈72 % hors code de test)
>
> Le composant `gateway-student` a été supprimé le 2026-07-30 (DEC-009 dans
> [codex-analyse.md](../../codex-analyse.md)). Les items qui ne le concernaient que lui
> — `EVA-006`, `EVA-023` — sont barrés ; le total ci-dessus ne compte plus ses 79 tests.

---

## Sommaire

1. [Tableau de bord](#1-tableau-de-bord)
2. [P0 — Bloquants avant production](#2-p0--bloquants-avant-production)
3. [P1 — Fiabilité et performance](#3-p1--fiabilité-et-performance)
4. [P2 — Automatisation du parcours « installation → premier token »](#4-p2--automatisation-du-parcours-installation--premier-token)
5. [P3 — Qualité, tests, standardisation](#5-p3--qualité-tests-standardisation)
6. [Analyse approfondie : performance et time-to-first-token](#6-analyse-approfondie--performance-et-time-to-first-token)
7. [Évaluation du rapport d'audit tiers](#7-évaluation-du-rapport-daudit-tiers)
8. [Journal des décisions](#8-journal-des-décisions)

---

## 1. Tableau de bord

| Lot | Items | Fait | En cours | À faire |
|---|---|---|---|---|
| **P0** — bloquants production | 9 | 0 | 0 | 9 |
| **P1** — fiabilité / performance | 15 | 0 | 0 | 15 |
| **P2** — automatisation install → token | 7 | 0 | 0 | 7 |
| **P3** — qualité / tests | 8 | 0 | 0 | 8 |
| **Total** | **39** | **0** | **0** | **39** |

**Portes de sortie :**

- 🚦 **Staging** → tous les P0 fermés.
- 🚦 **Pilote production** → P0 + P1 fermés, smoke test E2E réel au vert (`EVA-036`).
- 🚦 **Production générale** → + P2 livré, seuil de couverture en CI (`EVA-042`).

**Légende statut :** `☐` à faire · `◐` en cours · `☑` fait · `⊘` abandonné
**Légende preuve :** 🔬 reproduit en exécution · 📖 établi par lecture de code · 🧭 jugement d'architecture

---

## 2. P0 — Bloquants avant production

### ☐ EVA-001 · `/admin/status` retourne 500 en mode cluster 🔬

`VramBudgetResponse` exige `overhead_gb`, `safety_margin` et `budget_net_gb`
([schemas.py:190](../../gateway/schemas.py:190)). `ClusterManager.status()` ne produit que
`total_gb`, `used_gb`, `available_gb`, `nodes`, `nodes_online`
([cluster_manager.py:1212](../../gateway/cluster/cluster_manager.py:1212)).

Reproduit :

```
champ manquant: ('vram_budget', 'overhead_gb')   -> Field required
champ manquant: ('vram_budget', 'safety_margin') -> Field required
champ manquant: ('vram_budget', 'budget_net_gb') -> Field required
```

**Impact.** En `CLUSTER_MODE=cluster`, `GET /admin/status` lève `ResponseValidationError` → 500.
Le dashboard admin est donc inutilisable en cluster : c'est sa première requête.

**Correction.** Rendre les trois champs optionnels dans `VramBudgetResponse` (les notions
d'overhead/marge n'ont pas de sens agrégé côté cluster), **ou** ajouter un
`ClusterVramBudgetResponse` et typer `GatewayStatus.vram_budget` en union.
Préférer l'option 1 : moins de surface, contrat rétro-compatible en local.

- **Fichiers :** `gateway/schemas.py`, éventuellement `gateway/cluster/cluster_manager.py`
- **Validation :** test qui monte `/admin/status` en `CLUSTER_MODE=cluster` et vérifie 200
- **Statut :** ☐ · **Commit :** — · **Date :** —

---

### ☐ EVA-002 · La suppression d'un utilisateur échoue dès qu'il a servi 🔬

`usage_log.user_id` référence `users(id)` **sans `ON DELETE CASCADE`**
([gateway/database.py:48](../../gateway/database.py:48)),
alors que `PRAGMA foreign_keys = ON` est appliqué à chaque connexion.

Reproduit sur la gateway principale :

```
delete SANS usage -> True
delete AVEC usage -> EXCEPTION: IntegrityError FOREIGN KEY constraint failed
```

**Impact.** `DELETE /admin/users/{username}` remonte l'exception au handler global → **500**
pour tout utilisateur ayant fait au moins une requête, c'est-à-dire tous les utilisateurs réels.
La docstring annonce pourtant « suppression définitive […] CASCADE »
([database.py:302](../../gateway/database.py:302)). Fonction annoncée, jamais exerçable.
Sous-jacent : c'est aussi le chemin d'un droit à l'effacement RGPD.

**Correction.** Décision produit requise avant le code :

| Option | Conséquence | Recommandation |
|---|---|---|
| `ON DELETE CASCADE` sur `usage_log` | L'historique de facturation/audit disparaît | ❌ perte de traçabilité |
| `ON DELETE SET NULL` + `user_id` nullable | Les lignes restent, désassociées | ⚠️ casse les jointures des rapports |
| **Anonymisation** : `users` conservé, PII effacées, `is_active=0` | Historique agrégé préservé, personne non ré-identifiable | ✅ **retenu par défaut** |

Quelle que soit l'option : **migration versionnée obligatoire** (cf. `EVA-047`), une base
existante ne changeant pas de contrainte FK par simple `CREATE TABLE IF NOT EXISTS`.

- **Fichiers :** `gateway/database.py`, `gateway/cli.py`, `gateway/admin.py`
- **Validation :** test « créer user → logger de l'usage → supprimer → 200 et rapports cohérents »
- **Statut :** ☐ · **Commit :** — · **Date :** —

---

### ☐ EVA-003 · Le dashboard admin dépend de CDN externes, sans SRI ni CSP 📖

[dashboard.html:7-10](../../gateway/static/dashboard.html:7) charge Chart.js depuis `cdn.jsdelivr.net`
et les polices depuis `fonts.googleapis.com`. La page conserve l'`ADMIN_SECRET` en
`sessionStorage` ([dashboard.html:1466](../../gateway/static/dashboard.html:1466)).

**Impact triple, sur la surface la plus privilégiée du système :**

- **Supply-chain** — CDN compromis ⇒ JS arbitraire dans une page qui lit le secret admin et pilote tout le cluster. Aucun `integrity=`.
- **Souveraineté / RGPD** — l'IP de chaque poste admin part chez Google à chaque ouverture. En contradiction directe avec la mission affichée du projet.
- **Disponibilité** — en réseau isolé ou air-gap, les graphiques ne s'affichent pas.

**Correction.** Vendoriser `chart.umd.min.js` + les `.woff2` dans `gateway/static/`, ajouter
`Content-Security-Policy: default-src 'self'`. `install.sh` copie déjà `static/` récursivement
([install.sh:201-204](../../gateway/deploy/install.sh:201)) — aucun changement de packaging.

- **Fichiers :** `gateway/static/`, `gateway/main.py` (header CSP), `gateway/deploy/nginx.conf`
- **Validation :** chargement du dashboard avec le réseau externe coupé
- **Statut :** ☐ · **Commit :** — · **Date :** —

---

### ☐ EVA-004 · Le warm-up admin retourne 504 pour tout modèle réel 📖

[nginx.conf](../../gateway/deploy/nginx.conf) impose `proxy_read_timeout 30s` sur `location /admin/`.
Or `POST /admin/models/{id}/load` attend `MODEL_LOAD_TIMEOUT_SECONDS + 10`, soit **190 s** par
défaut ([server_manager.py:205](../../gateway/server_manager.py:205)), et **600 s** pour `minimax-m2.7`
([models.yaml:69](../../gateway/models.yaml:69)).

Le bouton « Load » du dashboard appelle exactement cette route
([dashboard.html:2250](../../gateway/static/dashboard.html:2250)).

**Impact.** Tout pré-chargement d'un modèle sérieux affiche un échec 504 **alors que le
chargement réussit côté serveur**. La fonctionnalité anti-cold-start — celle qui existe
précisément pour accélérer le premier token — est inutilisable via l'UI, et l'opérateur
perd confiance dans l'outil.

**Correction.** Bloc dédié :

```nginx
location ~ ^/admin/models/[^/]+/load$ {
    # mêmes restrictions IP que /admin/
    proxy_read_timeout 660s;   # > MODEL_LOAD_TIMEOUT_SECONDS max de models.yaml
    proxy_send_timeout 660s;
}
```

Alternative plus propre à moyen terme : rendre la route asynchrone (202 + polling sur
`/admin/status`), ce qui supprime la dépendance au timeout du reverse-proxy.

- **Fichiers :** `gateway/deploy/nginx.conf`, `docs/deployment.md`
- **Validation :** `curl` d'un load de 60 s+ à travers nginx, sans 504
- **Statut :** ☐ · **Commit :** — · **Date :** —

---

### ☐ EVA-005 · `install.sh` génère un `env` amputé de trois durcissements 📖

Le heredoc [install.sh:237-294](../../gateway/deploy/install.sh:237) omet `ALLOWED_MODEL_DIRS`,
`CORS_ALLOW_ORIGINS` et `LLAMA_SERVER_MIN_BUILD`. Ces clés existent dans `.env.example` et sont
documentées dans `docs/deployment.md` — mais un opérateur qui lit `/etc/llm-gateway/env`
(le seul fichier chargé par systemd) ne les découvre jamais.

**Toute installation issue du script tourne donc avec :**

| Réglage | Valeur effective | Conséquence |
|---|---|---|
| `CORS_ALLOW_ORIGINS` | `*` | N'importe quelle origine web appelle l'API avec une clé volée |
| `ALLOWED_MODEL_DIRS` | vide | Aucune contrainte de répertoire sur les chemins GGUF, alors que l'allowlist est implémentée et testée ([model_registry.py:426](../../gateway/model_registry.py:426)) |
| `LLAMA_SERVER_MIN_BUILD` | `0` | L'épinglage anti-`GHSA-8947-pfff-2f3c` est inactif, alors que tout [llama_version.py](../../gateway/llama_version.py) existe pour ça |

La documentation est en avance sur l'installateur. **Ces trois réglages deviennent
gratuits une fois `EVA-032`/`EVA-033` livrés** : c'est l'outil qui pose les fichiers, donc
l'outil connaît les chemins et le build.

- **Fichiers :** `gateway/deploy/install.sh`, `gateway/deploy/update.sh`
- **Validation :** `diff <(grep -o '^[A-Z_]*' /etc/llm-gateway/env) <(grep -o '^[A-Z_]*' gateway/.env.example)` sans clé de sécurité manquante
- **Statut :** ☐ · **Commit :** — · **Date :** —

---

### ~~EVA-006 · Fuite de slot de concurrence dans la gateway étudiante~~

**Abandonné le 2026-07-30 — sans objet.** Le défaut était localisé dans le composant
`gateway-student`, supprimé du dépôt (DEC-009 dans [codex-analyse.md](../../codex-analyse.md)).

Le mécanisme reste documenté ici parce qu'il éclaire `EVA-041` : **le `finally` d'un
générateur async jamais démarré ne s'exécute pas**, donc toute ressource acquise *avant*
de retourner une `StreamingResponse` fuit si le client se déconnecte avant le premier
`__anext__`. La gateway principale s'en protège déjà par le « pin de garde »
([proxy.py:238-249](../../gateway/proxy.py:238)) — c'est ce garde-fou qui reste à couvrir par
un test (`EVA-041`).

- **Statut :** ~~abandonné~~ · **Raison :** composant supprimé · **Date :** 2026-07-30

---

### ☐ EVA-007 · Le déchargement admin local coupe les générations en cours 📖

`LocalModelManager.unload_model()` ([model_manager.py:457](../../gateway/model_manager.py:457))
appelle `manager.unload()` **sans consulter `is_pinned`**. `ClusterManager.unload_model()`
refuse au contraire explicitement si `active_requests > 0`
([cluster_manager.py:807](../../gateway/cluster/cluster_manager.py:807)).

Chemins concernés : `POST /admin/models/{id}/unload`, `DELETE /admin/models/{id}`,
`PATCH` avec `enabled: false` ou changement de `llama_params` (qui décharge pour recharger).

**Impact.** Un opérateur qui désactive un modèle depuis le dashboard tue les streams en cours.
Cela viole l'invariant annoncé dans `AGENTS.md` : *« Un modèle traitant une requête active ne
doit pas être évincé »*. L'invariant est tenu pour l'éviction LRU, pas pour l'action admin.

**Correction.** Réutiliser `_drain_pinned()` — déjà écrit et testé pour le shutdown
([model_manager.py:483](../../gateway/model_manager.py:483)) — avec un timeout court, puis 409 si
le drain échoue, alignant le comportement local sur celui du cluster.

- **Fichiers :** `gateway/model_manager.py`, `gateway/admin.py`
- **Validation :** test « modèle pinné + unload admin → 409, stream intact »
- **Statut :** ☐ · **Commit :** — · **Date :** —

---

### ☐ EVA-008 · Les erreurs 401/429 ne respectent pas le format OpenAI 🔬

`auth.py` et `rate_limiter.py` lèvent `HTTPException(detail={"error": {...}})`. FastAPI
enveloppe systématiquement `detail`. Reproduit :

```
HTTP 401
corps : {"detail": {"error": {"message": "Clé API invalide...", "type": "authentication_error", "code": "401"}}}
cle 'error' au premier niveau (attendue par openai-python) -> ABSENTE
```

**Impact.** Le même client reçoit **deux formats d'erreur différents** selon l'échec : le proxy
utilise `_openai_error()` et produit `{"error": {...}}` correctement (400/404/503), mais l'auth
et les quotas produisent `{"detail": {"error": {...}}}`. `openai-python`, LiteLLM et le SDK
Vercel AI lisent `error.message` au premier niveau → message d'erreur vide ou générique
exactement sur les deux cas que l'utilisateur rencontre le plus (clé invalide, quota atteint).

**Correction.** Un `@app.exception_handler(HTTPException)` sur `app` qui ré-émet le corps
au format OpenAI de premier niveau, plutôt que de laisser FastAPI l'envelopper dans `detail`.

- **Fichiers :** `gateway/main.py`
- **Validation :** test paramétré sur 401 / 429 RPM / 429 quota → `"error"` au premier niveau
- **Statut :** ☐ · **Commit :** — · **Date :** —

---

### ☐ EVA-009 · `revoke_key` est vulnérable aux jokers SQL `LIKE` 📖

[database.py:277](../../gateway/database.py:277) : `UPDATE api_keys SET is_active = 0 WHERE key_prefix LIKE ?`
avec `(key_prefix + "%",)`, sans échapper `%` ni `_`.

**Impact.** `DELETE /admin/keys/%` révoque **toutes les clés du système** en une requête. Le
comportement est connu — un test le documente explicitement
([test_admin_routes.py:435](../../gateway/tests/test_admin_routes.py:435)) — mais reste non corrigé.
Route admin, donc pas une élévation de privilège : c'est un piège opérationnel à fort impact.

**Correction.** Valider le préfixe contre `^llmgw-[A-Za-z0-9_-]{1,32}$` avant la requête,
et ajouter `ESCAPE '\'` avec échappement de `%`/`_`.

- **Fichiers :** `gateway/database.py`, `gateway/admin.py`
- **Validation :** test `revoke_key("%")` → 0 clé révoquée / 422
- **Statut :** ☐ · **Commit :** — · **Date :** —

---

## 3. P1 — Fiabilité et performance

| ID | Item | Preuve | Fichier | Statut |
|---|---|---|---|---|
| EVA-010 | **3 à 5 connexions SQLite — donc autant de threads OS — par requête.** `aiosqlite.Connection` démarre un `Thread` par `connect()` (`aiosqlite/core.py:90`) et `get_db()` en ouvre une neuve à chaque appel + 4 PRAGMAs. Chemin gateway : `lookup_key` + `touch_key_last_used` + quota + `log_usage`. À 20 req/s, ~100 threads créés/détruits par seconde. Le pool `httpx` a été finement optimisé ; la couche DB n'a pas reçu le même traitement. → connexion persistante par process (uvicorn tourne en `--workers 1`) sous `asyncio.Lock`. | 📖 | `*/database.py` | ☐ |
| EVA-011 | **Cold start silencieux.** Sur modèle non chargé, `proxy_request` attend jusqu'à 190 s (600 s pour MiniMax) **sans émettre un octet**. Les SDK et proxies coupent avant. C'est exactement le parcours « première requête après installation ». → émettre les headers immédiatement puis `: keepalive\n\n` toutes les 5–10 s en mode `stream`. | 📖 | `gateway/proxy.py` | ☐ |
| EVA-012 | **Le streaming disparaît dès qu'il y a des `tools`.** [proxy.py:393](../../gateway/proxy.py:393) bufferise tout le stream avant le premier octet. Motif légitime (SDK Vercel AI refuse `content`+`tool_calls` mêlés) mais déclencheur trop large : s'applique même quand le modèle ne fait aucun tool call, soit la majorité des tours d'agent. → bufferiser jusqu'à la décision seulement, puis passthrough. | 📖 | `gateway/proxy.py` | ☐ |
| EVA-013 | **HTTP 200 renvoyé avant de connaître le statut upstream.** La `StreamingResponse` part avec 200 ; `status_code` upstream n'est lu qu'ensuite, dans le générateur. Une 4xx/5xx de `llama-server` devient donc un 200 contenant une erreur SSE. → sonder le statut upstream **avant** de construire la `StreamingResponse`, et propager l'erreur au format OpenAI. | 📖 | `gateway/proxy.py` | ☐ |
| EVA-014 | **`verify_integrity()` bloque la boucle événementielle du node-agent.** [node_agent/main.py:181](../../node_agent/main.py:181) : hash SHA-256 synchrone d'un GGUF de plusieurs centaines de Go, dans un handler `async`, **hors du `model_lock`** — donc l'agent entier gèle (health inclus → nœud marqué offline) et deux chargements concurrents hashent deux fois. → `asyncio.to_thread()` + déplacer sous le lock. | 📖 | `node_agent/main.py` | ☐ |
| EVA-015 | **`MemoryMax=64G` incompatible avec le plus gros modèle du registre.** `minimax-m2.7` = GGUF ~248 Go avec `cpu_moe: true` (experts FFN en RAM hôte). Les `llama-server` sont enfants du service, donc dans le même cgroup. Nuance technique : les pages mmap *propres* sont réclamables, donc l'OOM-kill n'est pas certain — mais le thrashing NVMe l'est, et le TTFT s'effondre. → dimensionner par profil de modèle et documenter le couple `MemoryMax` × `models.yaml`. | 🧭 | `gateway/deploy/llm-gateway.service` | ☐ |
| EVA-016 | **`/ready` peut déclarer prêt un système incapable de générer.** La sonde se contente de « un modèle ready **ou** de la VRAM disponible » ([main.py:286](../../gateway/main.py:286)) : ni existence du GGUF, ni binaire exécutable, ni capacité réelle. Or [update.sh:480](../../gateway/deploy/update.sh:480) décide du rollback **uniquement** sur `/ready` : une version incapable de servir est acceptée. → readiness structurelle stricte + smoke test séparé (`EVA-036`). | 📖 | `gateway/main.py`, `gateway/deploy/update.sh` | ☐ |
| EVA-017 | **Compteurs Prometheus qui décroissent.** `eva_requests_total` et `eva_tokens_total` sont déclarés `counter` ([metrics.py:369](../../gateway/metrics.py:369)) mais alimentés par une fenêtre glissante de 24 h. Un `counter` ne doit jamais décroître : `rate()` et `increase()` produiront des valeurs fausses. → `gauge` avec suffixe `_24h`, ou vrai compteur monotone en mémoire. | 📖 | `gateway/metrics.py` | ☐ |
| EVA-018 | **Les métriques du parcours utilisateur sont absentes.** Manquent : TTFT, temps d'attente en queue de capacité, temps de chargement modèle, taux de cold start, tokens/s par modèle, taux d'annulation client. Sans elles, aucune affirmation de performance n'est vérifiable — et `EVA-011`/`EVA-012` ne pourront pas être mesurés avant/après. **À faire en premier du lot P1.** | 🧭 | `gateway/metrics.py`, `gateway/proxy.py` | ☐ |
| EVA-019 | **Dépendances non figées.** Les `requirements.txt` n'utilisent que des `>=`, sans lockfile : deux installations à trois mois d'écart n'installent pas les mêmes versions. Vérifier au passage qu'aucune dépendance de test ne subsiste en runtime. → `requirements.lock` (`pip-compile`), `requirements-dev.txt` séparé. | 📖 | `*/requirements.txt` | ☐ |
| EVA-020 | **Race sur les quotas.** Vérification avant inférence, écriture après : N requêtes concurrentes passent toutes le contrôle et dépassent le quota d'autant. Impact borné en usage universitaire, réel en usage automatisé. → réservation atomique, ou acceptation documentée du dépassement borné. | 📖 | `gateway/rate_limiter.py` | ☐ |
| EVA-021 | **Data-plane cluster en HTTP clair.** Le plan de contrôle orchestrateur→agent est en HTTPS + Bearer, mais le trafic **contenant les prompts** orchestrateur→`llama-server` est en `http://` ([node_client.py:168](../../gateway/cluster/node_client.py:168)). La confidentialité repose entièrement sur l'isolation du LAN — à documenter explicitement comme prérequis, ou tunneliser (WireGuard / mTLS). | 📖 | `gateway/cluster/`, `docs/architecture.md` | ☐ |
| EVA-022 | **`/completion` documenté mais inaccessible.** La route existe ([main.py:422](../../gateway/main.py:422)) et est documentée, mais `nginx.conf` termine par `location / { return 404; }` et ne route que `/v1/`, `/health`, `/admin/`. → ajouter la route à nginx **ou** retirer l'alias non-`/v1` de la doc. | 📖 | `gateway/deploy/nginx.conf`, `docs/api.md` | ☐ |
| ~~EVA-023~~ | ~~**Rotation d'audit étudiante par taille, pas par durée.**~~ Abandonné le 2026-07-30 : composant `gateway-student` supprimé (DEC-009). Le principe reste valable si un journal d'audit à rétention temporelle est réintroduit ailleurs — `RotatingFileHandler(maxBytes=…)` borne un volume, pas une durée ; il faut `TimedRotatingFileHandler`. | — | — | ~~abandonné~~ |
| EVA-024 | **Rate-limiting nginx par IP derrière NAT campus.** `limit_req 60r/m` et `limit_conn 4` sur `$binary_remote_addr` : tous les utilisateurs derrière le NAT partagent une IP, donc le 5ᵉ stream simultané **de toute l'université** prend un 429. → clé sur un identifiant applicatif, ou exempter les plages campus (le rate limiting applicatif par utilisateur reste en place). | 🧭 | `gateway/deploy/nginx.conf` | ☐ |

---

## 4. P2 — Automatisation du parcours « installation → premier token »

### Le problème, chiffré

Aujourd'hui `install.sh` prépare **l'hôte** correctement — utilisateur système, répertoires,
permissions, venv, systemd, nginx, journald, timer de sauvegarde, secrets générés. Mais il
s'arrête avant le produit. Restent **six actions manuelles** entre la fin du script et le
premier token :

| # | Action manuelle | Risque d'erreur | Automatisable |
|---|---|---|---|
| 1 | Compiler/récupérer `llama-server` CUDA | Élevé — flags CUDA, arch GPU, version | ✅ `EVA-032` |
| 2 | Télécharger les GGUF (+ projecteurs mmproj) | Moyen — quantisation, chemin, licence | ✅ `EVA-033` |
| 3 | Renseigner `TOTAL_VRAM_GB` | Moyen — se trompe d'unité ou de GPU | ✅ `EVA-031` |
| 4 | **Estimer `vram_gb` par modèle** | **Très élevé** — voir ci-dessous | ✅ `EVA-034` |
| 5 | TLS + domaine | Élevé | ❌ hors périmètre (PKI établissement) |
| 6 | Créer utilisateur + clé + tester | Faible | ✅ `EVA-035` |

**Le point 4 est le plus coûteux et le moins visible.** `models.yaml` demande une estimation
manuelle qui doit inclure poids **et** KV cache, avec des commentaires du type
*« Affiner via nvidia-smi au 1er démarrage »* ([models.yaml:67](../../gateway/models.yaml:67)).
Une sous-estimation provoque un CUDA OOM en pleine charge ; une surestimation gâche de la VRAM
et déclenche des évictions LRU inutiles. Le code contient déjà un garde-fou d'approximation
(`_warn_kv_cache`, [admin.py:53](../../gateway/admin.py:53)) qui *« suppose une architecture 7B,
128 B/token »* — un minorant grossier, assumé comme tel.

Or **toutes les données nécessaires au calcul exact sont dans le fichier GGUF lui-même.**

### L'argument stratégique

Ce lot n'est pas seulement du confort. Trois durcissements aujourd'hui désactivés par défaut
(`EVA-005`) le sont parce qu'ils exigent une information que seul l'opérateur possède :

| Durcissement | Bloqué par | Débloqué par |
|---|---|---|
| `sha256` du GGUF | l'opérateur devrait hasher à la main | `EVA-033` — l'outil télécharge, donc il connaît le hash |
| `LLAMA_SERVER_MIN_BUILD` | l'opérateur devrait connaître le build | `EVA-032` — l'outil installe, donc il connaît le build |
| `ALLOWED_MODEL_DIRS` | l'opérateur devrait fixer les chemins | `EVA-033` — l'outil choisit le répertoire |

**Automatiser le provisionnement, c'est ce qui rend le durcissement par défaut réaliste.**
C'est le meilleur argument pour prioriser ce lot juste après les P0.

---

### ☐ EVA-030 · `evaruntime doctor` — préflight de la gateway

Le node-agent possède un préflight remarquable ([node_agent/preflight.py](../../node_agent/preflight.py)) :
permissions des fichiers sensibles, TLS, binaire exécutable, répertoires lisibles, health-check
sans exposer le secret à `ps`. **La gateway principale, plus critique, n'a pas d'équivalent.**

Portage direct, avec en plus : GPU détecté, budget VRAM cohérent avec `models.yaml`,
GGUF présents et lisibles, ports du pool libres, DB accessible en écriture, secrets non
placeholder, cohérence `nginx` ↔ `MODEL_LOAD_TIMEOUT_SECONDS` (`EVA-004`).

- **Sortie :** exit code + rapport lisible, exécutable avant `systemctl start` **et** dans `update.sh`
- **Statut :** ☐

### ☐ EVA-031 · Auto-détection GPU → `TOTAL_VRAM_GB`

```bash
nvidia-smi --query-gpu=index,name,memory.total,compute_cap --format=csv,noheader,nounits
```

Renseigne `TOTAL_VRAM_GB` et `CUDA_VISIBLE_DEVICES`, et sert d'entrée à `EVA-032` pour choisir
le bon binaire. `install.sh` **exige déjà** `nvidia-smi` en mode local
([install.sh:128](../../gateway/deploy/install.sh:128)) — l'information est disponible, simplement pas
exploitée. Effort minimal, gain immédiat.

- **Statut :** ☐

### ☐ EVA-032 · Récupération automatique de `llama-server`

`llama.cpp` est sous licence MIT et publie des binaires précompilés sur ses releases GitHub.
Deux chemins à prévoir :

1. **Release officielle** (défaut) — télécharger l'artefact Linux/CUDA correspondant à la
   compute capability détectée, vérifier la somme publiée, installer dans `/usr/local/bin`.
2. **Compilation locale** (repli) — pour les architectures non couvertes ; le repo a déjà
   [docs/build-llama-cpp-dgx-spark.md](../../docs/build-llama-cpp-dgx-spark.md) qui documente ce cas.

**Effet de bord précieux :** le script connaît le numéro de build installé → il renseigne
`LLAMA_SERVER_MIN_BUILD` automatiquement. Le garde-fou supply-chain de
[llama_version.py](../../gateway/llama_version.py), aujourd'hui inerte, devient actif **par défaut
et sans effort**.

> ⚠️ **À vérifier avant implémentation :** la matrice exacte des artefacts CUDA publiés par
> release (elle a changé plusieurs fois), et la disponibilité pour ARM64 / Grace-Blackwell si
> les DGX Spark sont ciblés. Ne pas coder le nom d'artefact en dur : le résoudre via l'API
> GitHub des releases, avec repli sur la compilation.

- **Statut :** ☐

### ☐ EVA-033 · Récupération automatique des GGUF + `sha256`

Téléchargement depuis Hugging Face vers `ALLOWED_MODEL_DIRS`, avec **calcul du SHA-256 au fil
du téléchargement** (aucun coût supplémentaire : le flux passe déjà) et écriture dans
`models.yaml`. La vérification d'intégrité de
[model_registry.py:156](../../gateway/model_registry.py:156), aujourd'hui opt-in et donc inutilisée,
devient active par défaut.

**Trois pièges à traiter explicitement :**

- **Modèles *gated*** — Llama exige l'acceptation de la licence Meta. Détecter le 401/403 et
  afficher un message actionnable (`HF_TOKEN`), sans jamais contourner.
- **Licences hétérogènes** — Qwen (Apache-2.0), Llama (licence communautaire Meta), Gemma
  (conditions Google). **Afficher la licence et exiger un consentement explicite** avant
  téléchargement. Une université ne peut pas redistribuer implicitement des poids sous licence
  restrictive.
- **Reprise** — un GGUF de 40 Go doit se reprendre après coupure (`Range`), sinon l'outil est
  inutilisable sur réseau campus.

- **Statut :** ☐

### ☐ EVA-034 · Calcul automatique de `vram_gb` depuis le header GGUF

**C'est l'item à plus fort effet de levier du lot.**

Le format GGUF expose dans son header, en clair, tout ce qu'il faut :

| Métadonnée GGUF | Usage dans le calcul |
|---|---|
| taille du fichier / tailles de tenseurs | poids résidents en VRAM (si `n_gpu_layers` = tout) |
| `{arch}.block_count` | nombre de couches → dimension du KV cache |
| `{arch}.attention.head_count_kv` | têtes KV (GQA/MQA — divise le KV par 4 à 8 vs MHA) |
| `{arch}.embedding_length`, `.attention.head_count` | dimension par tête |
| `{arch}.context_length` | borne haute de `ctx_size` — permet de **refuser une config impossible** |
| `general.architecture` | choix de la formule, détection MoE |

Combiné aux `llama_params` déjà présents (`ctx_size`, `parallel`, `cache_type_k/v`, `cpu_moe`),
on obtient une estimation calculée plutôt que devinée :

```
vram ≈ poids_offloadés
     + 2 × block_count × head_count_kv × (embedding_length / head_count)
         × ctx_size × parallel × octets_par_élément(cache_type)
     + buffers_compute(batch_size, ubatch_size)
     + overhead_CUDA (~0,6–1 Go)
```

**Sur le choix de l'outil.** Vous évoquez `LLMfit` : je ne peux pas confirmer son existence
exacte, sa licence ni son périmètre sans vérification — et **la licence est éliminatoire**
(MIT / Apache-2.0 / BSD intégrables ; GPL/AGPL contamineraient un projet destiné à être déployé
en établissement). Trois options, par ordre de préférence :

| Option | Licence | Avantage | Inconvénient |
|---|---|---|---|
| **`gguf` (paquet Python officiel de llama.cpp)** | MIT (à confirmer sur PyPI) | Même projet que le binaire, donc suit le format ; maintenu | Une dépendance de plus |
| Parseur interne (~250 lignes, `struct` stdlib) | — | Zéro dépendance, périmètre étroit (lire un header) | À maintenir si le format évolue |
| `LLMfit` ou équivalent tiers | **à vérifier** | Peut-être plus complet | Licence et pérennité inconnues |

**Recommandation :** partir sur `gguf` officiel après vérification de licence, avec repli
sur le parseur interne. Le besoin réel est étroit — lire un header, pas gérer quarante backends —
donc l'option 2 reste tout à fait tenable si la licence pose problème. Je rejoins votre
intuition : ne pas réécrire ce qui existe, **mais** ici le « from scratch » coûte
raisonnablement peu.

**Bénéfice collatéral :** remplace l'approximation « architecture 7B » de `_warn_kv_cache`
([admin.py:53](../../gateway/admin.py:53)) par un vrai calcul, et permet à `POST /admin/models` de
**refuser** une configuration qui ne tiendra pas, au lieu d'avertir dans les logs.

- **Statut :** ☐

### ☐ EVA-035 · `evaruntime bootstrap` — one-shot jusqu'au premier token

Enchaînement des briques ci-dessus, en une commande idempotente :

```
1. doctor            → préflight hôte (EVA-030)
2. detect-gpu        → TOTAL_VRAM_GB, compute capability (EVA-031)
3. fetch-runtime     → llama-server + LLAMA_SERVER_MIN_BUILD (EVA-032)
4. fetch-model       → GGUF + sha256 + ALLOWED_MODEL_DIRS (EVA-033)
5. plan-model        → vram_gb, ctx_size max, capabilities (EVA-034)
6. create-user       → utilisateur + clé API (CLI existant)
7. smoke             → génération réelle, TTFT chaud et froid mesurés
```

Le CLI existe déjà et couvre l'étape 6 ([gateway/cli.py](../../gateway/cli.py)) : il s'agit de
l'étendre, pas de repartir de zéro.

**Critère de réussite :** sur une machine neuve avec GPU, `bootstrap` produit un token généré
et affiche le TTFT mesuré, **sans aucune édition manuelle de fichier** hormis le TLS.

- **Statut :** ☐

### ☐ EVA-036 · Smoke test réel dans `update.sh`

`update.sh` décide aujourd'hui de conserver ou restaurer une version sur la seule base de
`/ready` ([update.sh:480](../../gateway/deploy/update.sh:480)) — sonde permissive (`EVA-016`). Ajouter
après le redémarrage : authentification avec une clé de service, génération de quelques tokens
sur un petit modèle de référence, mesure du TTFT, rollback si échec ou si le TTFT dépasse un
seuil configuré.

C'est ce qui transforme « le service répond » en « le service **sert** ».

- **Statut :** ☐

---

## 5. P3 — Qualité, tests, standardisation

| ID | Item | Détail | Statut |
|---|---|---|---|
| EVA-040 | **Tests du CLI** | `cli.py` : **0 % de couverture** (218 statements). C'est l'outil d'administration principal en production, jamais exécuté par un test. Typer fournit `CliRunner` — coût faible, gain immédiat. | ☐ |
| EVA-041 | **Test du pin de garde streaming** | Le mécanisme le plus subtil du proxy (cf. `EVA-006` pour le détail du piège qu'il neutralise) n'est couvert par aucun test. `proxy.py` : 56 %, lignes 214-268 non couvertes. | ☐ |
| EVA-042 | **Seuil de couverture en CI** | La CI ne mesure pas la couverture : elle ne peut donc que dériver. Ajouter `--cov --cov-fail-under=70`, à relever progressivement. | ☐ |
| EVA-043 | **`pip-audit` bloquant** | Actuellement `continue-on-error: true` ([ci.yml:106](../../.github/workflows/ci.yml:106)) : les CVE sont affichées mais n'arrêtent rien. Rendre bloquant avec un fichier d'exceptions daté et justifié. | ☐ |
| EVA-044 | **Élargir `ruff`** | Aujourd'hui `E` + `F` seulement, avec des `per-file-ignores` sur des `F401` préexistants. Ajouter `I` (imports), `B` (bugbear), `UP`, et **nettoyer** les imports morts plutôt que les ignorer. | ☐ |
| EVA-045 | **`cleanup_stale()` jamais appelé** | [rate_limiter.py:62](../../gateway/rate_limiter.py:62) : code écrit pour éviter une fuite mémoire, jamais branché. Croissance bornée par le nombre d'utilisateurs, donc bénin — mais c'est une intention non tenue. Brancher ou supprimer. | ☐ |
| EVA-046 | **Tests E2E avec un vrai `llama-server`** | Les 433 tests tournent en < 3 s : ils sont excellents mais **exclusivement unitaires**. Manquent : un vrai binaire avec un GGUF minuscule, un parcours nginx→gateway→llama, un cluster gateway→agent→llama, les coupures réseau réelles. À faire tourner sur un runner GPU dédié, hors CI principale. | ☐ |
| EVA-047 | **Migrations SQLite versionnées** | Le schéma est créé par `CREATE TABLE IF NOT EXISTS` : **aucun changement de contrainte n'atteint une base existante**. Bloquant pour `EVA-002`. → moteur de migrations versionné par `PRAGMA user_version`, transactionnel, avec sauvegarde préalable. | ☐ |

---

## 6. Analyse approfondie : performance et time-to-first-token

### Ce qui est déjà bien fait

Les fondations sont saines et il faut le dire clairement : appels réseau et sous-processus
asynchrones de bout en bout, client `httpx` partagé avec keep-alive dimensionné et commenté
([proxy.py:58-81](../../gateway/proxy.py:58)), SQLite WAL avec PRAGMAs de robustesse par connexion,
SSE sans buffering jusqu'à nginx inclus, queue d'admission bornée, éviction LRU respectant
les requêtes actives, drain borné au shutdown. Ce sont les bons choix.

### Décomposition du TTFT, et où passe le temps

| Phase | Coût actuel | Item |
|---|---|---|
| Auth (`lookup_key`) | 1 connexion SQLite = 1 thread OS | `EVA-010` |
| `touch_key_last_used` | 1 connexion (hors chemin critique, fire-and-forget) | `EVA-010` |
| Quota mensuel | 1 connexion + `SUM()` sur `usage_log`, **à chaque requête** si quota actif | `EVA-010` |
| Résolution + admission modèle | négligeable si chaud | — |
| **Chargement à froid** | **jusqu'à 190 s (600 s MiniMax), en silence total** | `EVA-011` |
| Premier octet upstream | dépend de `llama.cpp` | — |
| Réécriture par chunk | `json.loads` + `json.dumps` **par token** | mesurer via `EVA-018` |
| **Avec `tools`** | **stream entièrement bufferisé → TTFT = durée totale** | `EVA-012` |

**Deux constats dominent.**

**1. Le cold start est le vrai problème de « premier token », et il est invisible.**
Un utilisateur qui envoie sa première requête après installation tombe systématiquement dans
le cas 190 s de silence. Aucun octet, aucun header, aucune indication. Les SDK coupent, les
proxies coupent, l'utilisateur conclut que ça ne marche pas. Le correctif (`EVA-011`) est
petit — quelques lignes de heartbeat SSE — et son effet perçu est disproportionné.

**2. Rien n'est mesuré, donc rien n'est améliorable de façon défendable.**
Aucune métrique de TTFT, de temps de queue, de temps de chargement ni de taux de cold start
n'existe. Avant d'optimiser quoi que ce soit, livrer `EVA-018` : sinon les gains de `EVA-010`
et `EVA-012` resteront des affirmations, pas des mesures. **C'est le premier item du lot P1
à traiter.**

### Ordre d'attaque recommandé pour la performance

```
EVA-018 (instrumenter)  →  mesure de référence
   ↓
EVA-011 (heartbeat cold start)      gain perçu maximal, coût minimal
   ↓
EVA-010 (connexion SQLite tenue)    ~3-5 ms de TTFT + charge CPU sous concurrence
   ↓
EVA-012 (buffering tools ciblé)     restaure le streaming pour les agents
   ↓
EVA-034 (vram_gb calculé)           supprime les OOM et les évictions inutiles
   ↓
mesure post-correctifs, comparaison à la référence
```

---

## 7. Évaluation du rapport d'audit tiers

> Rapport transmis pour relecture, produit par un autre système. Chaque affirmation vérifiable
> a été **testée**, pas seulement lue.

### Verdict : rapport de bonne qualité, à retenir — avec une réserve

C'est un audit sérieux, honnête sur ses limites, et **il a trouvé plusieurs choses réelles que
mon propre audit avait manquées**. Sa méthode a été plus systématique que la mienne sur un point
précis : il a confronté les **contrats de données** (schémas Pydantic, contraintes FK) au
comportement réel, là où je m'étais concentré sur les chemins d'exécution et l'exploitation.

### Affirmations vérifiées comme exactes

| Affirmation | Vérification |
|---|---|
| `/admin/status` casse en cluster | 🔬 **Confirmé** — 3 champs manquants reproduits (`EVA-001`) |
| `delete_user` échoue après usage | 🔬 **Confirmé** — `FOREIGN KEY constraint failed` reproduit sur les deux composants (`EVA-002`) |
| Erreurs auth/quota hors format OpenAI | 🔬 **Confirmé** — `{"detail":{"error":…}}` reproduit (`EVA-008`) |
| Unload admin local sans drain | 📖 **Confirmé** — asymétrie avec `ClusterManager` (`EVA-007`) |
| SHA-256 bloquant dans le node-agent | 📖 **Confirmé** — synchrone, hors lock (`EVA-014`) |
| Compteurs Prometheus décroissants | 📖 **Confirmé** (`EVA-017`) |
| `/completion` bloqué par nginx | 📖 **Confirmé** (`EVA-022`) |
| Stream 200 avant statut upstream | 📖 **Confirmé** (`EVA-013`) |
| Rotation audit par taille ≠ 90 jours | 📖 **Confirmé** (`EVA-023`) |
| `MemoryMax=64G` vs MiniMax 248 Go | 📖 **Confirmé** avec nuance (`EVA-015`) |
| Readiness permissive + gate de rollback | 📖 **Confirmé** (`EVA-016`) |
| Dashboard CDN, timeouts admin, durcissements optionnels | 📖 **Confirmé** — convergent avec mon audit |

**Sept findings que je n'avais pas relevés**, dont deux bloquants de production
(`EVA-001`, `EVA-002`). Sur ce plan, son rapport est plus complet que le mien.

### Une affirmation à corriger

> *« Après `ensure_model_loaded()` dans proxy.py:202, le pin n'est posé que plus tard.
> Une requête concurrente peut théoriquement évincer le modèle pendant cette fenêtre. »*

**Cette fenêtre n'existe pas.** Entre le retour de `ensure_model_loaded()`
([proxy.py:202](../../gateway/proxy.py:202)) et `manager.pin()` ([proxy.py:238](../../gateway/proxy.py:238)
et [proxy.py:264](../../gateway/proxy.py:264)), il n'y a que trois affectations synchrones —
`is_streaming`, `request_id`, `start_time` — et **aucun point de suspension `await`**.
En asyncio mono-thread coopératif, aucune autre tâche ne peut s'intercaler sans `await` :
l'éviction concurrente est impossible sur ce segment.

Ce n'est pas anodin : l'auteur en tire une recommandation d'architecture (« admission atomique
retournant un lease déjà pinné ») sur une prémisse fausse. La recommandation reste défendable
comme défense en profondeur — elle protégerait un futur refactor qui introduirait un `await`
au mauvais endroit — mais **ce n'est pas un bug actuel et cela ne justifie pas un P0**.
En revanche, le second point du même paragraphe (unload admin sans drain) est bien réel :
c'est `EVA-007`.

### Points de méthode

**À son crédit :** il déclare explicitement ce qu'il n'a pas pu faire — `ruff` non exécuté,
`pip-audit` non finalisé, aucun GPU disponible — au lieu de le passer sous silence. Il refuse
de conclure sur la performance faute de mesure. C'est la bonne posture : un audit qui affirme
des choses qu'il n'a pas vérifiées est plus dangereux qu'un audit incomplet.

**Ses angles morts**, par rapport à ce que j'ai trouvé :

- **Le coût des threads `aiosqlite`** (`EVA-010`) — 3 à 5 threads OS créés/détruits par requête.
  C'est le principal levier de performance identifiable *sans* GPU, et il ne le mentionne pas.
- **Le cold start silencieux** (`EVA-011`) — il note bien l'absence de benchmark, mais pas le
  fait que la première requête reste 190 s sans émettre un octet. C'est pourtant le défaut
  n°1 du parcours « premier token » qu'il évalue.
- **`revoke_key` et les jokers `LIKE`** (`EVA-009`).

### Sur sa notation

Il conclut à **5–6/10** de préparation production. Je le trouve un peu sévère, mais
défendable maintenant que `EVA-001` et `EVA-002` sont confirmés : deux routes d'administration
retournent des 500 sur des parcours normaux, et ça pèse lourd. Mon évaluation : **6,5–7/10** —
l'architecture, le durcissement systemd, les invariants de cycle de vie et la documentation
sont nettement au-dessus de la moyenne, et les défauts trouvés sont tous localisés et
corrigibles sans refonte. Nous convergeons sur l'essentiel : **consolider, pas réécrire.**

### Conclusion sur ce rapport

**Rapport fiable, à intégrer.** Onze affirmations vérifiées exactes sur douze, une seule
erreur de raisonnement identifiée et documentée ci-dessus. Ses findings sont fusionnés dans
les lots P0/P1 de ce document. Le fait que deux audits indépendants convergent sur le
dashboard CDN, les timeouts admin et les durcissements optionnels renforce la priorité
de ces items.

---

## 8. Journal des décisions

| Date | Décision | Motif | Items impactés |
|---|---|---|---|
| 2026-07-30 | Création du document de suivi | Consolidation de deux audits indépendants | tous |
| — | *(à compléter : choix cascade / anonymisation)* | | `EVA-002` |
| — | *(à compléter : `gguf` officiel vs parseur interne)* | | `EVA-034` |
| — | *(à compléter : licence de l'outil d'estimation VRAM)* | | `EVA-034` |

---

### Notes de tenue du document

- Un item passe à `☑` **uniquement** quand son critère de validation est couvert par un test
  automatisé — pas quand le code est écrit.
- Les items marqués 🔬 ont été reproduits en exécution : ils ne se discutent pas, ils se
  corrigent.
- Les items 🧭 relèvent d'un arbitrage : ils peuvent légitimement passer en `⊘` avec une
  justification écrite dans le journal.
