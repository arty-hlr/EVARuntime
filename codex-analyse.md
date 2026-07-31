# EVARuntime — plan de consolidation et parcours automatisé jusqu'au premier token

> Document de pilotage vivant
> Créé le 30 juillet 2026
> Périmètre : `gateway`, `node_agent`, déploiement et exploitation
> État initial : audit en lecture seule, 433 tests réussis, aucun test réel GPU/GGUF

---

## 0. État d'avancement de l'implémentation

> **Cette section est l'en-tête de suivi vivant : lisez-la en premier.** Elle
> répond en trente secondes à « où en est-on, qu'est-ce qui tourne, qu'est-ce
> qui vient ensuite ». Elle est mise à jour à chaque lot livré. Le reste du
> document (§1 à §17) est le **plan de référence** : il ne change pas, sauf pour
> passer un marqueur d'état sur une ligne de backlog.

### 0.0 En un coup d'œil

| | |
|---|---|
| **Où en est-on** | Jalon **M1 atteint** (§0.12). Le système sait désormais **expliquer ce qu'il installerait et pourquoi**, sans rien appliquer : `python cli.py bootstrap-plan` rend un plan versionné, validé, sans secret et lisible avant application. M0 était atteint, le premier déploiement réel sur deux VMs avait révélé 6 défauts invisibles en CI (§0.10), tous fermés (§0.11) |
| **Ce qui vient ensuite** | Jalon **M2 — installation jusqu'au premier token** (§13) : c'est l'exécution du plan que M1 sait maintenant écrire. Lot D sécurité reste l'alternative selon arbitrage. **Plus aucun P0 ouvert dans les Lots A et B** |
| **Santé des tests** | **1257 tests verts** (1194 gateway + 63 node_agent), `ruff` propre et `bash -n` propre sur les deux composants |
| **Reste à faire** | 36 items sur 63 (§0.3). Aucun P0 ouvert hors Lots C, D et E |
| **Ce qui n'est toujours pas démontré** | Aucun test contre un **GPU réel** : la VRAM reste déclarative. Le parcours physique jusqu'au premier token a été exercé sur CPU avec de vrais GGUF (§0.10), pas sur GPU. Le plan de bootstrap n'a **jamais été appliqué** — c'est M2, pas M1 |

**Comment lire la suite** : §0.1 à §0.4 donnent l'état chiffré, §0.5 les
décisions tranchées, §0.7 le journal des livraisons, §0.8, §0.10 et §0.12 les
défauts trouvés en implémentant et en déployant — ceux qu'aucun des deux audits
n'avait vus —, §0.9 ce que l'exploitation doit savoir.

### 0.1 Situation

| Champ | Valeur |
|---|---|
| Dernière mise à jour | 2026-07-31 |
| Phase | **Vague 5 livrée** — le planificateur de bootstrap existe et est verrouillé par ses régressions (§0.12) |
| Jalon atteint | **M1 — planificateur de bootstrap** (§13), sortie prononcée (§0.12.1). M0 atteint le 2026-07-30 (§0.7.1) |
| Jalon visé | **M2 — installation jusqu'au premier token** (§13) |
| Branche de travail | `feat/lot-b-vague5-planificateur-bootstrap` (créée depuis `feat/lot-a-vague4-retours-deploiement` @ `9af8172`) |
| Base de référence | `9af8172` — 758 tests verts, `ruff` propre |
| Périmètre livré à ce jour | AUT-001 → AUT-005, AUT-012, AUT-013, COR-001, COR-002, COR-004 → COR-007, COR-009, COR-014 → COR-017, OPS-006, OPS-008, OPS-009, SEC-008, TST-001, TST-006 |
| Périmètre de la vague 5 | AUT-001 → AUT-005 et AUT-013 — **6 items, tous `[x]`** — plus SEC-009, né de la vague (§0.12) |
| Prochain jalon | **M2 — installation jusqu'au premier token** (§13), ou Lot D sécurité selon arbitrage |

### 0.2 Base de référence des tests et état courant

| Suite | Commande | Référence | État courant |
|---|---|---:|---:|
| `gateway` | `cd gateway && .venv/bin/python -m pytest tests -q` | 309 | **1194** 🔬 |
| `node_agent` | `cd node_agent && .venv/bin/python -m pytest tests -q` | 45 | **63** 🔬 |
| **Total** | — | **354** | **1257** 🔬 |

Cette base est le point de non-régression : aucune livraison ne doit la faire
baisser, et chaque item livré doit l'augmenter du nombre de ses régressions.
**+324 tests** ajoutés par le jalon M0, **+80 par la vague 4**, puis **+499 par
la vague 5**, tous des régressions rouges avant correctif et vertes après. Les
scripts de déploiement passent `bash -n`.

> **Retrait du composant `gateway-student` (DEC-009, 30 juillet 2026).** Les
> totaux ci-dessus perdent mécaniquement les 79 tests de référence et 138 tests
> courants de ce composant. Le journal §0.7 conserve les chiffres tels qu'ils
> étaient au moment de chaque livraison.

`ruff` **est** installé dans les deux venv du dépôt et passe au vert sur
`gateway` comme sur `node_agent` — le constat inverse de §0.9 date de la vague 1
et n'est plus vrai. `shellcheck` reste absent : la syntaxe des scripts est
vérifiée par `bash -n`, à compléter avec EVA-044.

### 0.3 Avancement par lot

| Lot | Items | `[x]` Fait | `[~]` En cours | `[ ]` À faire | `[–]` Annulé |
|---|---:|---:|---:|---:|---:|
| A — bloqueurs et invariants | 17 | 11 | 0 | 5 | 1 |
| B — bootstrap automatisé | 13 | 7 | 0 | 6 | 0 |
| C — performance | 8 | 0 | 0 | 8 | 0 |
| D — sécurité et supply-chain | 9 | 1 | 0 | 8 | 0 |
| E — tests et exploitation | 16 | 4 | 1 | 9 | 2 |
| **Total** | **63** | **23** | **1** | **36** | **3** |

Dix items ont été **ajoutés au backlog** après coup, d'où 63 au lieu de 53 :
COR-014 (Lot A) et SEC-008 (Lot D) pendant l'implémentation (§0.8), puis
COR-015 à COR-017 (Lot A) et TST-006, OPS-008, OPS-009 (Lot E) lors du premier
déploiement réel sur deux VMs (§0.10), **OPS-010** (Lot E) né de la vague 4,
enfin **SEC-009** (Lot D) né de la vague 5 (§0.12).

Les **8 items du jalon M0**, les **7 de la vague 4** et les **6 de la vague 5**
sont terminés et vérifiés. Le seul `[~]` restant est **OPS-010**, dont la part
venvs est livrée et testée mais dont la borne sur les sauvegardes
`*.pre-migration.*.bak` reste à poser, sous OPS-002.

**Il ne reste aucun P0 ouvert dans les Lots A et B.** Les 5 items restants du
Lot A (COR-003, COR-008, COR-010, COR-012, COR-013) sont en P1/P2 ; les 6 du
Lot B (AUT-006 → AUT-011) relèvent du jalon M2, c'est-à-dire de l'**exécution**
du plan que la vague 5 sait désormais écrire.

