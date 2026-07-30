# EVARuntime — plan de consolidation et parcours automatisé jusqu'au premier token

> Document de pilotage vivant
> Créé le 30 juillet 2026
> Périmètre : `gateway`, `gateway-student`, `node_agent`, déploiement et exploitation
> État initial : audit en lecture seule, 433 tests réussis, aucun test réel GPU/GGUF

---

## 0. État d'avancement de l'implémentation

> **Cette section est l'en-tête de suivi vivant.** Elle est mise à jour à chaque
> lot livré. Le reste du document (§1 à §17) reste le plan de référence et ne
> change pas, sauf pour passer un marqueur d'état.

### 0.1 Situation

| Champ | Valeur |
|---|---|
| Dernière mise à jour | 2026-07-30 |
| Phase | **Jalon M0 atteint** — les 8 items livrés et vérifiés |
| Jalon visé | **M0 — socle fonctionnel fiable** (§13) |
| Branche de travail | `feat/lot-a-m0-invariants` (créée depuis `dev` @ `49f8d59`) |
| Base de référence | `dev` @ `49f8d59` |
| Périmètre livré | COR-001, COR-002, COR-004 → COR-007, COR-014, AUT-012, OPS-006, SEC-008, TST-001 |
| Prochain jalon | **M1 — planificateur de bootstrap** (§13), ou Lot D sécurité selon arbitrage |

### 0.2 Base de référence des tests et état courant

| Suite | Commande | Référence | État courant |
|---|---|---:|---:|
| `gateway` | `cd gateway && .venv/bin/python -m pytest tests -q` | 309 | **632** 🔬 |
| `gateway-student` | `cd gateway-student && .venv/bin/python -m pytest tests -q` | 79 | **138** 🔬 |
| `node_agent` | `cd node_agent && .venv/bin/python -m pytest tests -q` | 45 | **45** 🔬 |
| **Total** | — | **433** | **815** 🔬 |

Cette base est le point de non-régression : aucune livraison ne doit la faire
baisser, et chaque item livré doit l'augmenter du nombre de ses régressions.
**+382 tests** ajoutés par le jalon M0, tous des régressions rouges avant
correctif et vertes après. Les scripts de déploiement passent `bash -n`.
`shellcheck` et `ruff` ne sont installés dans aucun environnement du dépôt : ce
sont des lacunes d'outillage, à traiter avec EVA-044.

### 0.3 Avancement par lot

| Lot | Items | `[x]` Fait | `[~]` En cours | `[ ]` À faire | `[!]` Bloqué |
|---|---:|---:|---:|---:|---:|
| A — bloqueurs et invariants | 14 | 7 | 0 | 7 | 0 |
| B — bootstrap automatisé | 13 | 1 | 0 | 12 | 0 |
| C — performance | 8 | 0 | 0 | 8 | 0 |
| D — sécurité et supply-chain | 8 | 1 | 0 | 7 | 0 |
| E — tests et exploitation | 12 | 1 | 0 | 11 | 0 |
| **Total** | **55** | **10** | **0** | **45** | **0** |

Deux items ont été **ajoutés au backlog** pendant l'implémentation, d'où 55 au
lieu de 53 : COR-014 (Lot A) et SEC-008 (Lot D). Voir §0.8.

Les **8 items du jalon M0 sont terminés et vérifiés.**

### 0.4 Vague en cours — jalon M0

Les items sont regroupés par propriété de fichiers, de façon à ce que deux
chantiers menés en parallèle ne se marchent jamais dessus.

| Vague | ID | État | Commit | Tests ajoutés |
|---|---|---|---|---:|
| 1 | COR-001 | `[x]` | `8417bed` | +13 |
| 1 | OPS-006 | `[x]` | `63c46b6` | +14 / +16 |
| 1 | COR-005 | `[x]` | `0f62556` | +39 |
| 1 | COR-007 | `[x]` | `90fda1c` | +12 |
| 1 | COR-004 | `[x]` | `fe76ea0` | +24 |
| 2 | COR-002 | `[x]` | `27e4c2c` | +35 / +30 |
| 2 | COR-002 | `[x]` | `7a035ec` (dashboard) | — |
| 2 | AUT-012 | `[x]` | `0efa5a7` | +103 |
| — | SEC-008 | `[x]` | `04f4433` | +13 |
| — | COR-014 | `[x]` | `06b50af` | +25 / +13 |
| 3 | COR-006 | `[x]` | `0102760` | +45 |

Les deux lignes sans numéro de vague sont les items **ajoutés en cours de
route** : ils n'étaient pas planifiés, ils sont nés d'une découverte.

Chaque chantier est mené dans un **worktree git isolé** et produit un commit
atomique sur sa propre branche, ensuite fusionnée dans la branche de lot. Deux
chantiers ne peuvent donc jamais se marcher dessus, même sur un fichier partagé.

TST-001 n'est pas un chantier séparé : chaque item ci-dessus livre ses propres
tests de régression, qui doivent échouer avant le correctif et passer après.

### 0.5 Décisions tranchées

| ID | Décision | Retenu le |
|---|---|---|
| DEC-001 | **Anonymisation** pour la suppression d'utilisateur : la ligne `users` est conservée, les données personnelles sont effacées, `is_active = 0`, les clés sont révoquées. L'historique d'usage agrégé reste exploitable, la personne n'est plus ré-identifiable. Écarté : `ON DELETE CASCADE` (perte de traçabilité de facturation) et `SET NULL` (casse les jointures des rapports). | 2026-07-30 |

Les décisions DEC-002 à DEC-008 restent `[!]` : elles ne bloquent pas le jalon
M0 et seront tranchées à l'entrée des lots concernés.

### 0.6 Conventions d'implémentation appliquées

- **Une branche par lot**, commits atomiques : un commit par ID de backlog,
  message au format `type(portée): ID — description`.
- **Aucun item ne passe `[x]` sans test automatisé** qui échoue avant le
  correctif et passe après. Code écrit mais non testé reste `[~]` (§1).
- **Migration obligatoire** dès qu'une contrainte de schéma change : un
  `CREATE TABLE IF NOT EXISTS` n'atteint jamais une base existante (OPS-006).
- **Documentation dans le même commit** que le changement de comportement (§14).
- Les deux suites de tests ne sont **jamais** lancées dans le même processus
  pytest : `gateway` et `gateway-student` partagent des noms de modules de
  premier niveau (`config`, `database`, `auth`, `schemas`).

### 0.7 Journal des livraisons

| Date | ID | Commit | Validation | Résultat |
|---|---|---|---|---|
| 2026-07-30 | — | — | Base de référence revérifiée sur `dev` @ `49f8d59` | 433 tests réussis 🔬 |
| 2026-07-30 | COR-001 | `8417bed` | `pytest tests -q` (gateway) | 322 réussis, `/admin/status` = 200 en local **et** cluster 🔬 |
| 2026-07-30 | OPS-006 | `63c46b6` | `pytest tests -q` (gateway + student) | 336 / 95 réussis, migrations versionnées et transactionnelles 🔬 |
| 2026-07-30 | COR-005 | `0f62556` | `pytest tests -q` (gateway) | 375 réussis, binaire ou GGUF absent → 503 🔬 |
| 2026-07-30 | COR-007 | `90fda1c` | `pytest tests -q` (gateway) | 387 réussis, invariant profil ↔ limite systemd verrouillé 🔬 |
| 2026-07-30 | COR-004 | `fe76ea0` | `pytest tests -q` (gateway) | 411 réussis, 5 routes admin protégées, 409 sur modèle occupé 🔬 |
| 2026-07-30 | — | — | Les 3 suites sur la branche cumulée, fin de vague 1 | **551 réussis** (411 / 95 / 45) 🔬 |
| 2026-07-30 | COR-002 | `27e4c2c` | `pytest tests -q` (gateway + student) | 446 / 125 réussis, anonymisation avec clés et usage, sans changer la contrainte FK 🔬 |
| 2026-07-30 | COR-002 | `7a035ec` | Relecture du dashboard | Le dashboard annonce une anonymisation, plus une suppression 📖 |
| 2026-07-30 | SEC-008 | `04f4433` | `pytest tests -q` (gateway) | 459 réussis, aucun nom d'utilisateur dans les journaux 🔬 |
| 2026-07-30 | AUT-012 | `0efa5a7` | `pytest tests -q` (gateway) | 562 réussis, diagnostic humain et JSON, exit codes stables 🔬 |
| 2026-07-30 | COR-014 | `06b50af` | `pytest tests -q` (gateway + student) | 587 / 138 réussis, les fichiers d'exemple livrés sont chargeables 🔬 |
| 2026-07-30 | COR-006 | `0102760` | `pytest tests -q` (gateway) + `bash -n` | 632 réussis, un flux sans contenu est un échec, rollback fonctionnel 🔬 |
| 2026-07-30 | **M0** | `0102760` | Les 3 suites + syntaxe des 9 scripts de déploiement | **815 réussis** (632 / 138 / 45), jalon atteint 🔬 |

### 0.7.1 Sortie du jalon M0

Conditions de §13 et leur état :

| Condition M0 | État |
|---|---|
| COR-001, COR-002 et COR-004 à COR-007 terminés | `[x]` — plus COR-014, découvert en route |
| AUT-012 et OPS-006 terminés | `[x]` |
| Toutes les régressions P0 présentes | `[x]` — 382 tests ajoutés, chacun rouge avant correctif |
| Readiness et rollback fiables | `[x]` — readiness structurelle stricte (COR-005) et rollback adossé à une génération réelle (COR-006) |
| Aucune opération admin ne tue silencieusement une requête | `[x]` — 5 routes protégées, drain borné puis 409 (COR-004) |

**Décision de sortie prononcée** : le travail de bootstrap (Lot B) peut démarrer
sur une base correcte.

Ce que M0 ne prétend **pas** avoir démontré, et qui reste vrai depuis l'audit
initial : aucun test n'a été exécuté contre un GPU réel, un vrai `llama-server`
ou un GGUF réel. La logique de décision du smoke test est testée contre un faux
serveur HTTP ; le parcours physique jusqu'au premier token reste à faire
(TST-004, AUT-009). Le niveau de preuve de COR-007 reste un jugement
d'architecture `🧭`, à remplacer par la calibration AUT-008.

### 0.8 Défauts découverts pendant l'implémentation, absents des deux audits

L'implémentation a mis au jour des défauts que ni l'audit Codex ni l'audit Claude
n'avaient relevés. Ils sont corrigés dans le commit de l'item qui les a trouvés.

| Découverte | Trouvé par | Traitement |
|---|---|---|
| `GatewayStatus` **supprimait silencieusement** des champs émis par les managers : `nodes`/`nodes_online` en cluster, `gpu_used_mb_measured`/`vram_drift_mb` en local. La réconciliation VRAM par `nvidia-smi`, pourtant consommée par le dashboard, était donc **morte** sur `/admin/status` — sans erreur, sans log. | COR-001 | Corrigé 📖 |
| `ReadWritePaths=/models /data/models` sans préfixe tolérant, combiné à `ProtectSystem=strict`, **empêche le démarrage** de l'unité si le répertoire est absent. Or `install.sh` ne crée que `/models`, alors que deux entrées de `models.yaml` pointent vers `/data/models`. | COR-007 | Corrigé en `-/models -/data/models` 📖 |
| `node_agent/deploy/llm-gateway-agent.service` n'avait **aucune limite mémoire**, alors que c'est son cgroup qui héberge les `llama-server` en mode cluster. Le durcissement mémoire ne portait que sur la topologie locale. | COR-007 | Politique mémoire ajoutée 📖 |
| `POST /admin/unload` présentait le même défaut que les routes de COR-004 en mode local : drain de 25 s **puis forçage**, donc requêtes actives tuées. La route n'était pas listée dans l'item. | COR-004 | Inclus dans le correctif 📖 |
| Les tests `/ready` existants passaient alors que **ni le binaire `llama-server` ni les GGUF n'existaient** dans l'environnement de test : ils n'exerçaient que le contrat de capacité. La permissivité de la sonde était donc verrouillée par les tests. | COR-005 | Tests adossés à un environnement structurellement sain 🔬 |
| `_split_statements()` du moteur de migration découpait sur `;` **avant** de retirer les commentaires : un point-virgule de ponctuation dans un commentaire français tronquait silencieusement l'instruction `CREATE TABLE` qui l'entourait. | COR-002, sur le code livré par OPS-006 | Commentaires retirés avant découpage, dans les deux composants 🔬 |
| Les journaux conservaient une copie du nom d'utilisateur, ce qui **vide de son effet** l'anonymisation RGPD : le middleware d'accès journalisait `request.url.path` (donc `/admin/users/<username>`), et `admin.create_key` journalisait le nom en clair. Deux fuites distinctes — la seconde a été trouvée par le test écrit pour la première. | COR-002, puis le test de SEC-008 | Nouvel item **SEC-008**, corrigé 🔬 |
| **Trois réglages de liste étaient inutilisables tels que documentés** : pydantic-settings décode un champ `list[str]` comme du JSON dans la source d'environnement, avant tout validateur. `ALLOWED_MODEL_DIRS` échouait au format CSV documenté **et** à la valeur vide livrée dans `.env.example` — copier ce fichier vers `/etc/llm-gateway/env` produisait un service mort. Le validateur `split_cors_origins` de `CORS_ALLOW_ORIGINS` ne s'exécutait jamais. Côté student, `deploy/env.example` livre `ALLOWED_MODELS` en CSV et il n'y a pas d'installateur : le déploiement documenté ne démarrait pas. Reproduit sur les deux sources, dont celle de la production. | AUT-012, reproduit puis élargi au student | Nouvel item **COR-014**, corrigé 🔬 |

Conséquence de la dernière ligne pour SEC-002 : l'allowlist `ALLOWED_MODEL_DIRS`
n'était pas seulement absente de l'environnement généré — elle était
**impossible à activer**. Les deux audits avaient relevé l'absence, pas
l'impossibilité. SEC-002 devient réalisable.

### 0.9 Points d'exploitation à connaître après la vague 1

| Sujet | Conséquence | Suite |
|---|---|---|
| `eva_vram_total_gb` en mode cluster passe du budget net à la VRAM physique des nœuds en ligne, pour aligner la sémantique sur le mode local. L'invariant `total_gb - overhead_gb == budget_net_gb` est désormais vrai dans les deux modes. | Un tableau de bord Grafana s'appuyant sur cette métrique en cluster change d'échelle. | À signaler à l'exploitation ; converge avec PERF-002 |
| `/ready` exige que **tous** les modèles `enabled: true` aient un GGUF présent et lisible. | Sur un hôte incomplet, `/ready` = 503 et `update.sh` déclenche le rollback. Le message propose explicitement `enabled: false`. | Comportement voulu (COR-005) ; sera adouci par le préchauffage AUT-010 |
| `minimax-m2.7` passe à `enabled: false` : ~236 Go de RAM hôte résidente en `cpu_moe`, incompatible avec tout `MemoryMax` raisonnable sur l'hôte de référence. | Le modèle n'est plus servi par défaut. Procédure de réactivation documentée (≥ 320 Go de RAM). | Décision assumée `🧭`, à revalider par la calibration AUT-008 |
| Les sauvegardes `*.pre-migration.*.bak` produites par OPS-006 ne sont **pas purgées** et ne sont connues ni de `.gitignore` ni des scripts de sauvegarde. | Accumulation d'une copie par changement de version de schéma. | À traiter dans OPS-002 |
| `ruff` n'est installé dans **aucun** des trois venv : aucun chantier ne peut exécuter le lint annoncé par la CI. | Le style est vérifié à la main. | À traiter avec EVA-044 (élargir `ruff`) |
| `SystemCallFilter=~@resources` bloque `set_mempolicy`/`mbind`/`sched_setaffinity` : `--numa distribute`, utile aux MoE `cpu_moe` bi-socket, est inaccessible. | Aucun modèle bloqué aujourd'hui (`--numa` non utilisé), mais un `llama-server` récent qui appellerait `sched_setaffinity` serait tué par `SIGSYS`. | Filtre conservé, conflit documenté, à valider en staging |
| Le timeout nginx de `/admin/` est de **30 s contre 310 s réellement requis** — et non 190 s comme estimé par l'audit Claude : `gemma-4-26b-a4b`, activé, porte `load_timeout_seconds: 300`. Les blocs `/v1/*` (600 s) suffisent aujourd'hui mais deviendraient trop courts si `minimax-m2.7` était réactivé (610 s requis). | Le pré-chargement d'un modèle depuis le dashboard échoue en 504 alors que le chargement réussit côté serveur. | Détecté et signalé par `doctor` ; le correctif appartient à COR-009 |
| `TOTAL_VRAM_GB=48.0` posé par `install.sh` contre **~45,0 Go réellement exposés** par une L40S : le nominal commercial n'est pas la VRAM utilisable. | Non bloquant — l'overhead de 2 Go et la marge de 5 % absorbent l'écart — mais la marge réelle est rognée d'environ 3 Go. | Avertissement `doctor` ; à reprendre dans AUT-002 (inventaire matériel) |
| Il n'existe **aucun entrypoint `evaruntime`** dans le dépôt (pas de `console_scripts`) : la commande réelle est `python cli.py doctor`. | Le nom « `evaruntime doctor` » employé par ce document reste aspirationnel. | Un item de packaging pourra l'exposer sans changer `doctor` |