### 0.4 Vagues 1 à 3 — jalon M0 (historique)

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
| DEC-009 | **Suppression du composant `gateway-student`** : jamais déployé, aucun consommateur. Code, tests, documentation, unités systemd/nginx/nftables et job CI retirés du dépôt. Vérifié sans effet fonctionnel — ni `gateway` ni `node_agent` n'en dépendaient (aucun import, aucune clé `llmstu-*`, aucune route ni base partagée). Les items de backlog exclusivement student (**COR-011**, **OPS-005**, **OPS-007**) sont annulés, ainsi que **EVA-006** et **EVA-023** côté `claude-analyse-projet.md`. Écarté : garder le composant en l'état (surface de maintenance, CI et audit de dépendances pour du code mort). | 2026-07-30 |

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
  pytest : `gateway` et `node_agent` partagent des noms de modules de
  premier niveau (`config`, `main`).

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
| 2026-07-30 | TST-006 | `40a22f4` | `pytest tests -q` (gateway) + rougeur rejouée par l'orchestrateur | 638 réussis ; `n.node_id` réintroduit → 2 échecs sur `AttributeError` 🔬 |
| 2026-07-30 | COR-015 | — | Clôturé par TST-006, correctif déjà présent sur `dev` | Passe `[~]` → `[x]` 🔬 |
| 2026-07-30 | COR-016 | `5a080a2` | `pytest tests -q` (node_agent) + rougeur rejouée | 56 réussis ; `mv` de venv réintroduit → 3 échecs, shebang pointant sur le staging supprimé 🔬 |
| 2026-07-30 | COR-017 | `5fe61d8`, `8961607` | `pytest tests -q` (gateway + node_agent) + rougeur rejouée | 652 / 56 réussis ; `reset-failed` retiré → 4 échecs, dont le rollback laissé en indisponibilité 🔬 |
| 2026-07-30 | OPS-008 | `ad953bc` | `pytest tests -q` (gateway) | Timer armé avec `--now`, après l'initialisation de la base 🔬 |
| 2026-07-30 | COR-009 | `0c2efcb` | `pytest tests -q` (gateway) | 649 réussis ; timeouts dérivés du registre (900 s), `/ready` et `/completion` exposés 🔬 |
| 2026-07-30 | OPS-009 | `28acf1c`, `66140b1` | `pytest tests -q` (gateway) + rougeur rejouée | 685 réussis ; HTTP/2 rendu selon `nginx -v`, seuil inversé → 4 échecs 🔬 |
| 2026-07-30 | OPS-010 | `d58b365` | `pytest tests -q` (gateway + node_agent) | Rétention bornée des venvs, cible du symlink protégée ; rouge en retirant cette protection 🔬 |
| 2026-07-30 | **Vague 4** | `532a5da` | Les 2 suites + `bash -n` sur les 12 scripts de déploiement | **758 réussis** (695 / 63), 6 défauts terrain fermés + OPS-010 🔬 |
| 2026-07-31 | AUT-001 | `5a8271e`, `380feff` | `pytest tests -q` (gateway) + 3 mutations rejouées | 750 réussis ; contrat du plan, 55 régressions, détecteur de secrets neutralisé → 8 échecs 🔬 |
| 2026-07-31 | AUT-002 | `e3516ee` | `pytest tests -q` (gateway) + 11 mutations rejouées | 798 réussis ; 11 mutants sur 11 tués, dont le filtrage `CUDA_VISIBLE_DEVICES` (7 échecs) 🔬 |
| 2026-07-31 | AUT-004 | `f5788c2` | `pytest tests -q` (gateway) + parité CI Python 3.11 | 896 réussis ; LLMfit absent → `skip`, épinglage version+SHA vérifié, timeout borné 🔬 |
| 2026-07-31 | AUT-003 | `54f61f6` | `pytest tests -q` (gateway) + 7 mutations rejouées | 964 réussis ; repli CPU marqué dégradé, manifeste incohérent refusé, clone superficiel reconnu 🔬 |
| 2026-07-31 | AUT-013, AUT-005 | `c349470`, `07534cd` | `pytest tests -q` (gateway) + empreintes recoupées | 1100 réussis ; header GGUF borné contre fichier hostile, catalogue fail-closed 🔬 |
| 2026-07-31 | AUT-001 | `1b77763` | `pytest tests -q` (gateway) + 3 mutations rejouées | Un constat `fail` bloque quelle que soit sa section ; `notes` rendues, `status_from_findings()` 🔬 |
| 2026-07-31 | AUT-001 | `976894d` | `pytest tests -q` (gateway) + CLI exercée sur 5 codes de sortie | **1139 réussis** ; plan assemblé de bout en bout, `bootstrap-plan` en JSON et en français 🔬 |
| 2026-07-31 | AUT-001 | `ca0871f` | `pytest tests -q` (gateway) + 5 codes de sortie rejoués | 1140 réussis ; faute de saisie (2) séparée de panne du planificateur (4) 🔬 |
| 2026-07-31 | — | `898088a` | Relecture de la documentation contre le code | `README`, `docs/admin.md` §9, `docs/architecture.md`, `docs/deployment.md` 📖 |
| 2026-07-31 | AUT-001, AUT-002, AUT-004 | `055eeea` | `pytest tests -q` (gateway) + 4 mutations rejouées | 1160 réussis ; cinq défauts de revue fermés, dont un plan bloqué qui décrivait 8 actions 🔬 |
| 2026-07-31 | AUT-001 | `8e25616` | `pytest tests -q` (gateway) + 2 mutations rejouées | 1180 réussis ; `--strict` bloque réellement, champs récapitulatifs typés et comparés 🔬 |
| 2026-07-31 | **M1** | `8e25616` | Les 2 suites + `ruff` sur les deux composants | **1243 réussis** (1180 / 63), jalon atteint 🔬 |
| 2026-07-31 | AUT-001 | `c7a25c6` | `pytest tests -q` (gateway) + reproduction ciblée | 1193 réussis ; les six compteurs refusent booléens, flottants, valeurs négatives et clés hors contrat 🔬 |
| 2026-07-31 | AUT-004 | `3fe1413` | Suite complète sous `GITHUB_ACTIONS=true`, puis sous Python 3.11 | **1194 réussis** ; échec CI-only fermé, un seul test dépendait de la colorisation 🔬 |

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
| **Trois réglages de liste étaient inutilisables tels que documentés** : pydantic-settings décode un champ `list[str]` comme du JSON dans la source d'environnement, avant tout validateur. `ALLOWED_MODEL_DIRS` échouait au format CSV documenté **et** à la valeur vide livrée dans `.env.example` — copier ce fichier vers `/etc/llm-gateway/env` produisait un service mort. Le validateur `split_cors_origins` de `CORS_ALLOW_ORIGINS` ne s'exécutait jamais. Reproduit sur les deux sources, dont celle de la production. | AUT-012, reproduit puis élargi | Nouvel item **COR-014**, corrigé 🔬 |

#### Découvertes de la vague 4

| Découverte | Trouvé par | Traitement |
|---|---|---|
| **Une branche d'erreur inatteignable dans `_build_manager()`** : le `RuntimeError` qui cite `settings.cluster_nodes_path` — le message le plus actionnable pour l'opérateur — ne s'affiche jamais, car `load_nodes_config()` lève un `ValueError` plus vague en amont. | TST-006 | Constat 📖 — à trancher : supprimer la branche morte ou unifier le message |
| **`doctor` est aveugle à la classe de défaut de COR-015** : il lit les nœuds via `getattr(n, "id", "?")`. Un renommage d'attribut n'y produirait aucune erreur — `doctor` afficherait `?, ?` et resterait au vert. L'outil censé détecter ce type de panne ne la voit pas. | TST-006 | Constat 📖 — angle mort de détection, à reprendre |
| **`chown -R` ne traverse pas un symlink** : une fois `venv-agent` devenu un lien, `install-agent.sh` n'aurait plus chowné la release réelle lors d'une réinstallation par-dessus une mise à jour. | COR-016 | Corrigé dans le même commit (`readlink -f` + `chown -h`) 📖 |
| **La branche `enabled` d'`update.sh` ne réparait rien** : sur un hôte déjà mis à jour, le timer est `enabled` mais `inactive`, et la branche se contentait d'un `info`. OPS-008 aurait donc survécu à *toutes* les mises à jour futures. Le critère d'acceptation de l'item ne tenait pas sans cette réparation, qu'il ne mentionnait pas. | OPS-008 | Corrigé 🔬 |
| **`rollback_failed_transaction` était pire qu'un `\|\| true`** : son `systemctl start` tournait sous `set +e` sans aucun garde, l'échec était avalé *silencieusement*, sans même l'intention qu'aurait signalée un `\|\| true`. | COR-017 | Corrigé 🔬 |
| **Armer le timer devenait un risque de rollback** : en `update.sh`, `systemctl enable` est sous `set -e` + trap ERR. Avec `--now`, un timer récalcitrant aurait pu déclencher le rollback transactionnel d'un déploiement sain. | OPS-008 | Armements placés en position de condition, verrouillé par un test 🔬 |
| **`http2 on;` casse les deux LTS supportées, pas seulement la plus ancienne** : Ubuntu 22.04 livre nginx 1.18 et 24.04 livre 1.24, or la directive n'existe qu'à partir de 1.25.1. L'audit initial ne relevait que la dépréciation, pas l'incompatibilité descendante. | OPS-009 | Rendu conditionnel à la version détectée 🔬 |
| **Debian 13 — l'hôte du premier déploiement réel — n'est pas dans la matrice de support** de `docs/deployment.md`, qui ne cite aucune distribution Debian. | OPS-009 | Constat 🧭 — décision produit, matrice à trancher |