---

## 1. Objet du document

Ce document transforme les audits techniques en plan d'implémentation suivi. Il
doit servir de source de vérité pour :

- corriger les écarts qui bloquent la production ;
- automatiser au maximum le parcours machine vierge → premier token ;
- mesurer la performance au lieu de la supposer ;
- standardiser la distribution de `llama-server` et des modèles GGUF ;
- conserver une trace des décisions, preuves et critères d'acceptation ;
- préparer un déploiement pilote, puis une production contrôlée.

Ce document ne remplace pas `docs/deployment.md`. Ce dernier reste le manuel
d'exploitation. Ici, chaque action doit être suivie jusqu'à une preuve
reproductible.

### Convention de suivi

| Marqueur | Signification |
|---|---|
| `[ ]` | À faire |
| `[~]` | En cours |
| `[x]` | Terminé et vérifié |
| `[!]` | Bloqué ou décision requise |

Chaque constat peut également porter un niveau de preuve :

| Marqueur | Niveau de preuve |
|---|---|
| `🔬` | Reproduit en exécution |
| `📖` | Établi par lecture du code ou de la configuration |
| `🧭` | Jugement d'architecture ou décision produit à confirmer |

Quand une ligne passe à `[x]`, ajouter :

1. le lien vers le commit ou la PR ;
2. la commande de validation ;
3. le résultat observé ;
4. la date ;
5. le nom de la personne ayant validé.

Une fonctionnalité implémentée mais non testée reste `[~]`, pas `[x]`.

## 2. Décision actuelle

EVARuntime est adapté à un staging contrôlé, mais pas encore à une production
sans surveillance.

Les choix fondamentaux sont bons : FastAPI, SQLite WAL, processus
`llama-server` possédés par la gateway, séparation de la gateway étudiante,
registre de modèles, queue de capacité, pin/unpin, orchestration cluster légère,
systemd et nginx.

La priorité n'est pas une réécriture. Elle est de fermer les écarts entre les
invariants annoncés, les tests simulés et le comportement réellement déployé.

### Référence initiale vérifiée

| Contrôle | Résultat initial |
|---|---|
| Tests gateway | 309 réussis |
| Tests gateway-student | 79 réussis |
| Tests node_agent | 45 réussis |
| Total | 433 réussis |
| Scripts de déploiement | Syntaxe valide |
| Parcours install/update simulé | Local et cluster validés |
| Dépendances installées | `pip check` réussi |
| Test réel GPU | Non exécuté |
| Test réel `llama-server` | Non exécuté |
| Test réel GGUF → premier token | Non exécuté |
| Audit CVE local complet | Non exécuté |

## 3. Analyse du rapport produit par l'autre IA

### Verdict

Le rapport est bon, utile et techniquement au-dessus d'un audit superficiel. Il
contient plusieurs découvertes valides que le premier audit doit conserver.
Cependant, son verdict est trop optimiste et son plan de priorité est incomplet.
Il ne doit pas être utilisé seul comme feu vert de production.

Appréciation globale : **bon rapport d'investigation, environ 7,5/10 ; pas encore
un audit de readiness de production complet**.

### Points solides et confirmés

| Constat du rapport | Avis Codex |
|---|---|
| Assets externes dans le dashboard admin | Confirmé. Chart.js et Google Fonts sont chargés depuis des CDN, tandis que le secret admin est accessible au JavaScript de la page. Le risque supply-chain et le problème air-gap sont réels. |
| Timeout nginx de 30 s sur le chargement admin | Confirmé. Les gros modèles peuvent charger pendant plusieurs minutes. Dire que cela échoue pour « tout modèle réel » est trop absolu, mais le défaut est certain pour les modèles lents. |
| Variables de durcissement absentes de l'environnement généré | Confirmé pour `ALLOWED_MODEL_DIRS`, `CORS_ALLOW_ORIGINS` et `LLAMA_SERVER_MIN_BUILD`. Le niveau de risque de CORS doit néanmoins être nuancé : CORS n'est pas une frontière de sécurité serveur. |
| Fuite possible de concurrence student avant démarrage du générateur | Confirmé comme scénario crédible. `acquire()` précède la création effective du stream et aucun TTL ne récupère un slot abandonné. Ajouter un test de régression. |
| Multiplication des connexions `aiosqlite` | Confirmé dans le chemin chaud. Le coût exact annoncé en millisecondes n'est toutefois pas mesuré. Une connexion unique sérialisée pourrait devenir un autre goulot ; comparer connexion persistante, writer dédié et petit pool. |
| Buffering intégral en présence de `tools` | Confirmé. Le TTFT devient proche du temps de génération complet. |
| Silence pendant un cold start | Confirmé. La gateway ne retourne pas la `StreamingResponse` avant la fin de `ensure_model_loaded()`. |
| Dépendances non verrouillées | Confirmé. Les installations ne sont pas reproductibles et `pytest` est dans les dépendances runtime de la gateway étudiante. |
| Wildcards SQL dans `revoke_key()` | Confirmé techniquement. `%` et `_` ne sont pas échappés. C'est surtout un risque de mauvaise opération par un administrateur déjà privilégié, pas une élévation de privilèges distante. |
| Faible couverture de certains chemins difficiles | Crédible et cohérent avec les tests présents. Les pourcentages exacts du rapport ne sont pas reproductibles actuellement car la CI ne publie pas de rapport de couverture et `coverage` n'est pas installé dans l'environnement audité. |
| Qualité des invariants, du scheduler et du client cluster | Confirmé. Ces parties sont parmi les meilleures du projet. |

### Corrections proposées par le rapport à nuancer

#### Streaming avec tools

« Bufferiser jusqu'au premier delta `content` ou `tool_calls`, puis passer en
passthrough » ne préserve pas nécessairement le contrat actuel. Un modèle peut
émettre du contenu, puis annoncer un tool call plus tard. Une fois le contenu
envoyé au client, il n'est plus possible de le retirer.

Il faut choisir explicitement entre :

- conserver le buffering complet et accepter le TTFT ;
- imposer un mode de sortie où le modèle ne mélange jamais contenu et tools ;
- utiliser un parseur/contrat de tool calling déterministe ;
- limiter le buffer à un préfixe avec un comportement documenté si un tool call
  apparaît tard ;
- désactiver la suppression du contenu et assumer la compatibilité du client.

#### Heartbeat SSE pendant le chargement

Envoyer des commentaires SSE pendant le chargement est possible, mais oblige à
retourner HTTP 200 avant de savoir si le modèle chargera. En cas d'échec, la
gateway ne peut plus renvoyer un vrai statut HTTP 503 OpenAI-compatible.

La stratégie recommandée est :

1. préchauffer automatiquement le modèle avant d'accepter le trafic ;
2. exposer un chargement asynchrone `202 + job_id + progression` ;
3. conserver le heartbeat uniquement comme option pour les requêtes froides,
   avec son compromis documenté.

#### Connexion SQLite unique

Une connexion persistante réduit les créations de threads, mais sérialise les
requêtes. Le changement doit être précédé d'un benchmark. Le meilleur compromis
probable est un writer dédié et un petit nombre de connexions de lecture
persistantes.

#### Délais annoncés

« Une demi-journée » pour les cinq premiers points est trop optimiste si l'on
inclut tests, migration, documentation, validation nginx et non-régression. Les
estimations doivent être faites par lot après conception, et non à partir du seul
nombre de lignes à changer.

### Lacunes importantes du rapport

Le rapport externe ne relève pas plusieurs bloqueurs plus graves :

1. `/admin/status` ne respecte pas son response model en cluster ;
2. la suppression d'un utilisateur échoue dans les deux gateways après création
   d'un log d'usage ;
3. le déchargement administratif local peut tuer une requête active ;
4. `/ready` peut annoncer prêt sans binaire ni GGUF utilisable ;
5. le rollback d'update fait confiance à cette readiness insuffisante ;
6. la compatibilité de `MemoryMax=64G` avec le profil MiniMax de 248 Go n'est
   pas démontrée et paraît très improbable sans forte dégradation ;
7. les erreurs auth/quota de la gateway principale ne conservent pas toujours
   l'enveloppe OpenAI ;
8. `/completion` est implémenté et documenté mais bloqué par nginx ;
9. les métriques de type counter représentent une fenêtre glissante et peuvent
    diminuer ;
10. le data-plane cluster transportant les prompts reste en HTTP ;
11. le SHA-256 d'un très gros modèle peut bloquer l'event loop du node agent.

### Conclusion sur ce rapport

Il faut conserver ses constats spécifiques, mais fusionner son plan avec le
backlog ci-dessous. Son affirmation selon laquelle le cycle de vie est déjà au
niveau production doit être considérée comme prématurée tant que les opérations
administratives ne respectent pas le pinning et que les chemins réels ne sont pas
testés de bout en bout.

### Relecture croisée de `claude-analyse-projet.md`

Le document Claude est de très bonne qualité. Il reprend les défauts reproduits,
les transforme en items stables et ajoute des critères de validation beaucoup
plus actionnables qu'une simple liste de recommandations.

Sa correction la plus importante est juste : dans le code actuel, il n'existe
pas de course asyncio entre le retour de `ensure_model_loaded()` et le premier
`manager.pin()`. Les quelques instructions intermédiaires sont synchrones et ne
contiennent aucun `await`; une autre coroutine ne peut donc pas s'intercaler sur
ce segment. Un lease déjà pinné reste une bonne défense contre une future
régression, mais ce n'est pas un P0 actuel. Le vrai défaut reste l'unload
administratif local, qui ignore un pin déjà posé.

Apports intégrés dans ce document :

- marqueurs de niveau de preuve `🔬`, `📖` et `🧭` ;
- préflight `evaruntime doctor` réutilisable par install et update ;
- détection GPU directement exploitable par le bootstrap ;
- inspection du header GGUF avant la calibration ;
- migrations SQLite versionnées comme capacité de plateforme ;
- instrumentation avant toute optimisation de performance ;
- distinction plus précise entre limite RAM, mmap réclamable et risque de
  thrashing ;
- critères de validation plus concrets pour le smoke test.

Points du document Claude qui ne doivent pas être repris tels quels :

- le header GGUF permet une meilleure estimation, pas un calcul exact de la
  VRAM : allocations du backend, buffers de graph, fragmentation et version du
  runtime ne sont connus qu'à l'exécution ;
- les archives natives officielles Linux ne proposent pas nécessairement une
  variante CUDA ; les images officielles CUDA existent, sinon il faut un build
  local ou un artefact EVARuntime ;
- un heartbeat SSE pendant le chargement échange un meilleur retour utilisateur
  contre la perte d'un vrai statut HTTP d'erreur ;
- le test cité dans `test_admin_routes.py` documente la seconde révocation, pas
  le wildcard `%`, même si le wildcard SQL est bien un défaut réel ;
- « tous les tests sont exclusivement unitaires » est trop absolu : il existe
  des tests d'intégration en mémoire, mais aucun E2E avec runtime externe réel ;
- les pourcentages de couverture annoncés restent à rendre reproductibles en CI.

## 4. Parcours cible : machine vierge → premier token

### Objectif utilisateur

Pour une plateforme officiellement supportée, l'opérateur devrait pouvoir
exécuter une commande de ce type :

```bash
sudo ./gateway/deploy/install.sh --bootstrap \
  --use-case chat \
  --quality balanced \
  --model-source huggingface
```

La commande doit :

1. auditer le matériel et le système ;
2. proposer un plan sans modifier l'hôte ;
3. demander uniquement les consentements impossibles à automatiser ;
4. installer un runtime compatible et versionné ;
5. recommander un modèle autorisé ;
6. télécharger et vérifier ses fichiers ;
7. générer une configuration prudente ;
8. lancer et calibrer le modèle ;
9. effectuer un appel complet via l'API publique ;
10. afficher le TTFT, le débit, les versions et le rapport final.

Le mode recommandé doit rester en deux temps :

```bash
eva-bootstrap plan --use-case chat --quality balanced
eva-bootstrap apply /var/lib/llm-gateway/plans/<plan-id>.json
```

`install.sh --bootstrap` peut orchestrer ces deux commandes, mais le plan JSON
doit rester inspectable avant toute écriture ou téléchargement massif.

### Ce qui peut être automatisé

- détection CPU, architecture, instructions, RAM et espace disque ;
- détection GPU, VRAM, driver, backend et topologie multi-GPU ;
- choix du profil de `llama-server` ;
- récupération d'un artefact officiel ou construction reproductible ;
- vérification de version, empreinte et provenance ;
- recommandation du modèle et de sa quantification ;
- prévision de RAM, VRAM, disque et temps de téléchargement ;
- téléchargement reprenable des fichiers GGUF et mmproj ;
- génération de `models.yaml` ;
- configuration du budget VRAM de la machine ;
- calibration après chargement réel ;
- préchauffage du modèle par défaut ;
- création d'une identité de smoke test éphémère ;
- génération d'un premier token via nginx/gateway/llama-server ;
- production d'un rapport sans secrets.

### Ce qui doit rester une décision humaine

- DNS, choix du domaine et certificats TLS ;
- acceptation des licences de modèles et conditions des dépôts gated ;
- authentification Hugging Face pour un modèle gated ;
- politique de conservation des prompts et journaux ;
- sélection des réseaux autorisés et des administrateurs ;
- exposition Internet ou réseau privé ;
- choix final des SLO et du compromis qualité/coût ;
- autorisation de télécharger plusieurs dizaines ou centaines de Go.

TLS reste hors automatisation initiale. Le bootstrap doit seulement vérifier que
les fichiers fournis existent, sont lisibles par nginx, correspondent au domaine
et ne sont pas expirés.

### Écart actuel à fermer

| Étape actuelle | Risque manuel | Cible |
|---|---|---|
| Installer ou compiler `llama-server` | Mauvais backend, build ou options GPU | Résolveur de runtime versionné |
| Télécharger GGUF et mmproj | Mauvais fichier, licence ou chemin | Catalogue approuvé + téléchargement vérifié |
| Saisir la VRAM physique | Mauvaise unité ou mauvais GPU | Inventaire matériel automatique |
| Estimer `vram_gb` | OOM ou capacité gaspillée | Estimation header/LLMfit + calibration réelle |
| Configurer TLS et DNS | Dépend de la PKI de l'organisation | Décision humaine + validation automatique |
| Créer une clé et tester | Test incomplet ou secret conservé | Identité éphémère + recette E2E |

## 5. Architecture recommandée pour l'automatisation

### Séparer le bootstrap privilégié du choix de modèles

Conserver deux couches :

1. `install.sh` : utilisateurs système, répertoires, venv, systemd, nginx,
   permissions, sauvegardes ;
2. `eva-bootstrap` : inventaire matériel, runtime, catalogue, licence, modèles,
   calibration et smoke test.

Cette séparation évite qu'un outil de recommandation réseau tourne inutilement
avec tous les privilèges root.

### `evaruntime doctor`

Le node agent possède déjà un préflight solide. Il faut en dériver une commande
équivalente pour la gateway principale, exécutable :

- manuellement avant le premier démarrage ;
- par `install.sh` avant activation ;
- par `update.sh` avant et après bascule ;
- par l'opérateur lors d'un incident.

Le diagnostic doit vérifier au minimum :

- permissions des fichiers de secrets et de la base ;
- absence de valeurs placeholder ;
- binaire exécutable, version lisible et politique de build ;
- GPU, driver, backend et budget VRAM ;
- présence, lisibilité et intégrité des GGUF/mmproj ;
- cohérence des modèles activés avec RAM, VRAM et espace disque ;
- disponibilité du pool de ports ;
- accès en écriture à SQLite et aux répertoires de logs ;
- cohérence des timeouts nginx avec les timeouts de chargement ;
- certificats fournis, sans tenter de les émettre ;
- limites systemd compatibles avec les profils activés.