Conséquence de la dernière ligne du premier tableau pour SEC-002 : l'allowlist `ALLOWED_MODEL_DIRS`
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

### 0.10 Premier déploiement réel sur deux VMs (2026-07-30)

Premier parcours complet joué de bout en bout hors CI, sur deux VM Debian 13
(4 vCPU, 3 Go RAM, **sans GPU**) : `install.sh --mode local` sur EvR-A, puis
`install-agent.sh` sur EvR-B, puis migration `update.sh --mode cluster
--allow-mode-change`, avec un `llama-server` **réellement compilé** (CPU,
`GGML_CUDA=OFF`) et deux **vrais GGUF** (Qwen2.5-0.5B-Instruct-Q4_K_M,
Llama-3.2-1B-Instruct-Q4_K_M).

Ce que ce run change par rapport au constat de §0.7.1 : le parcours physique
jusqu'au premier token **a été exercé**, contre un vrai binaire et de vrais
modèles, en local comme en cluster. Il reste vrai qu'**aucun test n'a été
exécuté contre un GPU réel** : la VRAM est purement déclarative ici, et
`install.sh --mode local` a exigé un stub `nvidia-smi` pour franchir son
préflight. TST-004 est donc partiellement satisfait, AUT-009 démontré
manuellement, TST-005 (E2E cluster) exercé à la main mais toujours non
automatisé.

**Défauts trouvés — aucun n'était visible en CI :**

| Découverte | Preuve | Item |
|---|---|---|
| **Le mode cluster ne démarrait pas du tout.** `model_manager._build_manager()` construit sa ligne de log avec `n.node_id` alors que `NodeConfig` expose `id` : `AttributeError` à l'import, donc avant tout service. La ligne juste au-dessus utilise correctement `node.id`. `CLUSTER_MODE=cluster` était mort-né sur `dev`. | Reproduit : service `failed`, `journalctl` explicite 🔬 | **COR-015** |
| **`update-agent.sh` ne pouvait jamais réussir.** Le venv est construit dans `mktemp -d .agent-update.XXXXXX` puis **déplacé** vers `venv-agent`. Un venv n'est pas relogeable : le shebang de `bin/uvicorn` continue de pointer vers le staging (mode 0700, root-only, puis supprimé) → `203/EXEC Permission denied`, 5 health-checks en échec, rollback. `ExecStartPre` passait, lui, car `bin/python` est un lien vers l'interpréteur système — le symptôme désignait donc le mauvais coupable. `gateway/deploy/update.sh` ne souffre pas du défaut : il construit son venv à son emplacement final et bascule par symlink. | Reproduit, puis cause isolée en construisant un venv de test et en relisant le shebang après `mv` 🔬 | **COR-016** |
| **Le rollback de mode laisse le service à terre.** Après l'échec de bascule cluster, `update.sh` a bien restauré `CLUSTER_MODE=local` et l'unité locale, mais son `systemctl start` s'est heurté au start-limit systemd (`Start request repeated too quickly`) : service **`failed`**, gateway indisponible, intervention manuelle requise. Il n'existe **aucun `systemctl reset-failed`** dans le dépôt, alors que `update.sh` démarre le service à 5 endroits. | Reproduit : indisponibilité réelle, rétablie par `systemctl reset-failed` 🔬 | **COR-017** |
| **Le timer de sauvegarde quotidienne n'est jamais armé.** `install.sh` et `update.sh` font `systemctl enable` sans `--now` : le timer est `enabled` mais `inactive`, absent de `list-timers`, et aucune sauvegarde ne tourne **jusqu'au prochain reboot**. Le commentaire du code craint qu'un `--now` déclenche un rattrapage `Persistent=true` immédiat ; vérifié faux : un premier `start` sans stamp pose le stamp sans exécuter le job. | Reproduit : `is-enabled=enabled`, `is-active=inactive`, aucune ligne dans `list-timers` 🔬 | **OPS-008** |
| **`deploy/nginx.conf` utilise la directive dépréciée `listen … ssl http2`**, retirée au profit de `http2 on;` depuis nginx 1.25. Debian 13 émet deux warnings à chaque `nginx -t` et à chaque reload. | Reproduit sur nginx de Debian 13 🔬 | **OPS-009** |
| **Aucun test ne construit le manager en mode cluster.** `_build_manager()` n'apparaît que dans `test_doctor.py`. Les 633 tests gateway passent avec COR-015 présent : c'est exactement la classe de régression que la CI doit fermer. | Constat sur la suite de tests 🔬 | **TST-006** |
| **Les venvs de release ne sont jamais purgés.** Corollaire de la stratégie de bascule par symlink (`update.sh` de longue date, `update-agent.sh` depuis COR-016) : plus aucun venv n'est écrasé, donc chaque mise à jour laisse une arborescence complète (~200 Mo sur un nœud) sous `venv-release-*` / `venv-agent-release-*`, plus un éventuel `*-pre-update-*` issu de la migration. Aucun des deux scripts ne borne cette accumulation, et rien ne la signale : `/opt` sature en silence. Même classe de défaut que les sauvegardes `*.pre-migration.*.bak` d'OPS-006 (suivi sous OPS-002). | Constat de lecture des deux scripts de mise à jour, confirmé par le nommage horodaté des releases | **OPS-010** |

**Ce que le run a validé, lui, en conditions réelles** 🔬 : `doctor` (exit 3,
diagnostics pertinents dont COR-009 correctement signalé), `--verify-hashes`,
`/health` et `/ready` (structural puis serving), la recette du premier token à
travers nginx + TLS en local **et** en cluster, le SSE réellement non
bufferisé (~96 ms entre deltas), l'éviction LRU, l'invariant COR-004 (409 sur
`unload` pendant un stream, 200 après), le déchargement pour inactivité sans
processus orphelin, le rate limiting applicatif avec `Retry-After`, la
réconciliation VRAM de COR-001, le **rollback de `update.sh` sur une régression
volontairement injectée** (`generation:no_content` → restauration → code
redéployé byte-identique au sain), et le comportement en panne de nœud (offline
après 3 heartbeats, 503 explicite, retour en ligne et rechargement transparent).

**Points d'exploitation relevés au passage** — constats sans item dédié, à
arbitrer :

| Sujet | Conséquence | Suite |
|---|---|---|
| `install.sh --mode local` **exige `nvidia-smi`** en préflight (`command -v`), sans échappatoire. | Aucun hôte CPU — banc de test, staging, CI sur VM — ne peut installer le mode local. Ce run a dû poser un stub. | À trancher : soit un `--allow-no-gpu` explicite, soit assumer le refus et le documenter comme tel |
| La queue d'admission est **inerte en mode cluster** : `/v1/capacity` renvoie `enabled: false, status: unavailable` malgré `CAPACITY_QUEUE_ENABLED=true` dans l'environnement. | Divergence de comportement public entre local et cluster, contraire à l'invariant annoncé dans `AGENTS.md` (« le mode cluster garde le même comportement public que le mode local »). | À documenter comme limite assumée, ou à porter en cluster |
| `update.sh` accepte `--smoke-base-url https://…` mais **n'a aucun passthrough `--ca-cert`/`--insecure-tls`** vers la recette. | Viser nginx impose que la PKI interne soit déjà dans le magasin système. Vrai pour la PKI UPPA, mais non documenté — un opérateur qui teste avec un certificat hors magasin conclura à tort à une régression. | Option à ajouter, ou prérequis à écrire dans `docs/deployment.md` §11 |
| Les snapshots `code-pre-update-*` sont **étiquetés avec le commit entrant**, alors qu'ils contiennent l'**ancien** code. | Après un rollback, l'opérateur cherche le snapshot de la version saine et trouve un dossier portant le nom de la version fautive. | Renommage cosmétique, sans effet fonctionnel |
| Pendant qu'un nœud est `offline`, `/admin/cluster` continue d'afficher ses `loaded_models` d'avant la panne. | Le drapeau `online: false` est bien présent, mais la liste de modèles induit en erreur dans un diagnostic d'incident. | À vider ou à marquer `unavailable`, conformément à `docs/deployment.md` §13 |
| Le dashboard affiche `active_users_7d: 2` pour `total_users: 1`. | Conséquence directe et attendue de DEC-001 (usage conservé sous pseudonyme après anonymisation), mais l'affichage paraît incohérent. | Libellé à préciser côté dashboard |
| Un `git clone --depth 1` de llama.cpp produit un binaire qui se déclare `version: 1`. | `LLAMA_SERVER_MIN_BUILD` deviendrait impossible à satisfaire sur un clone superficiel. Le garde-fou supply-chain suppose un clone complet, ce que `docs/deployment.md` §2 fait bien — mais sans dire pourquoi. | À mentionner dans la note de §2 quand SEC-005 sera traité |