La sortie doit exister en format humain et JSON, avec un exit code stable. Aucun
secret, token Hugging Face ou chemin sensible non nécessaire ne doit apparaître
dans le rapport.

### Pipeline proposé

```text
Inventaire matériel
        ↓
Résolution du runtime
        ↓
Recommandation LLMfit
        ↓
Filtre catalogue EVARuntime + politique de licence
        ↓
Plan signé/inspectable
        ↓
Téléchargements à révisions figées
        ↓
Vérification SHA-256 + espace disque
        ↓
Registre généré, modèle encore désactivé
        ↓
Chargement de calibration
        ↓
Budget RAM/VRAM ajusté
        ↓
Activation + préchauffage
        ↓
Test nginx → gateway → llama-server → premier token
        ↓
Rapport de recette
```

### Données d'inventaire minimales

Le profil matériel doit contenir :

```json
{
  "os": "ubuntu",
  "os_version": "24.04",
  "arch": "x86_64",
  "cpu_model": "...",
  "cpu_flags": ["avx2"],
  "ram_total_bytes": 0,
  "ram_available_bytes": 0,
  "disk_available_bytes": 0,
  "gpus": [
    {
      "uuid": "...",
      "vendor": "nvidia",
      "model": "...",
      "vram_total_bytes": 0,
      "driver_version": "...",
      "compute_capability": "..."
    }
  ],
  "backend_candidates": ["cuda12", "vulkan", "cpu"]
}
```

Ne jamais demander à l'opérateur de saisir manuellement la VRAM physique si elle
est détectable. Une option `--hardware-profile` doit exister pour les VM,
passthrough ou systèmes où les outils constructeur échouent.

Pour NVIDIA, une première source structurée est :

```bash
nvidia-smi \
  --query-gpu=index,uuid,name,memory.total,driver_version,compute_cap \
  --format=csv,noheader,nounits
```

La valeur utilisée pour `TOTAL_VRAM_GB` doit être calculée à partir des devices
réellement exposés dans `CUDA_VISIBLE_DEVICES`, pas de tous les GPU de l'hôte.

## 6. Distribution de llama-server

### Réalité à prendre en compte

Il n'existe pas un seul binaire optimal pour toutes les machines. Les variantes
dépendent au minimum de :

- système et architecture ;
- CPU et jeux d'instructions ;
- NVIDIA CUDA 12/13 ;
- AMD ROCm ;
- Vulkan, SYCL, Metal ou CPU ;
- version du driver ;
- dépendances dynamiques disponibles.

Distribuer un « binaire universel » conduirait soit à de mauvaises performances,
soit à des incompatibilités.

### Stratégie de résolution

Ordre recommandé :

1. artefact natif officiel `llama.cpp` si le backend visé existe ;
2. image officielle GHCR épinglée par digest si un mode conteneur est accepté ;
3. artefact EVARuntime construit en CI depuis un tag/commit officiel figé ;
4. build local reproductible et mis en cache ;
5. refus explicite si aucune variante sûre ne correspond.

Ne jamais basculer silencieusement d'un backend GPU vers CPU. Ce fallback peut
faire croire que l'installation fonctionne tout en produisant un TTFT
inacceptable.

### Cas NVIDIA Linux

Le projet officiel publie des images `server-cuda` et `server-cuda13`, mais les
archives natives de release Linux ne couvrent pas nécessairement CUDA. Deux
voies sont donc réalistes :

- ajouter un backend conteneur au gestionnaire de serveurs ;
- publier des artefacts CUDA EVARuntime issus d'une CI reproductible.

À court terme, le build local depuis un tag officiel figé est plus lent mais
respecte l'architecture actuelle de subprocessus natifs. À moyen terme, une
matrice d'artefacts EVARuntime est préférable pour réduire le temps
d'installation.

### Manifeste de provenance obligatoire

Chaque runtime installé doit avoir un manifeste :

```yaml
runtime:
  project: ggml-org/llama.cpp
  version: "bNNNNN"
  commit: "<sha>"
  source: "official-release|official-container|evaruntime-build|local-build"
  backend: "cuda12"
  platform: "linux-x86_64"
  artifact_sha256: "<sha256>"
  container_digest: null
  build_options:
    GGML_CUDA: true
  installed_at: "<ISO-8601>"
```

`LLAMA_SERVER_MIN_BUILD` doit être généré à partir de la politique de release.
Si une version minimale est exigée mais illisible, la validation production doit
échouer en mode fail-closed.

### Obligations de licence

`llama.cpp` est sous licence MIT. La redistribution est adaptée au projet à
condition de conserver la licence et les notices. Les dépendances embarquées
doivent être inventoriées séparément.

Pour des artefacts EVARuntime :

- conserver la licence MIT de `llama.cpp` ;
- produire `THIRD_PARTY_NOTICES`;
- publier la source exacte ou le commit et les options de build ;
- produire un SBOM ;
- signer les sommes ou attestations de provenance ;
- vérifier l'artefact avant installation.

## 7. Intégration de LLMfit

### Décision recommandée

**Adopter LLMfit comme moteur de recommandation optionnel, par CLI versionnée et
sortie JSON. Ne pas réimplémenter son moteur et ne pas le forker initialement.**

LLMfit :

- détecte RAM, CPU, GPU et backend ;
- gère plusieurs GPU et les architectures MoE ;
- recommande dynamiquement une quantification ;
- produit `recommend --json` pour les scripts ;
- expose un mode REST pour les schedulers ;
- sait mesurer TTFT et tokens/s contre `llama-server` ;
- est distribué sous licence MIT.

La licence est donc compatible en principe. Il faut conserver la licence et le
copyright lors d'une redistribution. L'analyse juridique des modèles reste
totalement séparée : la licence MIT de LLMfit n'accorde aucun droit sur les
poids qu'il référence.

### Pourquoi utiliser la CLI

- faible couplage avec le code Rust interne ;
- mise à jour ou rollback indépendants ;
- validation du schéma JSON à la frontière ;
- possibilité de désactiver LLMfit et d'utiliser un profil manuel ;
- aucune nouvelle implémentation de calcul mémoire à maintenir ;
- possibilité de comparer ses estimations aux mesures EVARuntime.

La version et le SHA-256 du binaire LLMfit doivent être figés dans le manifeste
du bootstrap. Éviter `curl | sh` dans le chemin de production.

### LLMfit reste un conseiller, pas une autorité

Ses estimations sont une excellente présélection, mais elles ne connaissent pas
nécessairement :

- tous les paramètres EVARuntime ;
- le coût exact de `ctx_size × parallel` ;
- les caches K/V sélectionnés ;
- le comportement exact de `cpu_moe` ;
- l'empreinte des projecteurs multimodaux ;
- la fragmentation VRAM ;
- les autres modèles chargés simultanément ;
- les contraintes systemd de l'hôte.

Le choix doit donc suivre cette règle :

```text
recommandation LLMfit
  + modèle approuvé par le catalogue EVARuntime
  + estimation conservatrice
  + chargement réel de calibration
  = modèle activable
```

## 8. Catalogue de modèles EVARuntime

Ne pas laisser le bootstrap télécharger arbitrairement le premier dépôt GGUF
retourné par une heuristique.

Créer un catalogue maintenu, distinct du registre opérationnel :

```yaml
catalog_version: 1
models:
  - id: "example-8b-q4"
    family: "example-8b"
    use_cases: ["chat"]
    source:
      provider: "huggingface"
      repo_id: "owner/repository"
      revision: "<commit-sha>"
      files:
        - name: "model-q4_k_m.gguf"
          sha256: "<sha256>"
    license:
      id: "apache-2.0"
      url: "https://..."
      gated: false
      redistribution_allowed: true
      operator_acceptance_required: true
    runtime:
      min_llama_build: 0
      capabilities: ["text_generation", "streaming"]
      defaults:
        ctx_size: 8192
        parallel: 1
        cache_type_k: "q8_0"
        cache_type_v: "q8_0"
    resources:
      disk_gb: 0
      initial_vram_gb: 0
      initial_ram_gb: 0
```

Le catalogue doit distinguer :