### 0.11 Vague 4 — fermeture des retours du premier déploiement réel

Le déploiement de §0.10 a produit six items. Cette vague les ferme. Elle y
ajoute **COR-009**, parce que son défaut de timeout a été reproduit sur VM
(§0.9) et qu'il touche le même fichier que OPS-009 : les traiter ensemble évite
deux passes sur `nginx.conf`.

Les chantiers sont regroupés par **propriété exclusive de fichiers**, de sorte
que deux chantiers menés en parallèle ne puissent jamais se marcher dessus.
Chacun est mené dans un worktree git isolé, sur sa propre branche, et produit
des commits atomiques ensuite fusionnés dans la branche de lot.

| Chantier | Items | Priorité | Fichiers possédés | Commits | Tests | État |
|---|---|---|---|---|---:|---|
| C1 | TST-006, clôture COR-015 | P0 | `gateway/model_manager.py`, tests cluster | `40a22f4` | +5 | `[x]` |
| C2 | COR-016, COR-017 (part agent) | P0 | `node_agent/deploy/`, `node_agent/tests/` | `5a080a2`, `5fe61d8` | +11 | `[x]` |
| C3 | COR-017 (part gateway), OPS-008 | P0 / P1 | `gateway/deploy/install.sh`, `update.sh` | `8961607`, `ad953bc` | +19 | `[x]` |
| C4 | OPS-009, COR-009 | P2 / P1 | `gateway/deploy/nginx.conf`, `docs/api.md` | `28acf1c`, `0c2efcb` | +16 | `[x]` |
| — | OPS-009 (complément) | P2 | `gateway/deploy/nginx-lib.sh` | `66140b1` | +12 | `[x]` |
| — | OPS-010 (corollaire) | P1 | `*/deploy/*venv*-lib.sh` | `d58b365` | +17 | `[~]` |

**Total : +80 tests**, chacun vérifié rouge avant correctif et vert après. La
rougeur de C1, C2 et C3 a été **re-jouée indépendamment par l'orchestrateur**
avant fusion, pas seulement rapportée par le chantier.

#### OPS-010, le défaut que la vague a elle-même créé

COR-016 supprime le déplacement de venv au profit d'une bascule par symlink.
Corollaire immédiat, et signalé par le chantier lui-même : **plus aucun venv
n'est écrasé**, donc chaque mise à jour laisse une arborescence complète sur le
disque — environ 200 Mo par mise à jour et par nœud — sans que rien ne borne
l'accumulation ni ne la signale. `gateway/deploy/update.sh` portait d'ailleurs
le même comportement depuis toujours ; COR-016 l'a rendu visible en
l'étendant au node-agent.

La rétention est désormais explicite et bornée des deux côtés (`EVA_*_VENV_KEEP`,
défaut 2 : l'active et la précédente, celle que réclame le retour arrière manuel).
Trois garde-fous méritent d'être connus : la cible courante du symlink n'est
**jamais** supprimée quel que soit son âge, l'ordre est celui des dates de
modification et non des noms (un nom de release gateway commence par un hash de
commit et ne se trie pas), et la purge n'a lieu qu'**après** validation — purger
plus tôt supprimerait ce vers quoi le rollback rebascule.

L'item reste `[~]` : les venvs sont bornés, les sauvegardes
`*.pre-migration.*.bak` d'OPS-006 ne le sont toujours pas (suivi sous OPS-002).

#### Les deux points de vigilance, et ce qu'ils ont donné

- **COR-015 était déjà corrigé dans le code** (`n.node_id` → `n.id`, présent sur
  `dev`) et ne restait `[~]` que faute de test. C1 n'a donc pas réécrit le
  correctif : il a livré la régression qui le verrouille. Rougeur confirmée —
  réintroduire `n.node_id` fait tomber 2 tests sur `AttributeError`.
- **OPS-009 n'était pas cosmétique sans condition**, et le piège était pire que
  prévu. `http2 on;` demande nginx ≥ 1.25.1, mais le socle documenté est
  **Ubuntu 22.04 (nginx 1.18) et 24.04 (nginx 1.24)** : la directive moderne
  aurait empêché nginx de démarrer sur **les deux LTS supportées**, pas
  seulement sur la plus ancienne. C4 a donc refusé de restreindre le socle et a
  livré une conf neutre, sans HTTP/2.

  **Cet arbitrage a été révisé.** Retirer HTTP/2 faisait perdre la
  fonctionnalité aux deux plateformes supportées pour supprimer un
  avertissement qui n'apparaît qu'à partir de 1.25.1 — donc sur une plateforme
  *absente de la matrice de support*, Debian 13, celle de l'incident. Le
  complément `66140b1` rend l'activation **conditionnelle à la version
  détectée** (`deploy/nginx-lib.sh`, sourcé par `install.sh` et `update.sh`) :
  `listen … ssl http2` en dessous de 1.25.1, `http2 on;` au-dessus. Personne ne
  perd HTTP/2, `nginx -t` est silencieux de 1.18 à 1.29, et le repli sur une
  version illisible est délibérément la forme sans HTTP/2 — un avertissement
  cosmétique vaut mieux qu'un service mort.

#### Décision de sortie de vague

Les six défauts du premier déploiement réel sont fermés, chacun par un test qui
échoue sans son correctif. **Le Lot A n'a plus de P0 ouvert** : le travail de
bootstrap (Lot B / jalon M1) peut démarrer sans dette bloquante.

### 0.12 Vague 5 — le planificateur de bootstrap (jalon M1)

Cette vague livre la couche **non privilégiée** de §5 : un paquet
`gateway/bootstrap/` et une commande `python cli.py bootstrap-plan` qui
calculent ce qu'il faudrait installer sur un hôte pour aller jusqu'au premier
token — **sans rien appliquer**. Aucun téléchargement, aucune compilation,
aucune écriture. Le seul sous-processus possible est `llama-server --version`,
et seulement si l'opérateur fournit `--llama-bin`.

| Chantier | Items | Priorité | Fichiers possédés | Commits | Tests | État |
|---|---|---|---|---|---:|---|
| — | AUT-001 (contrat) | P0 | `bootstrap/schema.py` | `5a8271e`, `380feff` | +55 | `[x]` |
| C1 | AUT-002 | P0 | `bootstrap/inventory.py` | `e3516ee` | +48 | `[x]` |
| C2 | AUT-013, AUT-005 | P1 / P0 | `bootstrap/catalog.*`, `bootstrap/gguf_meta.py` | `c349470`, `07534cd` | +126 | `[x]` |
| C3 | AUT-003 | P0 | `bootstrap/runtime_resolver.py` | `54f61f6` | +68 | `[x]` |
| C4 | AUT-004 | P1 | `bootstrap/llmfit.py` | `f5788c2` | +98 | `[x]` |
| — | AUT-001 (correctifs de contrat) | P0 | `bootstrap/schema.py` | `1b77763` | +10 | `[x]` |
| — | AUT-001 (assemblage et CLI) | P0 | `bootstrap/planner.py`, `cli.py` | `976894d`, `ca0871f` | +40 | `[x]` |
| — | AUT-001/002/004 (revue) | P1 | `bootstrap/schema.py`, `planner.py`, `cli.py` | `055eeea` | +20 | `[x]` |
| — | AUT-001 (revue, 2ᵉ passe) | P1 | `bootstrap/schema.py` | `8e25616` | +20 | `[x]` |
| — | AUT-001 (revue, 3ᵉ passe) | P2 | `bootstrap/schema.py` | `c7a25c6` | +13 | `[x]` |
| — | AUT-004 (échec CI) | P2 | `tests/test_bootstrap_planner.py` | `3fe1413` | +1 | `[x]` |

**Total : +499 tests.** Les chantiers initiaux ont été menés dans des worktrees
git isolés, avec propriété exclusive de leurs fichiers, et ont dû prouver la
rougeur de leurs tests en cassant réellement leur code — 27 mutations appliquées
et rejouées au total, pas seulement affirmées. Les passes de revue suivantes ont
reproduit directement chaque défaut avant correction. La documentation, écrite
en dernier contre le code plutôt que contre l'intention, a elle-même trouvé un
défaut de code de sortie — c'est le meilleur argument pour ne pas la rédiger
avant.

#### L'architecture qui a rendu la parallélisation possible

Les producteurs (`inventory`, `runtime_resolver`, `llmfit`, `catalog`,
`gguf_meta`) **ne s'importent jamais entre eux**. Ils se projettent vers un
contrat commun — `schema.PlanSection` — et seul `planner` les connaît tous.
Cette règle a deux effets, et le second n'était pas recherché :

1. un producteur peut échouer sans entraîner les autres : un `nvidia-smi` cassé
   dégrade la section matériel, il n'empêche ni le catalogue d'être lu, ni le
   plan d'exister pour dire où est le trou ;
2. quatre chantiers ont pu être menés **en parallèle** sans jamais se croiser,
   parce que le seul fichier partagé — le contrat — était figé avant leur
   démarrage.

#### Les arbitrages qui méritent d'être connus

- **Le planificateur refuse d'inventer un numéro de build.** Sans
  `--pin-version`/`--pin-commit`, `ReleasePolicy` ne peut pas être construite et
  le plan sort **bloqué**, avec un message qui dit quoi fournir. Un `bNNNNN`
  inventé se propagerait dans tous les manifestes de provenance produits, où il
  aurait l'apparence d'un fait vérifié. Même logique côté catalogue : une entrée
  sans SHA-256 ni révision est listée mais **fail-closed**, exclue de toute
  planification de téléchargement.
- **Un plan bloqué ne propose aucune étape.** Décrire des téléchargements sans
  binaire capable de servir les modèles inviterait à n'exécuter que la moitié du
  plan — celle qui consomme du disque et du réseau. Ce qui *serait* retenu reste
  visible dans la section « modèles » : rien n'est caché, seule la séquence
  actionnable est retenue.
- **LLMfit ne peut qu'ordonner.** La règle de §7 est appliquée dans son ordre
  littéral : le catalogue et le budget sont des **filtres durs**, la
  recommandation n'intervient qu'ensuite et seulement pour faire passer un
  candidat approuvé devant un autre candidat approuvé. Trois barrières
  structurelles l'empêchent d'activer un modèle seul — le module ne référence
  aucune constante `ACTION_*`, ses identifiants sortent sous la clé `candidate`
  et jamais `model_id`, et chaque entrée porte `catalog_approved: null`. Les
  trois sont testées **sur l'AST du module**, pas par `grep`.
- **L'heuristique « VRAM nominale contre VRAM exposée » a été refusée**, pas
  oubliée : elle échoue sur son cas fondateur (`NVIDIA L40S` ne contient aucun
  chiffre) et produit un faux positif sur `RTX 4090`. La valeur exposée devient
  la seule vérité ; la confrontation au `TOTAL_VRAM_GB` configuré reste chez
  `doctor`, seul endroit qui tient les deux grandeurs.
- **Le paquet `gguf` officiel a été évalué puis écarté** pour cette vague : il
  tire `numpy` dans le chemin d'un planificateur censé tourner sur une machine
  vierge, et son `GGUFReader` matérialise exactement ce qu'on ne veut pas lire
  (tous les tenseurs, le vocabulaire du tokenizer). Le parseur maison tient en
  ~200 lignes bornées, testables avec des fichiers fabriqués.

#### Les deux modèles du catalogue initial

| | Modèle 1 | Modèle 2 |
|---|---|---|
| Dépôt | `Qwen/Qwen2.5-0.5B-Instruct-GGUF` | `HuggingFaceTB/SmolLM2-360M-Instruct-GGUF` |
| Licence base **et** fine-tune | apache-2.0 | apache-2.0 |
| Gated | non | non |
| Révision et SHA-256 | épinglés | épinglés |

Les empreintes sont **réelles** : relevées sur l'API publique de Hugging Face,
recoupées indépendamment par l'en-tête `X-Linked-Etag` du point de
téléchargement, puis **revérifiées par l'orchestrateur** avant fusion — c'était
la revendication la plus risquée de la vague, elle tient. `Llama-3.2-1B`, utilisé
lors du déploiement réel de §0.10, a été **écarté** : la « Llama 3.2 Community
License » impose des conditions d'usage, une clause de nommage et un seuil
d'utilisateurs — elle n'est pas permissive, et un test échoue si quelqu'un
l'introduit demain.

#### Défauts découverts pendant la vague 5

| Découverte | Trouvé par | Traitement |
|---|---|---|
| **Un constat `fail` pouvait disparaître silencieusement.** `BootstrapPlan.blockers` ne collectait les constats bloquants que dans les sections dont le statut valait lui-même `fail`. Un producteur dont le calcul de statut diverge un peu de ses propres constats voyait son bloqueur s'évaporer : absent du verdict, absent du rendu, sans effet sur le code de sortie. Le contrat se lisait comme s'il bloquait. | AUT-003 **et** AUT-005, indépendamment | Corrigé (`1b77763`) 🔬 |
| **`enforce_llama_min_build()` n'est pas fail-closed**, alors que §6 l'exige : version illisible avec `min_build > 0` → `log.warning` et démarrage autorisé. La politique existe en trois endroits avec deux sémantiques — `doctor` est fail-closed, `main._validate_inference_runtime` et `llama_version` ne le sont pas. Une gateway démarrée sans passer par `doctor` peut donc servir sur un binaire inattestable. La divergence est **documentée et délibérée** dans `doctor.py`, mais le risque résiduel est réel. | AUT-003 | Nouvel item **SEC-009** 📖 |
| **`llama_version._VERSION_RE` prend le PREMIER `version\|build : <digits>`** de la sortie combinée stdout+stderr. Sur un build CUDA qui émet des lignes d'initialisation de backend avant la ligne de build, un motif parasite donnerait un numéro absurdement bas. Même classe que le défaut du clone superficiel de §0.10. | AUT-003 | Hypothèse `🧭` non vérifiée faute de binaire GPU — à confirmer, suivi sous SEC-009 |
| **`doctor.visible_devices()` confond « `CUDA_VISIBLE_DEVICES` non définie » et « définie et vide »**, et n'implémente pas la troncature que son propre message décrit. | AUT-002 | Constat `📖` — **non bloquant, vérifié** : `cuda_visible_devices` a pour défaut `"0"`, `install.sh` l'écrit et `ServerManager` la propage toujours explicitement, donc une valeur vide signifie réellement « aucun device ». Le helper reste ambigu pour tout futur appelant |
| **`doctor.parse_nvidia_smi_csv()` accepte `memory.total <= 0`** : un GPU en erreur entre dans l'inventaire et contribue 0 au budget sans un mot. | AUT-002 | Constat `📖` — l'inventaire de la vague 5 l'écarte, `doctor` reste à aligner |
| **Un trou de couverture dans le planificateur lui-même** : remplacer `plannable_entries()` par `entries` ne faisait tomber aucun test, toutes les entrées livrées étant épinglées. Le filtre fail-closed était libre de disparaître. | Protocole de mutation, à l'assemblage | Corrigé — un test fabrique un catalogue partiellement épinglé 🔬 |
| **Un `--model` inconnu sortait en code 4**, c'est-à-dire « le planificateur lui-même a échoué » : la `CatalogError` remontait jusqu'au `except Exception` de la CLI. Un script d'exploitation qui lit 4 conclut à une panne de l'outil sur une faute de frappe. Même défaut pour un `--hardware-profile` introuvable. | Le chantier **documentation**, en rédigeant la grille des codes de sortie | Corrigé (`ca0871f`) — `PlannerUsageError` sépare les conséquences : 2 la commande est mal formée, 1 l'hôte est bloqué, 4 l'outil a cassé 🔬 |
| **`schema.merge_findings()` déduplique par `code` globalement.** Sûr à l'intérieur d'un producteur ; deux producteurs qui choisiraient le même code s'effaceraient mutuellement si le planificateur l'employait entre sections. | AUT-002, confirmé par AUT-003 | Constat `📖` — le planificateur ne l'emploie pas entre sections ; à documenter dans le contrat des producteurs |

#### Revue de la vague — cinq défauts fermés

Une relecture ciblée du code livré a trouvé cinq défauts que ni les chantiers ni
l'assemblage n'avaient vus. Tous ont été **reproduits** avant correction, aucun
n'a été corrigé sur description.

| Découverte | Preuve | Traitement |
|---|---|---|
| **Un plan bloqué proposait quand même des actions.** L'invariant n'était tenu que pour l'absence de politique de release. Un résolveur rendant `resolved=False` laissait passer téléchargement, écriture de registre, activation et recette — **sans la moindre étape d'installation**. | Reproduit : plateforme sans variante d'artefact → `applicable=False` et 8 étapes 🔬 | Invariant ré-adossé aux **bloqueurs** dans `build_plan`, donc valable pour toutes leurs causes ; et vérifié une seconde fois au niveau du document par `_validate_verdict()` |
| **`CUDA_VISIBLE_DEVICES` était ignoré avec `--hardware-profile`** : l'environnement n'était pas transmis au chargeur, qui retombait sur `{}`. | Reproduit : deux GPU déclarés, `CUDA_VISIBLE_DEVICES=1` → 2 GPU comptés, **90 Go au lieu de 45** 🔬 | Corrigé. À noter : la documentation décrivait déjà le bon comportement — c'est le code qui était en tort, pas elle |
| **`--mode cluster` était accepté sans aucun comportement cluster** : l'option n'était que recopiée dans le document final, et aucun test ne couvrait ce cas. | Constat de lecture, confirmé : aucune occurrence de « cluster » dans le planificateur 📖 | **Refusé explicitement** au jalon M1, avec la conduite à tenir dans le message. L'accepter produisait un plan cohérent et entièrement faux — inventaire de l'hôte gateway, runtime local, modèles sous son propre volume — ce qui est pire qu'une absence de plan |
| **L'épinglage LLMfit était inatteignable depuis la CLI** : l'adaptateur refusait correctement d'exécuter un binaire non épinglé, mais aucune option ne permettait de fournir version, empreinte, binaire ou profil manuel. AUT-004 existait comme bibliothèque, pas dans le parcours opérateur. | Constat : aucune occurrence de « llmfit » dans `cli.py` 📖 | Six options ajoutées (`--llmfit-bin/-version/-sha256/-timeout/-profile`, `--no-llmfit`), épinglage incohérent refusé en code 2 |
| **La validation ne recoupait aucun champ dérivé** : un document portant une section `fail` mais retouché en `status: ok` / `applicable: true` / `exit_code: 0` / `blockers: []` passait sans une seule erreur. | Reproduit : document falsifié à la main, validation vide 🔬 | `_validate_verdict()` recalcule status, applicable, exit_code, blockers, warnings, counts et volume depuis les sections. L'enjeu est **M2**, qui appliquera des plans relus depuis un fichier : un applicateur ne doit jamais pouvoir être convaincu d'agir par un champ dérivé que personne ne recoupe |

#### Seconde et troisième passes de revue — l'invariant tenait encore mal

Deux relectures successives du correctif ont montré que l'invariant puis son
validateur n'étaient pas encore complètement fermés.

| Découverte | Preuve | Traitement |
|---|---|---|
| **`--strict` produisait un plan bloqué qui proposait des actions.** La promotion des avertissements en blocage ne valait que pour le code de sortie : `status: fail`, `exit_code: 1`, et pourtant `applicable: true` avec **9 étapes** — validateur muet. | Reproduit 🔬 | `is_blocked()` devient le seul lieu de décision ; `applicable`, `steps`, `counts` et le volume en dérivent, dans le JSON comme dans le rendu. Le validateur rejette tout statut `fail`/`blocked` portant des étapes. `--strict` change donc le **document**, pas seulement son affichage |
| **Les champs récapitulatifs étaient mal contrôlés** : une liste `warnings` entièrement inventée passait si sa longueur était juste ; `warnings: 7` faisait lever un `TypeError` ; `strict: "false"` était converti en VRAI par `bool(...)`, donc le verdict était recalculé à contresens. | Reproduit sur les trois 🔬 | Les sept champs sont obligatoires et typés strictement ; `blockers` et `warnings` sont comparés **élément par élément** aux constats recalculés |
| **Les valeurs de `counts` échappaient encore au typage strict.** L'égalité Python assimile `True` à `1`, `False` à `0` et `1.0` à `1` : six compteurs booléens passaient donc la validation. | Reproduit : document entièrement booléen → validation vide 🔬 | Sous-contrat fermé sur les six clés ; chaque valeur doit être un entier non booléen positif ou nul, et toute clé absente ou inconnue est refusée |

Ce que ces trois passes enseignent sur la vague elle-même : les tests écrits par
chaque chantier vérifiaient bien **leur** module, et l'assemblage vérifiait bien
ses raccords — mais l'invariant transverse « un plan non applicable ne propose
aucune action » n'appartenait à personne. Il a fallu deux corrections pour le
poser correctement, et la seconde a montré que la première le tenait encore par
la mauvaise grandeur : les bloqueurs, et non le **statut**. Il est désormais
vérifié à deux niveaux indépendants — au calcul par le planificateur, à la
publication par le contrat — et ni un producteur, ni un assembleur, ni un mode
d'affichage ne peut le contourner seul.

#### Un échec visible seulement en CI

La PR de la vague a échoué sur un test que rien ne faisait tomber en local :
`test_cli_expose_l_epinglage_llmfit` cherchait `--llmfit-bin` dans la sortie de
`--help`. **rich colorise cette sortie dès qu'il détecte GitHub Actions** et
découpe alors les noms d'options en fragments séparés par des séquences ANSI :
une option parfaitement déclarée devient introuvable par recherche de
sous-chaîne.

Le défaut n'était pas dans la CLI mais dans le test, qui vérifiait **le rendu au
lieu du contrat**. Les options sont désormais lues sur l'objet Click de la
commande, avec un contrôle positif prouvant que l'introspection voit bien les
autres options.

Deux enseignements, au-delà du correctif :

- la recette de parité CI d'`AGENTS.md` (Python 3.11, dépendances fraîchement
  résolues) **n'aurait pas suffi** : le déclencheur n'était ni la version de
  Python ni celle de typer/click, mais la variable `GITHUB_ACTIONS`. Rejouer la
  suite avec `GITHUB_ACTIONS=true` a reproduit l'échec en une passe ;