- la licence du logiciel de téléchargement ;
- la licence du modèle de base ;
- la licence du fine-tune ;
- les éventuelles conditions d'utilisation ;
- le droit de redistribution du GGUF ;
- le statut gated ;
- la révision exacte et les fichiers attendus.

Hugging Face expose des métadonnées de licence dans les model cards, mais leur
présence ne constitue pas une validation juridique. Un modèle sans licence
identifiable doit être refusé par défaut.

### Téléchargement

Utiliser le client officiel `huggingface_hub`/`hf` avec :

- révision figée ;
- liste exacte de fichiers ;
- reprise de téléchargement ;
- prévision de l'espace disque ;
- token via variable d'environnement ou credential store, jamais dans argv ;
- téléchargement vers un nom temporaire ;
- vérification SHA-256 ;
- renommage atomique ;
- manifeste de provenance.

Les fichiers split GGUF et `mmproj` doivent être traités comme un ensemble
indivisible.

## 9. Estimation et calibration automatiques de RAM/VRAM

### Deux valeurs différentes

Il ne faut pas confondre :

1. la VRAM physique disponible sur la machine ;
2. `vram_gb` du registre, estimation du coût d'un modèle avec ses paramètres.

La première est détectable automatiquement. La seconde dépend de la
quantification, du contexte, du parallélisme, du cache KV, de l'offload et de la
version de `llama.cpp`.

### Calcul initial

Le bootstrap doit combiner :

- inventaire matériel ;
- estimation LLMfit ;
- taille des fichiers ;
- paramètres `ctx_size`, `parallel`, batch et cache KV ;
- marge fixe runtime/driver ;
- marge de sécurité configurable ;
- RAM nécessaire pour le mmap et les experts CPU.

Le registre initial doit rester conservateur. Un modèle ne doit pas être activé
si sa capacité estimée dépasse le budget net.

### Inspection du header GGUF

Le document Claude propose à juste titre d'exploiter les métadonnées GGUF au
lieu de déduire la mémoire depuis le seul nom du modèle. Les champs utiles
incluent notamment :

| Métadonnée | Utilité |
|---|---|
| Architecture et nombre de blocs | Choix de la formule et coût par couche |
| Dimensions d'embedding et têtes KV | Estimation du cache KV, y compris GQA/MQA |
| Contexte maximal déclaré | Refus d'un `ctx_size` incohérent |
| Types, dimensions et tailles des tenseurs | Estimation des poids réellement offloadables |
| Métadonnées MoE | Détection des experts et besoin potentiel en RAM hôte |