- l'assertion d'**absence** de secret dans une sortie CLI était affaiblie par le
  même mécanisme : un jeton fragmenté par des séquences de couleur y serait
  passé inaperçu. Les sorties CLI sont maintenant nettoyées avant toute
  recherche — le test d'absence y gagne plus que les autres.

#### Ce que la vague 5 ne prétend pas avoir démontré

- **Le plan n'a jamais été appliqué.** C'est M2, et c'est le point important :
  savoir décrire une installation n'est pas savoir l'exécuter.
- **Les fixtures de test LLMfit sont synthétiques.** Le dépôt amont ne publie ni
  les noms de champs ni un exemple de la sortie `recommend --json` ; la forme est
  dérivée de §7. Le coût d'une hypothèse fausse est borné par construction : une
  sortie non conforme donne `llmfit_schema_invalid` en `warn` et le plan continue
  **sans** recommandation. À remplacer par de vraies captures avant production.
- **La matrice d'artefacts `llama-server` mélange constats et hypothèses.** Chaque
  variante porte un champ `evidence`, et une variante retenue sur hypothèse
  déclenche un avertissement. L'existence des archives officielles CPU, de
  l'archive macOS Metal et la faisabilité des builds ROCm/Vulkan/arm64 sont des
  **hypothèses non vérifiées**, faute d'accès à la matrice de release amont.
- **Toujours aucun test contre un GPU réel** : la VRAM reste déclarative.
- **Le mode cluster n'est pas planifiable.** Il est refusé explicitement plutôt
  que silencieusement faux, mais la planification des nœuds — interroger chaque
  node-agent pour son inventaire et son runtime — reste entièrement à faire.

#### 0.12.1 Sortie du jalon M1

Conditions de §13 et leur état :

| Condition M1 | État |
|---|---|
| Inventaire matériel | `[x]` — AUT-002, sondes injectables, `CUDA_VISIBLE_DEVICES` respecté |
| Résolution runtime en mode dry-run | `[x]` — AUT-003, ordre de §6 testé étape par étape, refus explicite inclus |
| LLMfit intégré par JSON | `[x]` — AUT-004, validé à la frontière, absent = `skip` |
| Catalogue initial de deux petits modèles permissifs | `[x]` — AUT-005, apache-2.0 des deux côtés, empreintes réelles |
| Plan inspectable sans écriture | `[x]` — AUT-001, `bootstrap-plan` en JSON et en français, aucune écriture |

**Décision de sortie prononcée** : le système sait expliquer ce qu'il
installerait et pourquoi. Le travail d'exécution (M2 : AUT-006 → AUT-011) peut
démarrer sur une base qui décrit correctement son intention.

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
`llama-server` possédés par la gateway, registre de modèles, queue de capacité,
pin/unpin, orchestration cluster légère, systemd et nginx.

La priorité n'est pas une réécriture. Elle est de fermer les écarts entre les
invariants annoncés, les tests simulés et le comportement réellement déployé.

### Référence initiale vérifiée