Le paquet Python officiel
[`gguf`](https://github.com/ggml-org/llama.cpp/tree/master/gguf-py), maintenu
dans `llama.cpp` et déclaré sous licence MIT, doit être évalué avant d'écrire un
parseur interne. Il peut fournir une inspection structurée et suivre les
évolutions du format.

Cette inspection ne donne toutefois pas une empreinte VRAM exacte. Restent
dépendants du runtime et du matériel :

- placement réel des couches et des experts ;
- buffers de calcul et graphes CUDA ;
- implémentation de Flash Attention ;
- batch, ubatch et parallélisme effectifs ;
- fragmentation et allocations du driver ;
- version et options de build de `llama.cpp`.

La hiérarchie correcte est donc :

```text
header GGUF + paramètres + estimation LLMfit
  → estimation conservatrice
  → chargement réel
  → mesure des pics
  → valeur de capacité approuvée
```

### Calibration réelle

Pour chaque profil supporté :

1. relever RAM/VRAM au repos ;
2. charger le modèle avec un contexte réduit ;
3. relever les pics RAM/VRAM ;
4. effectuer un prompt court ;
5. répéter au contexte et parallélisme cibles ;
6. conserver le maximum observé ;
7. ajouter une marge ;
8. écrire les mesures dans un rapport séparé ;
9. proposer, sans l'appliquer silencieusement, une nouvelle valeur `vram_gb`.

Le rapport doit conserver :

```yaml
calibration:
  model_id: "..."
  runtime_version: "..."
  hardware_fingerprint: "..."
  params_fingerprint: "..."
  idle_vram_gb: 0
  peak_vram_gb: 0
  peak_ram_gb: 0
  load_seconds: 0
  ttft_ms: 0
  prompt_tokens_per_second: 0
  generation_tokens_per_second: 0
  tested_at: "..."
```

Une mesure n'est réutilisable que si matériel, runtime et paramètres sont
compatibles.

## 10. Recette automatique du premier token

### Le premier token doit traverser le vrai chemin public

Le test final doit couvrir :

```text
client
  → TLS/nginx si configuré
  → authentification gateway
  → quota/rate limit
  → résolution du modèle
  → llama-server
  → chunk SSE contenant du contenu
  → log d'usage
```

Un simple `/health` ou `/ready` ne suffit pas.

### Séquence

1. vérifier la liveness ;
2. vérifier la readiness structurelle ;
3. créer un utilisateur et une clé de smoke test éphémères ;
4. charger explicitement le modèle ;
5. appeler `/v1/chat/completions` avec `stream: true` ;
6. mesurer le temps jusqu'aux headers ;
7. mesurer le temps jusqu'au premier delta utile ;
8. attendre `[DONE]` ;
9. vérifier l'enveloppe, le modèle et l'usage ;
10. vérifier l'écriture du log ;
11. révoquer la clé et supprimer/anonymiser l'utilisateur ;
12. produire un rapport sans clé ni contenu sensible.

Cette recette dépend de la correction de la suppression utilisateur.

### Readiness à trois niveaux

| Route/état | Signification |
|---|---|
| Liveness | Le processus répond. |
| Structural readiness | Configuration, DB, binaire, répertoires et au moins un modèle activé sont valides. |
| Serving readiness | Un modèle est chargé ou un smoke load récent a prouvé qu'il peut l'être. |

Le déploiement peut utiliser la structural readiness pour décider si le process
est correctement installé, mais le feu vert production doit exiger la serving
readiness ou un smoke test explicite.

### Pré-chauffage

Après installation ou mise à jour :

- charger le modèle par défaut avant d'ouvrir le trafic ;
- attendre sa health réelle ;
- exécuter une génération courte ;
- ne déclarer la version déployée qu'après succès ;
- conserver l'ancienne version tant que la recette n'est pas terminée.

Cela améliore beaucoup plus le premier token utilisateur qu'un heartbeat pendant
un chargement de dix minutes.

## 11. Indicateurs de performance obligatoires

### Mesures par requête

- temps d'admission/queue ;
- temps de chargement froid ;
- temps jusqu'à la connexion upstream ;
- TTFT vu par la gateway ;
- TTFT vu par le client externe ;
- prompt tokens/s ;
- generation tokens/s ;
- durée totale ;
- nombre de tokens ;
- état chaud/froid ;
- modèle, paramètres et nœud ;
- cause d'erreur ou d'annulation.

### SLO à définir par modèle

Les objectifs ne doivent pas être globaux pour un 8B et un modèle de 248 Go.

| SLO | Valeur cible | Mesure actuelle | Statut |
|---|---:|---:|---|
| Disponibilité API | À décider | Inconnue | `[ ]` |
| Taux d'erreurs 5xx | À décider | Inconnu | `[ ]` |
| TTFT chaud p50/p95 | Par modèle | Inconnu | `[ ]` |
| TTFT froid p50/p95 | Par modèle | Inconnu | `[ ]` |
| Tokens/s p50/p95 | Par modèle | Inconnu | `[ ]` |
| Temps de queue p95 | À décider | Inconnu | `[ ]` |
| Taux de cold start | À décider | Inconnu | `[ ]` |
| Perte de comptabilisation | 0 | Non prouvée | `[ ]` |

### Scénarios de benchmark

- un client, modèle chaud ;
- clients concurrents jusqu'à `parallel` ;
- au-delà de `parallel` ;
- modèle froid ;
- alternance de modèles provoquant une éviction ;
- contexte court, moyen et maximal ;
- tools sans tool call ;
- tools avec tool call tardif ;
- client qui interrompt le stream ;
- perte d'un node agent ;
- quota proche de la limite ;
- endurance 24 h puis 72 h.

### Ordre d'optimisation

L'ordre proposé par Claude est pertinent à condition de ne pas considérer le
heartbeat comme la solution principale au cold start :

```text
1. Instrumenter TTFT, chargement, queue et annulations
2. Capturer une baseline versionnée
3. Automatiser préchauffage et smoke test
4. Benchmarker puis optimiser SQLite
5. Décider le contrat tools/streaming
6. Inspecter GGUF et calibrer RAM/VRAM
7. Rejouer exactement les mêmes scénarios
```

Les seuils de performance ne doivent pas provoquer automatiquement un rollback
tant qu'ils ne sont pas stabilisés. Le succès fonctionnel est un hard gate ; une
régression TTFT est d'abord une alerte ou un gate configuré séparément, afin
d'éviter les boucles de rollback dues à une machine momentanément chargée.

## 12. Backlog priorisé

### Lot A — correction des bloqueurs et invariants

| ID | État | Priorité | Action | Critère d'acceptation |
|---|---|---|---|---|
| COR-001 | `[x]` | P0 | Aligner le statut cluster sur `GatewayStatus` | `/admin/status` retourne 200 en local et cluster, avec validation du response model. |
| COR-002 | `[x]` | P0 | Définir et migrer la politique de suppression utilisateur | Un utilisateur avec clés et usage peut être supprimé/anonymisé conformément à la politique, dans les deux gateways. |
| COR-003 | `[ ]` | P2 | Formaliser admission + pin sous forme de lease | Hardening préventif : aucun futur `await` ou refactor ne peut ouvrir une fenêtre d'éviction. Ce n'est pas un bug reproduit dans le code actuel. |
| COR-004 | `[x]` | P0 | Protéger unload/update/delete contre les requêtes actives | Drain borné, conflit explicite ou job différé ; aucun stream actif tué silencieusement. |
| COR-005 | `[x]` | P0 | Renforcer `/ready` | Binaire ou modèle absent → non-ready ; test local et cluster. |
| COR-006 | `[x]` | P0 | Utiliser un vrai smoke test pour update/rollback | Une version incapable de générer n'est jamais validée. |
| COR-007 | `[x]` | P0 | Aligner limites RAM/systemd sur les profils | Aucun modèle approuvé n'est incompatible avec `MemoryMax`; profil MiniMax explicitement traité. |
| COR-008 | `[ ]` | P1 | Uniformiser les erreurs OpenAI | Auth, quota, chargement et upstream renvoient tous `{"error": ...}` sans double enveloppe. |
| COR-009 | `[ ]` | P1 | Aligner les routes nginx et FastAPI | Chaque route documentée est exposée ou supprimée de la documentation. |
| COR-010 | `[ ]` | P1 | Corriger `revoke_key()` | `%` et `_` sont littéraux ou rejetés ; seconde révocation a un comportement défini. |
| COR-011 | `[ ]` | P1 | Corriger le slot student abandonné | Un générateur jamais démarré ne bloque jamais durablement l'utilisateur. |
| COR-012 | `[ ]` | P1 | Définir une réservation de quota | Les requêtes concurrentes ne dépassent pas silencieusement le budget, ou le dépassement maximal accepté est borné et documenté. |
| COR-013 | `[ ]` | P1 | Préserver les erreurs upstream avant SSE | Une 4xx/5xx de `llama-server` ne devient pas un HTTP 200 ambigu ; contrat d'erreur streaming testé. |
| COR-014 | `[x]` | P0 | Rendre chargeables les réglages de liste de l'environnement | `ALLOWED_MODEL_DIRS`, `CORS_ALLOW_ORIGINS` et `ALLOWED_MODELS` acceptent la syntaxe documentée sans faire échouer le démarrage ; les fichiers d'exemple livrés sont chargeables. **Item ajouté le 2026-07-30**, découvert par AUT-012 : ces trois réglages étaient inutilisables tels que documentés. |

### Lot B — bootstrap automatisé

| ID | État | Priorité | Action | Critère d'acceptation |
|---|---|---|---|---|
| AUT-001 | `[ ]` | P0 | Définir le schéma du plan de bootstrap | Schéma versionné, validé, sans secret, lisible avant application. |
| AUT-002 | `[ ]` | P0 | Inventaire matériel automatique | CPU/RAM/disque/GPU/VRAM/driver/backend détectés et testés sur la matrice supportée. |
| AUT-003 | `[ ]` | P0 | Résolveur `llama-server` | Sélection versionnée, SHA vérifié, aucun fallback CPU silencieux. |
| AUT-004 | `[ ]` | P1 | Adapter LLMfit JSON | Version figée, schéma validé, timeout, fallback manuel et tests avec sorties enregistrées. |
| AUT-005 | `[ ]` | P0 | Créer le catalogue approuvé | Révision, fichiers, SHA, licence, paramètres et ressources pour chaque modèle proposé. |
| AUT-006 | `[ ]` | P0 | Télécharger les modèles de façon sûre | Reprise, espace disque, fichier temporaire, SHA, renommage atomique et provenance. |
| AUT-007 | `[ ]` | P1 | Générer `models.yaml` | Entrée désactivée tant que la calibration et le smoke test n'ont pas réussi. |
| AUT-008 | `[ ]` | P1 | Calibrer RAM/VRAM | Mesures avant/après, pics et marge enregistrés par fingerprint. |
| AUT-009 | `[ ]` | P0 | Recette premier token | Appel public complet avec TTFT mesuré et rapport sans secrets. |
| AUT-010 | `[ ]` | P1 | Pré-chauffer le modèle par défaut | Le premier utilisateur ne déclenche pas le chargement après un déploiement réussi. |
| AUT-011 | `[ ]` | P1 | Produire le rapport d'installation | Versions, empreintes, licences, matériel, modèle, performances et contrôles. |
| AUT-012 | `[x]` | P0 | Ajouter `evaruntime doctor` | Rapport humain/JSON et exit codes couvrant secrets, runtime, GPU, modèles, ports, DB, nginx, TLS fourni et limites systemd. |
| AUT-013 | `[ ]` | P1 | Inspecter les métadonnées GGUF | Architecture, tenseurs, contexte et KV alimentent une estimation conservatrice sans être présentés comme une mesure exacte. |

### Lot C — performance

| ID | État | Priorité | Action | Critère d'acceptation |
|---|---|---|---|---|
| PERF-001 | `[ ]` | P0 | Instrumenter TTFT, load et queue | Histogrammes par modèle/nœud, sans cardinalité non bornée. |
| PERF-002 | `[ ]` | P1 | Corriger la sémantique Prometheus | Counters monotones ou gauges clairement nommées. |
| PERF-003 | `[ ]` | P1 | Évaluer le chemin SQLite | Benchmark avant/après ; choix justifié entre pool, writer et connexion persistante. |
| PERF-004 | `[ ]` | P1 | Décider le contrat tools + streaming | Comportement documenté, tests client Vercel/OpenAI et TTFT mesuré. |
| PERF-005 | `[ ]` | P1 | Chargement admin asynchrone | `202 + job/progression`, annulation et résultat final observables. |
| PERF-006 | `[ ]` | P1 | Profiler chaque modèle approuvé | Baseline chaud/froid/concurrent versionnée. |
| PERF-007 | `[ ]` | P1 | Tester nginx derrière NAT campus | Limites par IP compatibles avec la concurrence réelle. |
| PERF-008 | `[ ]` | P2 | Brancher ou supprimer `cleanup_stale()` | Les structures des rate limiters ont une politique de purge testée, ou le caractère borné de leur croissance est explicitement assumé. |

### Lot D — sécurité et supply-chain

| ID | État | Priorité | Action | Critère d'acceptation |
|---|---|---|---|---|
| SEC-001 | `[ ]` | P0 | Vendoriser les assets admin et ajouter CSP | Dashboard fonctionnel hors ligne, aucune ressource tierce, CSP testée. |
| SEC-002 | `[ ]` | P0 | Durcir l'environnement généré | Allowlist modèle, CORS explicite et build minimum visibles dans le fichier généré. |
| SEC-003 | `[ ]` | P0 | Verrouiller les dépendances | Installation reproductible, dépendances dev séparées, hashes ou artefacts contrôlés. |
| SEC-004 | `[ ]` | P0 | Rendre l'audit CVE bloquant | Politique d'exception documentée avec expiration. |
| SEC-005 | `[ ]` | P1 | Imposer l'intégrité des modèles approuvés | Aucun modèle catalogue ne charge sans SHA/provenance. |
| SEC-006 | `[ ]` | P1 | Sécuriser le data-plane cluster | Prompts chiffrés ou réseau isolé attesté et contrôlé. |
| SEC-007 | `[ ]` | P1 | Produire SBOM et attestations runtime | Chaque release et binaire redistribué possède provenance et notices. |
| SEC-008 | `[x]` | P1 | Ne pas journaliser les noms d'utilisateur | Aucun `log.*` de la gateway ne porte de nom d'utilisateur ; le chemin de requête est rédigé. **Item ajouté le 2026-07-30**, découvert en livrant COR-002 : anonymiser en base est sans effet si le journal garde une copie du nom. |

### Lot E — tests, exploitation et standardisation

| ID | État | Priorité | Action | Critère d'acceptation |
|---|---|---|---|---|
| TST-001 | `[ ]` | P0 | Ajouter les régressions des COR applicables | Chaque défaut reproduit échoue avant correctif et passe après ; COR-003 est testé comme invariant préventif, pas présenté comme reproduction d'un bug actuel. |
| TST-002 | `[ ]` | P1 | Ajouter couverture et seuil CI | Rapport publié, seuil initial réaliste puis remonté progressivement. |
| TST-003 | `[ ]` | P1 | Tester CLI et lifespan | Chemins opérateur et startup/shutdown couverts. |
| TST-004 | `[ ]` | P0 | Test réel petit GGUF | `llama-server` réel et petit modèle dans une recette dédiée. |
| TST-005 | `[ ]` | P1 | Test E2E cluster | Gateway → agent → llama → stream, erreur et failover. |
| OPS-001 | `[ ]` | P0 | Profils systemd/nginx par matériel | Timeouts et mémoire dérivés du plan, valeurs validées en staging. |
| OPS-002 | `[ ]` | P1 | Sauvegarde hors hôte et restore drill | Restauration chronométrée et documentée. |
| OPS-003 | `[ ]` | P1 | Releases immuables | Déploiement d'un tag/artefact, pas d'une branche mouvante. |
| OPS-004 | `[ ]` | P1 | Corrélation des requêtes | Même request ID dans gateway, agent et backend. |
| OPS-005 | `[ ]` | P1 | Automatiser la gateway étudiante | Install/update/backup/retention avec placeholders interdits. |
| OPS-006 | `[x]` | P0 | Versionner les migrations SQLite | `PRAGMA user_version` ou équivalent, migration transactionnelle, sauvegarde préalable et test depuis chaque version supportée. |
| OPS-007 | `[ ]` | P1 | Définir la rétention d'audit student | Rétention temporelle vérifiable, rotation et espace disque cohérents avec la politique. |

## 13. Jalons

### M0 — socle fonctionnel fiable

Conditions :

- COR-001, COR-002 et COR-004 à COR-007 terminés ;
- AUT-012 et OPS-006 terminés ;
- toutes les régressions P0 présentes ;
- readiness et rollback fiables ;
- aucune opération admin ne tue silencieusement une requête.

Décision de sortie : autoriser le travail de bootstrap sur une base correcte.

### M1 — planificateur de bootstrap

Conditions :

- inventaire matériel ;
- résolution runtime en mode dry-run ;
- LLMfit intégré par JSON ;
- catalogue initial de deux petits modèles permissifs ;
- plan inspectable sans écriture.

Décision de sortie : le système sait expliquer ce qu'il installera et pourquoi.

### M2 — installation jusqu'au premier token

Conditions :

- runtime installé et vérifié ;
- modèle téléchargé à révision figée ;
- licence acceptée ;
- calibration effectuée ;
- modèle préchauffé ;
- appel E2E réussi ;
- rapport final produit.

Décision de sortie : « une commande » sur une plateforme officiellement
supportée.

### M3 — performance mesurée

Conditions :

- TTFT/load/queue/tokens/s instrumentés ;
- baselines par modèle ;
- charge et déconnexions testées ;
- contrat tools/streaming décidé ;
- profils systemd/nginx calibrés.

Décision de sortie : publication de SLO réalistes.

### M4 — pilote de production

Conditions :

- sécurité supply-chain P0 terminée ;
- audit CVE bloquant ;
- sauvegarde restaurée lors d'un exercice ;
- endurance 24 h réussie ;
- panne d'un nœud testée ;
- runbooks incident/rollback validés.

### M5 — production standardisée

Conditions :

- endurance 72 h ;
- SLO atteints ;
- observabilité et alertes validées ;
- release immuable et SBOM ;
- revue de sécurité indépendante ;
- approbation de l'exploitation.

## 14. Definition of Done commune

Une entrée de backlog n'est terminée que si :

- le comportement est documenté ;
- les erreurs sont explicites et exploitables ;
- les tests positifs, négatifs et concurrents pertinents existent ;
- les tests du composant passent ;
- la CI couvre le nouveau chemin ;
- aucune donnée sensible n'est journalisée ;
- les chemins local et cluster sont cohérents ou l'écart est documenté ;
- les scripts d'installation/update sont synchronisés ;
- les métriques nécessaires existent ;
- un rollback ou une désactivation sûre est possible.

## 15. Décisions à prendre

| ID | État | Décision |
|---|---|---|
| DEC-001 | `[!]` | Politique RGPD : suppression en cascade, anonymisation ou conservation légale des usages. |
| DEC-002 | `[!]` | Plateformes officiellement supportées en premier : probablement Ubuntu x86_64 + NVIDIA CUDA 12. |
| DEC-003 | `[!]` | Runtime natif uniquement ou ajout d'un backend conteneur officiel. |
| DEC-004 | `[!]` | Licences de modèles autorisées et droit de redistribution des GGUF. |
| DEC-005 | `[!]` | Modèles de bootstrap par défaut, idéalement petits et non gated. |
| DEC-006 | `[!]` | Contrat attendu lorsque contenu et tool calls sont mélangés. |
| DEC-007 | `[!]` | SLO par modèle et nombre de clients simultanés. |
| DEC-008 | `[!]` | Réseau de confiance cluster ou chiffrement mTLS du data-plane. |

## 16. Sources techniques vérifiées le 30 juillet 2026

- [Dépôt officiel LLMfit](https://github.com/AlexsJones/llmfit) — détection
  matérielle, recommandation JSON, MoE, benchmark TTFT/tokens/s et licence MIT.
- [Licence LLMfit](https://github.com/AlexsJones/llmfit/blob/main/LICENSE) — MIT.
- [CLI et automatisation LLMfit](https://github.com/AlexsJones/llmfit/blob/main/docs/cli.md)
  — `recommend --json`, overrides matériels et API de scheduling.
- [Fonctionnement de LLMfit](https://github.com/AlexsJones/llmfit/blob/main/docs/how-it-works.md)
  — sources des estimations et limites.
- [Dépôt officiel llama.cpp](https://github.com/ggml-org/llama.cpp) — releases,
  installation, exécution `-hf` et serveur OpenAI-compatible.
- [Licence llama.cpp](https://github.com/ggml-org/llama.cpp/blob/master/LICENSE) —
  MIT.
- [Paquet Python officiel gguf](https://github.com/ggml-org/llama.cpp/tree/master/gguf-py)
  — lecture structurée des métadonnées et tenseurs GGUF, licence MIT.
- [Images officielles llama.cpp](https://github.com/ggml-org/llama.cpp/blob/master/docs/docker.md)
  — profils server CPU, CUDA, ROCm, Vulkan, SYCL et autres.
- [Client officiel Hugging Face](https://huggingface.co/docs/huggingface_hub/en/guides/cli)
  — téléchargement à révision donnée, dry-run, reprise et authentification.
- [Métadonnées des model cards](https://github.com/huggingface/hub-docs/blob/main/modelcard.md)
  — champs de licence et provenance disponibles.
- [Licence huggingface_hub](https://github.com/huggingface/huggingface_hub/blob/main/LICENSE)
  — Apache-2.0.

## 17. Journal de suivi

| Date | Auteur | Modification | Preuve |
|---|---|---|---|
| 2026-07-30 | Codex | Création du plan consolidé, analyse du rapport externe et architecture du bootstrap | Audit local, 433 tests, sources officielles listées ci-dessus |
| 2026-07-30 | Codex | Relecture de `claude-analyse-projet.md`; correction du statut de la supposée course pin, ajout de doctor, inspection GGUF, migrations et ordre de travail performance | Lecture intégrale du document Claude, vérification du segment asyncio et du paquet officiel `gguf` |