| Contrôle | Résultat initial |
|---|---|
| Tests gateway | 309 réussis |
| Tests node_agent | 45 réussis |
| Total | 354 réussis |
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
| Multiplication des connexions `aiosqlite` | Confirmé dans le chemin chaud. Le coût exact annoncé en millisecondes n'est toutefois pas mesuré. Une connexion unique sérialisée pourrait devenir un autre goulot ; comparer connexion persistante, writer dédié et petit pool. |
| Buffering intégral en présence de `tools` | Confirmé. Le TTFT devient proche du temps de génération complet. |
| Silence pendant un cold start | Confirmé. La gateway ne retourne pas la `StreamingResponse` avant la fin de `ensure_model_loaded()`. |
| Dépendances non verrouillées | Confirmé. Les installations ne sont pas reproductibles : aucun `requirements.lock`, uniquement des contraintes `>=`. |
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
| COR-009 | `[x]` | P1 | Aligner les routes nginx et FastAPI | Chaque route documentée est exposée ou supprimée de la documentation. **Livré le 2026-07-30** : `/ready` et `/completion` étaient documentés sur l'URL publique mais retombaient en 404 côté nginx ; les timeouts sont désormais dérivés du `load_timeout_seconds` maximal du registre (900 s) au lieu des 30 s qui faisaient échouer tout pré-chargement admin en 504. |
| COR-010 | `[ ]` | P1 | Corriger `revoke_key()` | `%` et `_` sont littéraux ou rejetés ; seconde révocation a un comportement défini. |
| COR-011 | `[–]` | — | ~~Corriger le slot student abandonné~~ | Annulé : composant supprimé (DEC-009). |
| COR-012 | `[ ]` | P1 | Définir une réservation de quota | Les requêtes concurrentes ne dépassent pas silencieusement le budget, ou le dépassement maximal accepté est borné et documenté. |
| COR-013 | `[ ]` | P1 | Préserver les erreurs upstream avant SSE | Une 4xx/5xx de `llama-server` ne devient pas un HTTP 200 ambigu ; contrat d'erreur streaming testé. |
| COR-014 | `[x]` | P0 | Rendre chargeables les réglages de liste de l'environnement | `ALLOWED_MODEL_DIRS`, `CORS_ALLOW_ORIGINS` et `ALLOWED_MODELS` acceptent la syntaxe documentée sans faire échouer le démarrage ; les fichiers d'exemple livrés sont chargeables. **Item ajouté le 2026-07-30**, découvert par AUT-012 : ces trois réglages étaient inutilisables tels que documentés. |
| COR-015 | `[x]` | P0 | Réparer le démarrage en mode cluster | `CLUSTER_MODE=cluster` démarre et sert ; la régression est verrouillée par TST-006. **Item ajouté le 2026-07-30** (§0.10). Correctif d'une ligne appliqué et vérifié sur VM (`n.node_id` → `n.id` dans `model_manager._build_manager()`), puis **verrouillé par TST-006** le 2026-07-30 : réintroduire `n.node_id` fait échouer 2 tests sur `AttributeError`. |
| COR-016 | `[x]` | P0 | Rendre `update-agent.sh` capable de réussir | Une mise à jour de node-agent aboutit sans rollback, et la stratégie de venv est la même que celle de `update.sh` (construction à l'emplacement final, bascule par symlink) plutôt qu'un déplacement de venv. **Item ajouté le 2026-07-30** (§0.10). Contournement appliqué et vérifié sur VM (`ExecStart` via `python -m uvicorn`, conservé en défense en profondeur), puis **correctif structurel livré** le 2026-07-30 : le venv est construit à son emplacement définitif et `venv-agent` devient un symlink que l'on bascule, comme dans `update.sh`. Un agent installé par l'ancien script est migré en place, sans action opérateur. Test de non-régression : un exécutable de `bin/` doit rester lançable après la bascule. |
| COR-017 | `[x]` | P0 | Rendre les redémarrages insensibles au start-limit systemd | Un rollback ne peut pas laisser le service en `failed` : chaque `systemctl start` des scripts de déploiement est précédé d'un `systemctl reset-failed`, et un échec de rollback est signalé comme une indisponibilité, pas comme un simple avertissement. **Item ajouté le 2026-07-30** (§0.10), **livré le même jour** : `reset-failed` avant chaque démarrage dans les 4 scripts de déploiement, et l'échec de redémarrage d'un rollback sort désormais en code 9 « INDISPONIBILITÉ », distinct du code 1 « version précédente restaurée et en service ». |

### Lot B — bootstrap automatisé

| ID | État | Priorité | Action | Critère d'acceptation |
|---|---|---|---|---|
| AUT-001 | `[x]` | P0 | Définir le schéma du plan de bootstrap | Schéma versionné, validé, sans secret, lisible avant application. **Livré le 2026-07-31** : `bootstrap/schema.py` porte le contrat (`PLAN_SCHEMA_VERSION`, `validate_plan_dict()` avec chemins de champs fautifs, `find_secret_leaks()` à deux filets — par nom de champ ET par forme de valeur —, `render_human()`/`render_json()` refusant tous deux de publier un document qui fuit), `bootstrap/planner.py` l'assemble et `python cli.py bootstrap-plan` l'expose. Aucune écriture, aucun téléchargement. Un plan **bloqué ne décrit aucune étape**, quelle qu'en soit la cause, et la validation rejette tout document qui porterait les deux. `--mode cluster` est refusé explicitement au jalon M1 : la planification des nœuds reste à faire, et un plan local présenté comme cluster serait cohérent et faux. |
| AUT-002 | `[x]` | P0 | Inventaire matériel automatique | CPU/RAM/disque/GPU/VRAM/driver/backend détectés et testés sur la matrice supportée. **Livré le 2026-07-31** : document §5 littéral, toutes les sondes injectables, `CUDA_VISIBLE_DEVICES` respecté y compris sa troncature, `--hardware-profile` validé à la frontière. Quatre issues GPU distinctes au lieu de deux — un `nvidia-smi` qui **échoue** rend une liste de backends **vide**, jamais `cpu` : c'est le refus du repli silencieux. |
| AUT-003 | `[x]` | P0 | Résolveur `llama-server` | Sélection versionnée, SHA vérifié, aucun fallback CPU silencieux. **Livré le 2026-07-31** : ordre de §6 testé étape par étape jusqu'au refus explicite, recherche backend-d'abord pour qu'une archive CPU officielle ne l'emporte jamais sur une image CUDA officielle, variante non épinglée inéligible, manifeste de provenance qui ne peut pas être incohérent par construction, `LLAMA_SERVER_MIN_BUILD` généré par `derive_min_build()` et fail-closed. Le clone superficiel de §0.10 est reconnu et nommé pour ce qu'il est. |
| AUT-004 | `[x]` | P1 | Adapter LLMfit JSON | Version figée, schéma validé, timeout, fallback manuel et tests avec sorties enregistrées. **Livré le 2026-07-31** : binaire refusé si son empreinte ne correspond pas à l'épinglage, validation stricte et bornée de la sortie JSON, timeout borné, profil manuel passant par la MÊME validation, absent = `skip` et non échec. Trois barrières structurelles empêchent une recommandation d'activer un modèle seule, testées sur l'AST du module. Intégré au parcours opérateur le même jour, après revue : `--llmfit-bin`, `--llmfit-version`, `--llmfit-sha256`, `--llmfit-timeout`, `--llmfit-profile` et `--no-llmfit` — l'adaptateur était auparavant inatteignable depuis la CLI. **Réserve** : les fixtures sont synthétiques, le dépôt amont ne publiant aucun exemple de sortie — à remplacer par de vraies captures avant production (§0.12). |
| AUT-005 | `[x]` | P0 | Créer le catalogue approuvé | Révision, fichiers, SHA, licence, paramètres et ressources pour chaque modèle proposé. **Livré le 2026-07-31** : `bootstrap/catalog.yaml`, deux entrées apache-2.0 des deux côtés de la chaîne de licence, révisions et SHA-256 **réels** (relevés sur l'API publique Hugging Face, recoupés par `X-Linked-Etag`, revérifiés à la fusion). Entrée non épinglée = **fail-closed** : listée, mais exclue de toute planification de téléchargement. L'ensemble split/`mmproj` est indivisible par construction, pas par consigne. |
| AUT-006 | `[ ]` | P0 | Télécharger les modèles de façon sûre | Reprise, espace disque, fichier temporaire, SHA, renommage atomique et provenance. |
| AUT-007 | `[ ]` | P1 | Générer `models.yaml` | Entrée désactivée tant que la calibration et le smoke test n'ont pas réussi. |
| AUT-008 | `[ ]` | P1 | Calibrer RAM/VRAM | Mesures avant/après, pics et marge enregistrés par fingerprint. |
| AUT-009 | `[ ]` | P0 | Recette premier token | Appel public complet avec TTFT mesuré et rapport sans secrets. |
| AUT-010 | `[ ]` | P1 | Pré-chauffer le modèle par défaut | Le premier utilisateur ne déclenche pas le chargement après un déploiement réussi. |
| AUT-011 | `[ ]` | P1 | Produire le rapport d'installation | Versions, empreintes, licences, matériel, modèle, performances et contrôles. |
| AUT-012 | `[x]` | P0 | Ajouter `evaruntime doctor` | Rapport humain/JSON et exit codes couvrant secrets, runtime, GPU, modèles, ports, DB, nginx, TLS fourni et limites systemd. |
| AUT-013 | `[x]` | P1 | Inspecter les métadonnées GGUF | Architecture, tenseurs, contexte et KV alimentent une estimation conservatrice sans être présentés comme une mesure exacte. **Livré le 2026-07-31** : parseur maison en bibliothèque standard, bornes explicites sur chaque champ de longueur venu du fichier (un GGUF est une entrée non fiable), validé contre deux vrais headers récupérés par requête `Range`. Le mot « estimation » figure dans le nom des types, dans le rendu et dans la liste des facteurs ignorés. Paquet `gguf` officiel évalué puis écarté (`numpy` sur une machine vierge), conclusion écrite dans le docstring. |

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
| SEC-009 | `[ ]` | P1 | Unifier la politique fail-closed de `LLAMA_SERVER_MIN_BUILD` | Une version de `llama-server` illisible alors qu'un build minimal est exigé refuse le démarrage, quel que soit le chemin emprunté. **Item ajouté le 2026-07-31** (§0.12), découvert en livrant AUT-003 : la politique existe en trois endroits avec deux sémantiques — `doctor` est fail-closed comme l'exige §6, `main._validate_inference_runtime` et `llama_version.enforce_llama_min_build()` ne le sont pas. Une gateway démarrée sans passer par `doctor` peut donc servir sur un binaire inattestable (cf. GHSA-8947-pfff-2f3c). Couvre aussi l'hypothèse `_VERSION_RE` : le premier motif `version|build` de la sortie peut être une ligne d'initialisation de backend, pas la ligne de build. |
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
| OPS-005 | `[–]` | — | ~~Automatiser la gateway étudiante~~ | Annulé : composant supprimé (DEC-009). |
| OPS-006 | `[x]` | P0 | Versionner les migrations SQLite | `PRAGMA user_version` ou équivalent, migration transactionnelle, sauvegarde préalable et test depuis chaque version supportée. |
| OPS-007 | `[–]` | — | ~~Définir la rétention d'audit student~~ | Annulé : composant supprimé (DEC-009). |
| OPS-008 | `[x]` | P1 | Armer réellement le timer de sauvegarde | Après `install.sh` puis après `update.sh`, `systemctl is-active llm-gateway-backup.timer` retourne `active` et le timer apparaît dans `list-timers`, sans reboot et sans déclencher de sauvegarde immédiate avant l'initialisation de la base. **Item ajouté le 2026-07-30** (§0.10) : `enable` sans `--now` laissait la sauvegarde quotidienne inerte jusqu'au prochain redémarrage. |
| OPS-009 | `[x]` | P2 | Moderniser les directives nginx livrées | `nginx -t` ne produit aucun avertissement de dépréciation sur les versions supportées (`http2 on;` au lieu de `listen … ssl http2`). **Item ajouté le 2026-07-30** (§0.10) : le bruit à chaque reload masque les avertissements utiles. |
| OPS-010 | `[~]` | P1 | Borner la rétention des venvs de release | Après N mises à jour successives, `gateway/deploy/update.sh` et `node_agent/deploy/update-agent.sh` laissent au plus 2 arborescences de venv (l'active et la précédente) et la cible du symlink n'est jamais supprimée. **Item ajouté le 2026-07-30** : sans purge, chaque mise à jour ajoutait ~200 Mo au disque du nœud, en silence. Rétention livrée et testée dans les deux scripts (`EVA_GATEWAY_VENV_KEEP` / `EVA_AGENT_VENV_KEEP`, défaut 2). **Reste à faire** : la même borne pour les sauvegardes `*.pre-migration.*.bak` d'OPS-006, suivie sous OPS-002. |
| TST-006 | `[x]` | P0 | Couvrir la construction du manager en mode cluster | Un test construit `model_manager._build_manager()` avec `CLUSTER_MODE=cluster` et un `nodes.yaml` minimal ; il échoue sur le code d'avant COR-015 et passe après. **Item ajouté le 2026-07-30** (§0.10) : 633 tests passaient alors que le mode cluster ne démarrait pas. Complète TST-005, qui vise le parcours E2E et non la construction à l'import. |

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

**Jalon atteint le 2026-07-31**, sortie prononcée — détail des cinq conditions
et de leurs preuves en §0.12.1.

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
| 2026-07-30 | Claude | Vague 4 : fermeture des 6 défauts du premier déploiement réel, plus COR-009 et OPS-010. En-tête de suivi §0.0 ajouté | 4 chantiers en worktrees isolés, **758 tests** (+80), rougeur de chaque correctif rejouée indépendamment avant fusion |
| 2026-07-30 | Codex | Relecture de `claude-analyse-projet.md`; correction du statut de la supposée course pin, ajout de doctor, inspection GGUF, migrations et ordre de travail performance | Lecture intégrale du document Claude, vérification du segment asyncio et du paquet officiel `gguf` |
