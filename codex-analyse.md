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

```text
M0 socle fiable  ──  M1 planificateur  ──  M2 installation  ──  M3 perf  ──  M4 pilote  ──  M5 prod
   [x] 30 juil.        [x] 31 juil.         [~] EN COURS         [ ]          [ ]           [ ]
                                          bloqué par COR-022
```

| | |
|---|---|
| **Où en est-on** | Jalons **M0 et M1 atteints**, M1 ayant été **exercé sur deux VMs réelles** lors du second déploiement (§0.13). La **vague 6 (M2) a livré ses sept modules d'exécution** et son applicateur : le système sait installer un runtime vérifié, télécharger des modèles à empreinte contrôlée, écrire un registre désactivé, calibrer, activer sur preuve, pré-chauffer, jouer la recette du premier token et produire un rapport d'installation (§0.14) |
| **Ce qui bloque** | **COR-022**, découvert à l'assemblage : le plan ordonne `enable_model` **avant** le `smoke_test` dont l'activation exige la preuve, et la recette a besoin d'un modèle activé. Aucun plan produit par `bootstrap-plan` ne peut aller à son terme. L'applicateur le constate et refuse au lieu d'entamer. **DEC-010 tranchée le 2026-08-01 — voie A**, activation provisoire puis retour arrière ; reste à implémenter |
| **Ce qui vient ensuite** | Implémenter **COR-022** selon la voie A, puis **AUT-017** — les contrats de sonde sont définis, le dépôt n'en contient aucune implémentation. Ces deux items sont exactement ce qui sépare « la mécanique est écrite » de « la machine peut s'installer » |
| **Santé des tests** | **1929 tests verts** (1866 gateway + 63 node_agent), `ruff` propre et `bash -n` propre sur les deux composants |
| **Reste à faire** | 40 items sur 75 (§0.3). Deux P0 ouverts : COR-022 (décidé, à implémenter) et AUT-017 |
| **Ce qui n'est toujours pas démontré** | Aucun test contre un **GPU réel** : la VRAM reste entièrement déclarative, y compris pour le module qui prétend la mesurer. Le parcours physique jusqu'au premier token a été exercé sur CPU avec de vrais GGUF (§0.10 et §0.13), jamais sur GPU. Aucun nginx réel dans un test automatisé — le point n° 1 de §10 est **codé, pas démontré**. Le plan n'a **jamais été appliqué de bout en bout** |

**Comment lire la suite** : §0.1 à §0.4 donnent l'état chiffré, §0.5 les
décisions tranchées **et celle qui reste à prendre**, §0.7 le journal des
livraisons, §0.8, §0.10, §0.12, §0.13 et §0.14 les défauts trouvés en implémentant, en
déployant et en assemblant — ceux qu'aucun des deux audits n'avait vus —, §0.9
ce que l'exploitation doit savoir.

> **Une lecture qui revient à chaque vague.** Les défauts les plus coûteux ne
> sont jamais dans le code qu'on vient d'écrire : ils sont dans le **contrat**
> que ce code consomme, et ils ne se voient qu'au moment où plusieurs usagers
> l'emploient pour de bon. La vague 6 en a fourni trois exemplaires — un
> détecteur de secrets qui rendait impubliable le rapport que la spécification
> exige, trois trous dans un contrat d'exécution que ses 93 tests n'avaient pas
> vus, et une contradiction d'ordonnancement qu'aucun des six chantiers ne
> pouvait apercevoir seul.

### 0.1 Situation

| Champ | Valeur |
|---|---|
| Dernière mise à jour | 2026-08-01 |
| Phase | **Vague 6 livrée, jalon M2 non prononcé** — les sept modules d'exécution et l'applicateur existent et sont verrouillés par leurs régressions ; la chaîne de preuve du plan reste nouée (COR-022). La vague 5 avait été livrée puis **exercée sur deux VMs réelles** (§0.13) |
| Jalon atteint | **M1 — planificateur de bootstrap** (§13), sortie prononcée (§0.12.1). M0 atteint le 2026-07-30 (§0.7.1) |
| Jalon visé | **M2 — installation jusqu'au premier token** (§13) — conditions détaillées en §0.14.1 |
| Branche de travail | `feat/lot-b-vague6-execution-bootstrap` (créée depuis `dev` @ `6e576a8`) |
| Base de référence | `6e576a8` — 1257 tests verts, `ruff` propre |
| Périmètre livré à ce jour | AUT-001 → AUT-007, AUT-011 → AUT-016, COR-001, COR-002, COR-004 → COR-007, COR-009, COR-014 → COR-017, OPS-006, OPS-008, OPS-009, SEC-008, TST-001, TST-006 |
| Périmètre de la vague 6 | AUT-006 → AUT-011 et AUT-016, plus AUT-015 né du besoin de contrat — **9 items** : 6 `[x]`, 3 `[~]` — plus COR-020 à COR-022, SEC-010 et AUT-017, tous nés de la vague (§0.14) |
| Ce qui bloque la sortie de M2 | **COR-022** (DEC-010 tranchée, implémentation à faire) et **AUT-017** — les deux seuls P0 ouverts du dépôt |

### 0.2 Base de référence des tests et état courant

| Suite | Commande | Référence | État courant |
|---|---|---:|---:|
| `gateway` | `cd gateway && .venv/bin/python -m pytest tests -q` | 309 | **1866** 🔬 |
| `node_agent` | `cd node_agent && .venv/bin/python -m pytest tests -q` | 45 | **63** 🔬 |
| **Total** | — | **354** | **1929** 🔬 |

Cette base est le point de non-régression : aucune livraison ne doit la faire
baisser, et chaque item livré doit l'augmenter du nombre de ses régressions.
**+324 tests** ajoutés par le jalon M0, **+80 par la vague 4**, **+499 par la
vague 5**, puis **+672 par la vague 6**, tous des régressions rouges avant
correctif et vertes après. Les scripts de déploiement passent `bash -n`.

La vague 6 a produit **191 mutations appliquées et rejouées** — le code
réellement cassé, la suite relancée, la garde remise. **Onze ont survécu** au
premier passage : autant de garde-fous que personne ne testait, chacun fermé par
un test supplémentaire plutôt que par une affirmation. Un protocole de mutation
qui ne trouve jamais de survivant ne prouve rien sur les tests ; il prouve
seulement qu'on n'a pas cherché.

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
| A — bloqueurs et invariants | 22 | 11 | 0 | 10 | 1 |
| B — bootstrap automatisé | 17 | 12 | 3 | 2 | 0 |
| C — performance | 8 | 0 | 0 | 8 | 0 |
| D — sécurité et supply-chain | 10 | 1 | 0 | 9 | 0 |
| E — tests et exploitation | 18 | 4 | 1 | 11 | 2 |
| **Total** | **75** | **28** | **4** | **40** | **3** |

**Vingt-deux items ont été ajoutés au backlog après coup**, d'où 75 au lieu de
53. Aucun ne figurait dans les deux audits initiaux : tous sont nés de
l'implémentation, du déploiement réel ou de l'assemblage. C'est le chiffre le
plus instructif du tableau — **près d'un tiers du travail réel n'était pas
prévisible sur document**.

| Origine | Items |
|---|---|
| Implémentation du jalon M0 (§0.8) | COR-014, SEC-008 |
| Premier déploiement réel sur deux VMs (§0.10) | COR-015 → COR-017, TST-006, OPS-008, OPS-009 |
| Vague 4 | OPS-010 |
| Vague 5 (§0.12) | SEC-009 |
| Second déploiement réel sur deux VMs (§0.13) | COR-018, COR-019, AUT-014, OPS-011, OPS-012 |
| **Vague 6 (§0.14)** | **AUT-015, AUT-016, AUT-017, COR-020, COR-021, COR-022, SEC-010** |

Les **8 items du jalon M0**, les **7 de la vague 4** et les **6 de la vague 5**
sont terminés et vérifiés.

Les quatre `[~]` demandent une lecture précise, parce qu'ils ne disent pas la
même chose. **OPS-010** est livré côté venvs, la borne sur les sauvegardes
`*.pre-migration.*.bak` restant à poser sous OPS-002. **AUT-008, AUT-009 et
AUT-010** ont leur mécanique écrite, testée et verrouillée par des régressions —
mais le dépôt ne contient **aucune implémentation de leurs sondes**. Rien n'a
jamais été mesuré, aucun token n'a jamais été servi par ce chemin. Les passer
`[x]` reviendrait à écrire que la machine peut s'installer ; c'est AUT-017 qui
le décidera.

**Deux P0 restent ouverts**, tous deux nés de la vague 6 : **COR-022**, dont la
décision DEC-010 est prise et l'implémentation à faire, et **AUT-017**. Les 10
items restants du Lot A sont en P1/P2, dont COR-018 et COR-019 issus du second
déploiement réel ; les 2 du Lot B sont AUT-014 (second déploiement) et AUT-017.

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
| DEC-010 | **Activation provisoire avec retour arrière** pour dénouer la chaîne de preuve du plan d'amorçage (COR-022). L'ordre devient `calibrate → enable → smoke_test → (échec : retour à `disabled`) → warmup`. Assumé : une fenêtre pendant laquelle un modèle non encore validé est servable, bornée par le fait que l'amorçage précède l'ouverture du trafic. Écarté : aligner l'activation sur la seule calibration (contredit le critère d'acceptation d'AUT-007 — un modèle activé sans qu'aucun token n'ait été produit par lui) ; valider par un chemin d'admission interne (contredit §10, dont le point n° 1 exige le vrai chemin public). | 2026-08-01 |
| DEC-009 | **Suppression du composant `gateway-student`** : jamais déployé, aucun consommateur. Code, tests, documentation, unités systemd/nginx/nftables et job CI retirés du dépôt. Vérifié sans effet fonctionnel — ni `gateway` ni `node_agent` n'en dépendaient (aucun import, aucune clé `llmstu-*`, aucune route ni base partagée). Les items de backlog exclusivement student (**COR-011**, **OPS-005**, **OPS-007**) sont annulés, ainsi que **EVA-006** et **EVA-023** côté `claude-analyse-projet.md`. Écarté : garder le composant en l'état (surface de maintenance, CI et audit de dépendances pour du code mort). | 2026-07-30 |

Les décisions DEC-002 à DEC-008 restent `[!]` : elles ne bloquent pas le jalon
M0 et seront tranchées à l'entrée des lots concernés.

**DEC-010 — tranchée le 2026-08-01, voie A.** Comment un modèle
peut-il être validé par la recette du premier token alors que cette recette
exige qu'il soit déjà activé, et que l'activation exige la recette (COR-022) ?
Trois voies, aucune gratuite :

| Voie | Ce qu'elle donne | Ce qu'elle coûte |
|---|---|---|
| **A — activation provisoire puis retour arrière** | L'ordre devient `calibrate → enable → smoke_test → (échec : retour à `disabled`) → warmup`. C'est le modèle mental que le dépôt emploie déjà pour `update.sh`, éprouvé en conditions réelles (§0.10, rollback sur `generation:no_content`). | Un modèle non encore validé est **brièvement servable**. Sur un hôte qui reçoit du trafic pendant l'amorçage, un utilisateur peut l'atteindre. |
| **B — la recette valide la chaîne, pas le modèle** | On aligne `registry_writer` sur le planificateur : l'activation se contente de la calibration, et le `smoke_test` final atteste la chaîne complète. Aucun réordonnancement. | **Contredit le critère d'acceptation d'AUT-007**, qui est le garde-fou central du lot. Un modèle serait activé sans qu'aucun token n'ait jamais été produit par lui. |
| **C — la recette n'emprunte pas `/v1` pour valider** | Le smoke test par modèle passe par un chemin d'admission interne, l'activation suit, et le `smoke_test` public final reste. | **Contredit §10**, dont le point n° 1 est que la recette doit traverser le vrai chemin public — « un simple `/health` ou `/ready` ne suffit pas ». |

**Voie A retenue le 2026-08-01.** C'est la seule qui ne renonce ni au garde-fou
d'AUT-007 ni au chemin public de §10. Le coût est réel et assumé par écrit : une
fenêtre pendant laquelle un modèle non encore validé est servable. Elle est
bornable — l'amorçage précède l'ouverture du trafic, et le retour arrière est
déjà un mécanisme éprouvé du dépôt (§0.10). L'implémentation est suivie sous
COR-022, et devra faire de cette fenêtre une propriété **testée**, pas une
intention : un smoke test en échec doit reposer l'entrée en `enabled: false`, et
un test doit échouer si ce n'est pas le cas.

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
| 2026-07-31 | — | `6e576a8` | **Second déploiement réel sur deux VMs Debian 13** (§0.13) | Vague 5 exercée sur machine réelle ; SHA-256 du catalogue conformes, premier token en local et en cluster, COR-004 et panne de nœud vérifiés ; 2 défauts neufs, 4 confirmations 🔬 |
| 2026-08-01 | AUT-015 | `7296be8` | `pytest tests -q` (gateway) + 27 mutations rejouées | 1287 réussis ; contrat du journal d'exécution, une mutation survivante a révélé un plancher de version manquant 🔬 |
| 2026-08-01 | AUT-006 | `267aade`, `054eff6` | `pytest tests -q` (gateway) + 24 mutations + parité CI 3.11 | 1352 réussis ; aucun fichier ne porte son nom définitif sans être confronté au SHA du catalogue 🔬 |
| 2026-08-01 | AUT-016 | `00818e9` | `pytest tests -q` (gateway) + 40 mutations rejouées | 1364 réussis ; extraction défensive, bascule atomique, version relue sur le binaire posé et fail-closed 🔬 |
| 2026-08-01 | AUT-011 | `6133e3f` | `pytest tests -q` (gateway) + 26 mutations rejouées | 1362 réussis ; les sept conditions de M2 avec leur preuve, hypothèses restées visibles 🔬 |
| 2026-08-01 | AUT-007 | `ffab29c` | `pytest tests -q` (gateway) + 32 mutations rejouées | 1390 réussis ; entrée écrite désactivée, activation sur preuve typée, commentaires préservés 🔬 |
| 2026-08-01 | AUT-008 | `49987a7`, `02abcf8` | `pytest tests -q` (gateway) + 23 mutations rejouées | 1365 réussis ; pics relevés pendant la charge, mesure ratée jamais optimiste, déchargement en `finally` 🔬 |
| 2026-08-01 | AUT-009, AUT-010 | `318b0c7`, `bb9b701`, `3d7676f` | `pytest tests -q` (gateway) + 27 mutations rejouées | 1407 réussis ; flux sans contenu = échec, log d'usage exigé, borne de préchauffage dérivée 🔬 |
| 2026-08-01 | AUT-001 | `10e2f5e` | `pytest tests -q` (gateway) + mutation rejouée | 1703 réussis ; un comptage de jetons cesse d'être un secret, 3 contournements retirés, 30 tests rouges sans le correctif 🔬 |
| 2026-08-01 | AUT-015 | `82b8bc9` | `pytest tests -q` (gateway) + 3 mutations rejouées | 1706 réussis ; version de contrat du plan recoupée, clés racine fermées, durée nulle respectée 🔬 |
| 2026-08-01 | AUT-006 | `0836274` | `pytest tests -q` (gateway) + mutation rejouée | Le catalogue déclare le téléchargeur réellement employé ; le test recoupe la déclaration avec les imports 🔬 |
| 2026-08-01 | AUT-007/008/009 | `b8cc007` | `pytest tests -q` (gateway) + 6 mutations rejouées | Contrats de preuve réconciliés, producteurs alignés sur le consommateur, aucune couche de traduction 🔬 |
| 2026-08-01 | AUT-015 | `943a467`, `1dd5e00` | `pytest tests -q` (gateway) + 18 mutations + parité CI 3.11 | **1866 réussis** ; applicateur et `bootstrap-apply`, refus en bloc plutôt qu'exécution partielle 🔬 |
| 2026-08-01 | **Vague 6** | `1dd5e00` | Les 2 suites + `ruff` sur les deux composants | **1929 réussis** (1866 / 63) ; M2 **non prononcé**, bloqué par COR-020 🔬 |

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

### 0.13 Second déploiement réel sur deux VMs (2026-07-31)

Second parcours complet joué hors CI, sur deux VM **Debian 13** neuves
(4 vCPU, 3 Go de RAM, **sans GPU**, 26 Go libres) : `install.sh --mode local` sur
EvR-A, `install-agent.sh` sur EvR-B, puis migration `update.sh --mode cluster
--allow-mode-change`. `llama-server` **réellement compilé** (CPU, `GGML_CUDA=OFF`,
clone complet → build **10210**, version lisible) et les **deux GGUF du catalogue
d'amorçage** téléchargés à leur révision épinglée.

Ce que ce run ajoute au précédent (§0.10) : il exerce pour la première fois la
**vague 5** sur une machine réelle — `bootstrap-plan`, le catalogue, le résolveur
de runtime et le lecteur de header GGUF n'avaient jamais tourné hors des tests.

#### Ce que le run a validé 🔬

| Objet | Résultat observé |
|---|---|
| **Empreintes du catalogue (AUT-005)** | Les deux GGUF téléchargés correspondent **au bit près** aux SHA-256 épinglés. Première vérification terrain — c'était la revendication la plus risquée de la vague |
| **`bootstrap-plan` sans épinglage** | Bloqué, exit 1, **zéro étape** proposée |
| **`bootstrap-plan` épinglé + `--llama-bin`** | Reconnaît le binaire en place (build 10210 ≥ plancher), 15 étapes, **837,1 Mio annoncés = 838 Mo réellement présents** |
| **Lecture du header GGUF (AUT-013)** | Sur fichiers réels : `qwen2`, 24 blocs, 896 d'embedding, 14/2 têtes GQA, ctx 32768, 291 tenseurs, vocab 151936 |
| **Refus du mode cluster (vague 5)** | Exit 2 avec la conduite à tenir |
| Premier token via nginx + TLS | HTTP 200 en **5,09 s** à froid, réponse correcte |
| SSE réellement non bufferisé | deltas espacés de ~70 ms |
| Éviction LRU | chargement du second modèle → premier évincé |
| **COR-004** | **409 après 5 s de drain borné, aucun modèle déchargé, flux survivant jusqu'au bout** (2004 deltas + `DONE`) |
| OPS-009 | `nginx -t` silencieux ; `http2 on;` rendu **automatiquement** sur nginx 1.26.3 |
| COR-009 | timeouts nginx dérivés du registre à 900 s |
| OPS-008 | timer `active` et présent dans `list-timers` |
| SEC-008 | 0 occurrence du nom d'utilisateur sur 105 lignes de journal, contrôle positif inclus |
| OPS-010 | `venv` est un symlink, 1 release conservée |
| Panne de nœud | offline après 3 heartbeats, **503 au format OpenAI**, `/ready` 503, retour en ligne et rechargement transparents |
| Bascule cluster | `update.sh --allow-mode-change` : doctor avant/après + recette du premier token, exit 0 |
| Recette autonome | SUCCÈS, TTFT 4 ms à chaud |

Deux garde-fous se sont déclenchés **d'eux-mêmes, contre l'opérateur** : `doctor`
a refusé de valider une clé TLS posée en 0640 au lieu de 0600, et `update.sh` a
refusé un checkout git modifié.

#### Défauts trouvés — deux nouveaux, quatre confirmés

| Découverte | Preuve | Item |
|---|---|---|
| **`install-agent.sh` exige `rsync`, absent des prérequis documentés.** Le préflight le réclame et le script s'en sert deux fois, mais le mot n'apparaît **nulle part** dans `docs/deployment.md`. Sur une Debian 13 minimale il n'est pas installé : l'installation d'un nœud neuf échoue au premier écran. | Reproduit : `[ERREUR] Commande requise absente : rsync`, exit 1 🔬 | **OPS-011** |
| **Le plan propose de télécharger des artefacts qu'il a lui-même détectés comme présents.** L'inspection GGUF locale est rattachée à chaque modèle retenu (`local_inspection` non nulle, header lu), et pourtant les étapes `download_model` sont émises avec leur volume complet. Un opérateur qui applique le plan re-télécharge 837 Mio pour rien. | Reproduit sur EvR-A, GGUF déjà présents et vérifiés 🔬 | **AUT-014** |
| **`install.sh --mode local` exige toujours `nvidia-smi` sans échappatoire.** Deuxième run réel bloqué par ce préflight. Le §0.10 l'avait laissé « à trancher » ; deux bancs CPU s'y sont heurtés depuis. | Reproduit : le run a dû relâcher la ligne 149 du script pour installer 🔬 | **OPS-012** |
| **La queue d'admission reste inerte en mode cluster.** `/v1/capacity` renvoie `enabled: false, status: unavailable` alors que `CAPACITY_QUEUE_ENABLED=true`. Contredit l'invariant annoncé par `AGENTS.md` : « le mode cluster garde le même comportement public que le mode local ». Déjà relevé au §0.10, toujours vrai. | Reproduit sur l'orchestrateur en cluster 🔬 | **COR-019** |
| **`/admin/cluster` affiche encore les modèles d'un nœud hors ligne.** Pendant la panne, `online: false` et `consecutive_failures: 3` sont corrects, mais `loaded_models` continue d'annoncer un modèle chargé. Induit en erreur dans un diagnostic d'incident. Déjà relevé au §0.10. | Reproduit pendant la coupure du nœud 🔬 | **COR-018** |
| **COR-008 confirmé** : le corps du 429 est `{"detail":{"error":{…}}}` — double enveloppe. Le 503 cluster, lui, est correctement `{"error":{…}}`. La divergence annoncée par l'item est donc bien réelle et visible côté client. | Reproduit avec un quota abaissé à 3 req/min 🔬 | COR-008 (existant) |
| **SEC-001 confirmé** : le dashboard charge `cdn.jsdelivr.net` (chart.js) et `fonts.googleapis.com`. Il ne fonctionne pas hors ligne et adresse deux tiers à chaque ouverture. | Reproduit : 4 références externes dans le HTML servi 🔬 | SEC-001 (existant) |
| **Debian 13 reste hors matrice de support.** Les deux runs réels ont eu lieu dessus ; `docs/deployment.md` §1 ne cite qu'Ubuntu 22.04/24.04. | Constat 📖 | Décision produit, toujours ouverte (§0.8) |

#### Ce que ce run ne prétend pas avoir démontré

- **Le rollback n'a pas été rejoué.** L'injection d'une régression volontaire a
  été refusée par le garde-fou de l'environnement d'exécution, et le contournement
  n'a pas été tenté. Le rollback reste validé par le run du 30/07 (§0.10), pas par
  celui-ci.
- **Toujours aucun GPU** : la VRAM reste déclarative, et le préflight `nvidia-smi`
  a dû être relâché localement pour installer en mode local (voir OPS-012). Le
  checkout a été remis en état avant la bascule cluster — `update.sh` l'a d'ailleurs
  exigé.
- **`update-agent.sh` n'a pas été exercé** : le nœud a été installé, pas mis à jour.

### 0.14 Vague 6 — l'exécution du plan (jalon M2)

Cette vague livre les **exécuteurs** : les modules qui appliquent réellement le
plan que la vague 5 sait écrire. Elle est la première du projet à toucher au
disque, au réseau et au cycle de vie des modèles autrement que pour observer.

Elle commence par deux items que personne n'avait planifiés, et qui n'existaient
dans aucun des deux audits.

> **Note de renumérotation.** Cette vague a été développée en parallèle du
> second déploiement réel (§0.13), qui a ouvert ses propres items sur `dev`
> pendant ce temps. Trois identifiants sont entrés en collision à la fusion et
> ont été **décalés du côté de la vague 6**, `dev` faisant foi. Les messages de
> commit portent donc les identifiants d'avant le décalage — voici la table de
> correspondance, sans laquelle `git log` et ce document se contrediraient :
>
> | Dans les commits | Dans ce document | Objet |
> |---|---|---|
> | `AUT-014` | **AUT-015** | Contrat du journal d'exécution |
> | `AUT-015` | **AUT-016** | Installation du runtime |
> | `AUT-016` | **AUT-017** | Sondes de production |
> | `COR-018` | **COR-020** | `models.yaml` détruit par les mutations admin |
> | `COR-019` | **COR-021** | `assert` portant un invariant de production |
> | `COR-020` | **COR-022** | Chaîne de preuve nouée |
>
> Les identifiants du second déploiement — `AUT-014`, `COR-018`, `COR-019`,
> `OPS-011`, `OPS-012` — sont **inchangés** : ils étaient sur `dev` les premiers.

| Chantier | Items | Priorité | Fichiers possédés | Tests | État |
|---|---|---|---|---:|---|
| — | AUT-015 (contrat, préalable) | P0 | `bootstrap/execution.py` | +93 | `[x]` |
| D0 | AUT-016 | P0 | `bootstrap/runtime_installer.py` | +77 | `[x]` |
| D1 | AUT-006 | P0 | `bootstrap/downloader.py` | +65 | `[x]` |
| D2 | AUT-007 | P1 | `bootstrap/registry_writer.py` | +103 | `[x]` |
| D3 | AUT-008 | P1 | `bootstrap/calibration.py` | +78 | `[x]` |
| D4 | AUT-009, AUT-010 | P0 / P1 | `bootstrap/first_token.py`, `warmup.py` | +120 | `[x]` |
| D5 | AUT-011 | P1 | `bootstrap/install_report.py` | +75 | `[x]` |
| — | AUT-001 (défaut de contrat) | P0 | `bootstrap/schema.py` | +17 | `[x]` |
| — | AUT-015 (revue) | P1 | `bootstrap/execution.py` | +3 | `[x]` |
| — | AUT-006 (déclaration de licence) | P2 | `bootstrap/catalog.*` | +1 | `[x]` |

Les six chantiers ont été menés **en parallèle**, dans des worktrees git isolés,
avec propriété exclusive de leurs fichiers. Aucun ne s'importe. Chacun a dû
prouver la rougeur de ses tests en cassant réellement son code : **173 mutations
appliquées et rejouées** au total, pas seulement affirmées.

#### Deux items qui manquaient au backlog

- **AUT-015 — le contrat du journal d'exécution.** La revue de la vague 5 avait
  posé la question sans lui donner d'identifiant : « l'enjeu est M2, qui
  appliquera des plans relus depuis un fichier ; un applicateur ne doit jamais
  pouvoir être convaincu d'agir par un champ dérivé que personne ne recoupe »
  (§0.12). Sans ce contrat figé **avant** les chantiers, six modules auraient
  chacun inventé le leur — et la parallélisation aurait été impossible.
- **AUT-016 — l'installation du runtime.** Trou franc du backlog. AUT-003
  *résout* quelle variante de `llama-server` installer et s'arrête là ; aucun
  item ne portait la pose du binaire, alors que le jalon M2 l'exige en
  **première** condition. Ni l'audit Codex, ni l'audit Claude, ni cinq vagues
  d'implémentation ne l'avaient vu.

#### Le défaut qui a coûté trois contournements

`schema._SECRET_KEY_RE` cherchait « TOKEN » **en sous-chaîne**. Dans une
passerelle LLM, un jeton est d'abord une unité de facturation :
`completion_tokens`, `max_tokens`, `tokens_per_second` et `first_token_ms`
étaient donc tous déclarés « fuites », et le rendu refusait de publier le
document qui les portait.

Conséquence concrète : **le rapport de calibration prescrit par §9 était
littéralement impubliable**, puisqu'il exige `prompt_tokens_per_second`.

Trois chantiers ont buté dessus **indépendamment** — AUT-007, AUT-008 et
AUT-011 — et l'ont chacun contourné de son côté : alias `prompt_tps`, résumé
`digest()`, renommage en `*_units`. Aucun n'avait le droit de toucher à
`schema.py`, et chacun a donc documenté un défaut comme s'il s'agissait d'une
contrainte. C'est le coût réel de la propriété exclusive de fichiers : elle
empêche les collisions, elle n'empêche pas trois personnes de payer trois fois
la même dette.

Le motif est désormais ancré sur les frontières de composant — le singulier
reste un porteur d'authentification (`access_token`, `hf_token`, `token_file`),
le pluriel est un comptage — et les mesures dont le nom porte le singulier sont
exemptées nommément. Un nom non prévu **bloque la publication au lieu de fuir** :
la dégradation reste bruyante. Les trois contournements ont été retirés.
Rougeur : remettre le motif non ancré fait tomber 30 tests.

#### Trois trous dans le contrat d'exécution, trouvés par ses usagers

Aucun n'avait été vu par les 93 tests du contrat lui-même. C'est la limite d'un
contrat écrit avant ceux qui s'en servent.

| Découverte | Trouvé par | Traitement |
|---|---|---|
| **`plan_schema_version` était émise mais jamais relue.** Un journal archivé annonçant une version de plan arbitraire validait sans une erreur — le champ censé attester contre quel contrat l'exécution a eu lieu était décoratif. | AUT-011 | Corrigé 🔬 |
| **Les clés racine inconnues étaient acceptées.** Un rapport enrichi d'un « conclusion : tout est vert » que rien ne recoupe passait la validation. Exactement la classe de trou fermée en trois passes sur le contrat du plan en vague 5 — le journal, lui, était resté ouvert. | AUT-011 | Corrigé 🔬 |
| **`duration_ms` valait `0` par défaut et le lanceur écrasait toute valeur fausse** : un exécuteur mesurant honnêtement 0 ms était indistinguable d'un exécuteur muet. | AUT-008 | Corrigé — le défaut est `None`, et `0` est une mesure respectée 🔬 |

#### Défauts découverts pendant la vague 6

Ceux qui touchent du code hors du périmètre de la vague sont **signalés, pas
corrigés** : ils deviennent des items ou enrichissent ceux qui existaient.

| Découverte | Trouvé par | Traitement |
|---|---|---|
| **`proxy._stream_proxy` ne teste jamais `response.status_code` dans la branche streaming.** Le statut est lu puis utilisé **uniquement** pour le log d'usage ; le `StreamingResponse` est déjà parti avec un 200. Un `llama-server` qui répond 400/500 avec un corps JSON non-SSE voit ce corps relayé ligne par ligne, sous **HTTP 200**, sans préfixe `data:` ni `[DONE]`. Le client voit un succès, le journal enregistre un 502 : les deux vues divergent en silence. | AUT-009, en lisant le chemin qu'il devait exercer | **COR-013 précisé** — l'item existait, son libellé sous-estimait la portée 📖 |
| **La double enveloppe d'erreur est confirmée**, et `type`/`code` sont inversés par rapport à la convention OpenAI : `auth.py` lève `HTTPException(detail={"error": …})` sans handler dédié, donc `/v1/*` sort `{"detail": {"error": …}}` là où `proxy._openai_error` sort `{"error": …}`. Par ailleurs `_openai_error` met la classe machine dans `type` et le **statut HTTP en chaîne** dans `code` ; `_sse_error` n'émet aucun `code`. La même panne n'a pas la même forme avant et pendant le flux. | AUT-009 | **COR-008 précisé** 📖 |
| **`ModelRegistry._save()` détruit tous les commentaires de `models.yaml`.** `add()`, `update()`, `set_enabled()` et `remove()` — donc l'API admin et le dashboard — réécrivent le fichier par `yaml.dump`. Sur le fichier livré, cela efface 55 lignes d'en-tête opérationnel (budget VRAM, table RAM hôte, procédure de réactivation de `minimax-m2.7`). S'y ajoutent l'absence de sauvegarde et de `fsync`, et un basculement silencieux des permissions en 0600. **Perte de données en production, aujourd'hui.** | AUT-007 | Nouvel item **COR-020** 📖 |
| **`runtime_resolver._judge_existing_binary` fait confiance à un manifeste qu'il ne recoupe jamais contre le binaire.** Ni la version ni le commit du manifeste ne sont confrontés au build réellement rendu par `--version`, et aucune empreinte du binaire n'est comparée. Un manifeste recopié d'un autre hôte, ou survivant à un remplacement manuel du binaire, vaut donc attestation de provenance. | AUT-016 | **SEC-009 élargi** 📖 |
| **Des `assert` portent des invariants de production** dans `runtime_resolver` (`assert match is not None`, `assert variant is not None`). Sous `python -O` — que rien n'interdit dans une unité systemd — ils disparaissent et l'erreur devient un `AttributeError` opaque au lieu d'un refus nommé. | AUT-016 | Nouvel item **COR-021** 📖 |
| **`smoke_test.sh` passe le nom d'utilisateur en query string** (`GET /admin/usage?username=…`). Le nom y est éphémère et généré, donc sans donnée personnelle ; mais `main._redact_path()` redacte le **chemin**, pas la **requête**. Un vrai `username` passé à cette route atterrirait dans les journaux d'accès, ce que SEC-008 interdit. | AUT-009 | Nouvel item **SEC-010** 🧭 |
| **`schema.validate_plan_dict()` n'a pas de plancher de version** : seule une version *plus récente* est refusée. Un document portant `schema_version: 0` passe sans une erreur, et tout consommateur qui ne fait que valider traitera un plan d'un contrat antérieur comme un plan courant. | AUT-015, par une mutation qui a **survécu** | Contourné dans `execution` par une égalité stricte ; le contrat du plan reste ouvert 🔬 |
| **`schema.PlanSection.to_dict()` publie une référence, pas une copie** : muter le document rendu modifie le plan typé et donc tous les rendus ultérieurs. Un plan « immuable par contrat » est mutable par son propre rendu. | AUT-015 | Constat 🔬 — l'enjeu grandit en M2, où un applicateur manipule le document relu à côté du plan typé |
| **`catalog.CatalogFile.size_bytes` est optionnel** alors qu'aucune prévision d'espace disque n'est possible sans lui : une entrée peut être `pinned`, donc planifiable, tout en étant intéléchargeable. | AUT-006 | Refus explicite côté téléchargeur ; la cohérence voudrait que `size_bytes` entre dans la définition de l'épinglage 📖 |
| **Aucune variante de `DEFAULT_VARIANTS` ne porte `approx_bytes`** : le volume annoncé par le plan **ignore entièrement** le téléchargement du runtime. Un opérateur qui dimensionne son disque depuis le plan sous-compte de plusieurs centaines de Mo pour une variante CUDA. | AUT-016 | Constat 📖 |
| **Divergence de regex d'identifiant** entre le catalogue (3 à 64 caractères) et le registre (1 à 63). Une entrée de catalogue de 64 caractères est valide en amont et refusée en aval ; le pont ne le découvrirait qu'à l'écriture. | AUT-007 | Contrôlé côté `registry_writer`, non aligné en amont 📖 |
| **`models.yaml` livré : `gemma-4-26b-a4b` est `enabled: true` avec un chemin portant le commentaire « À ajuster selon le chemin réel ».** Un modèle activé dont le fichier est déclaré incertain fait échouer `/ready` (COR-005), donc `update.sh`. | AUT-007 | Constat 📖 — à trancher |
| **`codex-analyse.md` §9 n'ordonne pas le déchargement du modèle** entre la passe réduite et la passe cible, ni après. Suivre la spécification à la lettre laisse le modèle chargé, ce qui contredit les invariants de cycle de vie d'`AGENTS.md`. | AUT-008 | **Défaut de la spécification elle-même**, pas du code. Le déchargement a été ajouté ; §9 devrait l'inscrire 📖 |
| **Le catalogue déclarait un téléchargeur qui n'était pas celui employé** : `huggingface_hub` sous apache-2.0, alors qu'AUT-006 a délibérément écarté cette dépendance. §8 exige de distinguer la licence du logiciel de téléchargement — une déclaration fausse est pire qu'absente, elle a l'apparence d'une vérification. | AUT-006, en écrivant son manifeste de provenance | Corrigé — le test ne compare plus un nom en dur, il recoupe la déclaration avec les **imports réels** du module 🔬 |

#### L'assemblage, et le défaut que personne ne pouvait voir seul

Les six modules étaient irréprochables et **entièrement inertes** : aucun n'était
appelé par quoi que ce soit. Chaque chantier l'avait signalé dans les mêmes
termes — « le module existe comme bibliothèque, exactement comme AUT-004 avant
sa passe de revue ». L'assemblage livre `bootstrap/applier.py` et la commande
`python cli.py bootstrap-apply`, pendants M2 de `planner.py` et `bootstrap-plan`.

**La réconciliation des preuves était pire qu'annoncé.** `registry_writer` avait
dû définir seul la forme de la preuve qui autorise l'activation, ses producteurs
n'existant pas encore ; les deux producteurs ont livré autre chose (`model` vs
`model_id`, `http_code` vs `http_status`, `tested_at` vs `measured_at`). Mais le
consommateur **refuse aussi les clés inconnues**, et le rapport de §9 en porte
sept qu'il ignore : même parfaitement nommé, le bloc aurait été rejeté.

L'arbitrage retenu est d'**aligner les producteurs sur le contrat consommateur,
puis projeter strictement** — aucune couche de traduction nulle part. Une couche
de traduction serait un endroit de plus où quelqu'un finirait par être indulgent,
et l'activation est la seule action qui met un modèle en service. La projection
exige **toutes** les clés du consommateur et n'en prend **que** celles-là ; une
clé absente lève, elle ne vaut jamais `None`. Un seul écart était réellement
structurel : `endpoint` manquait, sans quoi le contrôle « la recette est passée
par le chemin public » de §10 est insatisfiable. Il est devenu une **constante
de module** et non un réglage — paramétrable, il aurait pu attester d'un chemin
privé.

**Puis l'assemblage a trouvé ce qu'aucun chantier ne pouvait voir.** Le plan
ordonne, par modèle, `download → write_registry → calibrate → enable_model →
warmup`, puis **un seul** `smoke_test` final. Or l'activation exige la preuve de
ce smoke test, et la recette de §10 passe par `/v1/chat/completions`, qui refuse
un modèle désactivé. **Aucun plan produit par `bootstrap-plan` ne peut aller à
son terme.** L'applicateur le constate avant toute exécution
(`chaine_de_preuve_impossible`) et refuse, plutôt que d'entamer un plan qu'il ne
peut pas finir. C'est **COR-022**, et c'est ce qui tient le jalon M2 ouvert ;
la décision est posée en **DEC-010**.

Deux autres incohérences ne se voyaient qu'une fois les modules côte à côte :
`verify_artifact` est **surchargée sur deux domaines** aux grammaires de cible
incompatibles alors que le registre n'admet qu'un exécuteur par action — laissée
en l'état, elle aurait produit un refus au message parfaitement plausible et
entièrement faux ; et l'étape `verify_artifact` du runtime est **structurellement
morte**, la vérification ayant lieu à l'intérieur d'`install_runtime`, avant
extraction.

#### Ce que l'orchestration a appris

Les worktrees fournis aux six chantiers ont été taillés depuis `main`, **66
commits en arrière**, sans le paquet `bootstrap` ni le contrat qu'ils devaient
consommer. Les six agents ont **refusé de travailler** et ont diagnostiqué la
cause au lieu d'inventer le contrat manquant.

Cela tient à une seule ligne du mandat : « vérifie d'abord que
`bootstrap/execution.py` existe ; s'il est absent, arrête-toi ». Sans cette
précondition, six chantiers auraient produit six définitions divergentes de
`StepExecutor` — et le défaut ne serait apparu qu'à la fusion, après des heures
de travail à jeter. **Une précondition vérifiable vaut mieux qu'une consigne
d'architecture**, aussi bien écrite soit-elle.

#### Ce que la vague 6 ne prétend pas avoir démontré

- **Toujours aucun GPU réel.** La VRAM reste entièrement déclarative. Le module
  qui *prétend mesurer* — `calibration` — n'a mesuré que des doubles : aucun
  `nvidia-smi` n'a été exécuté, aucun `llama-server` n'a été chargé, et les
  sondes qu'il définit n'ont **aucune implémentation** dans le dépôt.
- **Aucun réseau réel, aucun dépôt Hugging Face réel.** Le comportement effectif
  de HF n'est pas prouvé : chaîne de redirection vers le CDN, forme des
  `Content-Range`, réponses aux `Range` sur objets LFS, codes 401/403/416,
  coupures TCP en cours de flux. Les transports HTTP de production
  (`UrllibTransport`, des deux modules qui en ont un) ne sont **couverts par
  aucun test** — c'est le seul composant de production non testé de la vague.
- **Aucun nginx réel dans un test automatisé.** Le point n° 1 de §10 — « le test
  doit traverser le vrai chemin public » — est **codé, pas démontré**. La
  détection de SSE bufferisé n'a jamais vu un vrai buffering, et ses deux seuils
  sont des choix non validés terrain. Le chantier ajoute d'ailleurs que la
  bufferisation **n'est pas décidable depuis le client seul** : un backend
  rapide qui traite un long prompt puis répond en rafale produit la même
  signature.
- **Les formes de réponse des routes admin sont recopiées depuis la lecture du
  code**, pas obtenues d'un serveur. `/ready` est recoupé contre
  `readiness.ReadinessReport` réel ; **les cinq routes admin ne sont recoupées
  contre rien**, et une divergence de champ y passerait inaperçue.
- **Aucune archive de release amont réelle n'a été téléchargée** : la structure
  supposée des artefacts `llama-server` est une hypothèse. `DEFAULT_VARIANTS` ne
  contient aucune empreinte, donc **en configuration par défaut, l'installateur
  de runtime n'installe rien**.
- **Les marges sont des chiffres choisis, pas mesurés** : 10 % pour la
  calibration, 1,15 pour l'activation, 24 h de fraîcheur de preuve, 0,5 s
  d'intervalle d'échantillonnage. Aucune donnée ne les appuie.
- **Le prompt court de §9 ne remplit pas le cache KV.** C'est la limite la plus
  dangereuse de la calibration : le pic mesuré peut sous-estimer le régime
  permanent, et la spécification elle-même prescrit ce prompt court.
- **Aucune concurrence.** Deux applications simultanées sur le même
  `models_dir` ou le même `models.yaml` se marcheraient dessus : il n'existe
  aucun verrou. Une calibration menée pendant qu'un autre modèle est servi
  mesurerait la VRAM des deux.
- **L'atomicité est prouvée par construction, pas par expérience** : aucun test
  ne coupe le processus entre deux étapes, ne remplit un disque, ni ne simule un
  `os.replace` inter-volumes.
- **Les fixtures sont minuscules** — quelques centaines d'octets. Rien ne
  démontre le comportement sur un GGUF de 40 Go : coût du re-hachage à la
  reprise, coût du hachage d'idempotence à chaque exécution, pression mémoire.
- **Les releases et les venvs s'accumulent toujours.** L'installateur de runtime
  ne purge rien, comme `update.sh` avant OPS-010 et comme les sauvegardes
  `*.pre-migration.*.bak` (OPS-002).

#### 0.14.1 Sortie du jalon M2 — **non prononcée**

Conditions de §13 et leur état réel :

| Condition M2 | État |
|---|---|
| Runtime installé et vérifié | `[~]` — AUT-016 livré et testé : extraction défensive, bascule atomique, version relue sur le binaire posé, fail-closed. Mais `DEFAULT_VARIANTS` ne porte **aucune empreinte**, donc en configuration par défaut rien ne s'installe |
| Modèle téléchargé à révision figée | `[x]` — AUT-006, l'invariant central est verrouillé par 24 mutations |
| Licence acceptée | `[~]` — le mécanisme existe et refuse fail-closed, mais **aucun moyen de fournir une acceptation** depuis la CLI (AUT-017) |
| Calibration effectuée | `[~]` — AUT-008 : mécanique complète, **aucune sonde implémentée**, rien n'a jamais été mesuré |
| Modèle préchauffé | `[~]` — AUT-010 : borne dérivée du registre, mais rien n'appelle le module |
| Appel E2E réussi | `[~]` — AUT-009 : les douze points de §10 sont codés, **aucun nginx réel traversé**, aucun client HTTP concret |
| Rapport final produit | `[x]` — AUT-011, et il sait dire non |

**Décision de sortie : refusée.** Deux raisons, l'une structurelle et l'autre
matérielle :

1. **COR-022** — la chaîne de preuve du plan est nouée sur elle-même. Aucun plan
   produit par `bootstrap-plan` ne peut être appliqué jusqu'au bout. Tant que
   COR-022 n'est pas implémenté — DEC-010 tranche pour la voie A —, la
   condition « appel E2E réussi » est inatteignable par construction, pas par
   manque de code.
2. **AUT-017** — trois modules définissent le contrat de leurs sondes et le
   dépôt n'en contient aucune implémentation. `bootstrap-apply` ne câble que 5
   des 9 actions et refuse tout plan réel en code 2.

Ce que la vague **a** établi, et qui n'est pas rien : la mécanique complète
existe, elle est verrouillée par 672 régressions et 191 mutations rejouées, et
elle **refuse en bloc plutôt que d'entamer un plan qu'elle ne peut pas finir**.
Un applicateur qui échoue proprement à mi-chemin aurait été bien pire qu'un
applicateur qui refuse de commencer — c'est l'invariant sur lequel la vague a le
plus travaillé, et c'est celui qui tient.

La distance restante est honnête à énoncer : **le système sait décrire une
installation et sait l'exécuter en simulation ; il ne l'a jamais faite.**

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
| COR-008 | `[ ]` | P1 | Uniformiser les erreurs OpenAI | Auth, quota, chargement et upstream renvoient tous `{"error": ...}` sans double enveloppe, et `type`/`code` suivent la convention OpenAI. **Reproduit sur hôte réel le 2026-07-31** (§0.13) : un 429 de quota rend `{"detail":{"error":{…}}}` alors qu'un 503 cluster rend correctement `{"error":{…}}` — la divergence est visible côté client, sur deux chemins d'erreur du même service. **Cause établie et portée élargie le 2026-08-01** (§0.14) : `auth.py` lève `HTTPException(detail={"error": …})` et **aucun handler `HTTPException` n'existe**, donc FastAPI ré-enveloppe. S'y ajoutent deux écarts que le terrain n'avait pas montrés : `proxy._openai_error` met la classe machine dans `type` et le **statut HTTP en chaîne** dans `code`, à l'inverse de la convention OpenAI (un client SDK qui teste `err.code == "model_not_found"` échoue) ; et `_sse_error` code `"type": "server_error"` en dur **sans émettre de `code`**, de sorte que la même panne n'a pas la même forme avant et pendant le flux. |
| COR-009 | `[x]` | P1 | Aligner les routes nginx et FastAPI | Chaque route documentée est exposée ou supprimée de la documentation. **Livré le 2026-07-30** : `/ready` et `/completion` étaient documentés sur l'URL publique mais retombaient en 404 côté nginx ; les timeouts sont désormais dérivés du `load_timeout_seconds` maximal du registre (900 s) au lieu des 30 s qui faisaient échouer tout pré-chargement admin en 504. |
| COR-010 | `[ ]` | P1 | Corriger `revoke_key()` | `%` et `_` sont littéraux ou rejetés ; seconde révocation a un comportement défini. |
| COR-011 | `[–]` | — | ~~Corriger le slot student abandonné~~ | Annulé : composant supprimé (DEC-009). |
| COR-012 | `[ ]` | P1 | Définir une réservation de quota | Les requêtes concurrentes ne dépassent pas silencieusement le budget, ou le dépassement maximal accepté est borné et documenté. |
| COR-013 | `[ ]` | P1 | Préserver les erreurs upstream avant SSE | Une 4xx/5xx de `llama-server` ne devient pas un HTTP 200 ambigu ; contrat d'erreur streaming testé. **Portée corrigée le 2026-08-01** (§0.14) : le libellé sous-estimait le défaut. `proxy._stream_proxy` ne teste **jamais** `response.status_code` dans la branche streaming — il le lit et ne s'en sert que pour le log d'usage, le `StreamingResponse` étant déjà parti avec un 200. Un backend qui répond 400/500 avec un corps JSON non-SSE voit ce corps **relayé ligne par ligne**, sous HTTP 200, sans préfixe `data:` ni `[DONE]`. Le client conclut au succès, le journal enregistre un 502 : les deux vues divergent en silence. Le cas *timeout/connexion* est mieux traité (`_sse_error` émet bien une erreur SSE puis `[DONE]`), mais il ne couvre que les exceptions `httpx`, pas un statut HTTP d'erreur. |
| COR-014 | `[x]` | P0 | Rendre chargeables les réglages de liste de l'environnement | `ALLOWED_MODEL_DIRS`, `CORS_ALLOW_ORIGINS` et `ALLOWED_MODELS` acceptent la syntaxe documentée sans faire échouer le démarrage ; les fichiers d'exemple livrés sont chargeables. **Item ajouté le 2026-07-30**, découvert par AUT-012 : ces trois réglages étaient inutilisables tels que documentés. |
| COR-015 | `[x]` | P0 | Réparer le démarrage en mode cluster | `CLUSTER_MODE=cluster` démarre et sert ; la régression est verrouillée par TST-006. **Item ajouté le 2026-07-30** (§0.10). Correctif d'une ligne appliqué et vérifié sur VM (`n.node_id` → `n.id` dans `model_manager._build_manager()`), puis **verrouillé par TST-006** le 2026-07-30 : réintroduire `n.node_id` fait échouer 2 tests sur `AttributeError`. |
| COR-016 | `[x]` | P0 | Rendre `update-agent.sh` capable de réussir | Une mise à jour de node-agent aboutit sans rollback, et la stratégie de venv est la même que celle de `update.sh` (construction à l'emplacement final, bascule par symlink) plutôt qu'un déplacement de venv. **Item ajouté le 2026-07-30** (§0.10). Contournement appliqué et vérifié sur VM (`ExecStart` via `python -m uvicorn`, conservé en défense en profondeur), puis **correctif structurel livré** le 2026-07-30 : le venv est construit à son emplacement définitif et `venv-agent` devient un symlink que l'on bascule, comme dans `update.sh`. Un agent installé par l'ancien script est migré en place, sans action opérateur. Test de non-régression : un exécutable de `bin/` doit rester lançable après la bascule. |
| COR-017 | `[x]` | P0 | Rendre les redémarrages insensibles au start-limit systemd | Un rollback ne peut pas laisser le service en `failed` : chaque `systemctl start` des scripts de déploiement est précédé d'un `systemctl reset-failed`, et un échec de rollback est signalé comme une indisponibilité, pas comme un simple avertissement. **Item ajouté le 2026-07-30** (§0.10), **livré le même jour** : `reset-failed` avant chaque démarrage dans les 4 scripts de déploiement, et l'échec de redémarrage d'un rollback sort désormais en code 9 « INDISPONIBILITÉ », distinct du code 1 « version précédente restaurée et en service ». |

| COR-018 | `[ ]` | P2 | Cesser d'annoncer les modèles d'un nœud hors ligne | Pendant qu'un nœud est `online: false`, `/admin/cluster` ne présente plus ses `loaded_models` comme chargés : la liste est vidée ou explicitement marquée `unavailable`, et un test le verrouille avec un contrôle positif prouvant qu'elle sait afficher des modèles quand le nœud est en ligne. **Item ajouté le 2026-07-31** (§0.13), relevé une première fois au §0.10 et reproduit depuis : `online: false`, `consecutive_failures: 3`, et pourtant un modèle encore annoncé chargé. Le drapeau est juste, la liste ment — et c'est la liste qu'on lit en incident. |
| COR-019 | `[ ]` | P1 | Rendre la queue d'admission cohérente entre local et cluster | Soit la queue est portée en mode cluster et `/v1/capacity` y répond comme en local, soit son indisponibilité devient une **limite documentée** dans `docs/api.md` et `AGENTS.md`, dont la phrase « le mode cluster garde le même comportement public que le mode local » est alors amendée. Un test doit verrouiller le choix retenu. **Item ajouté le 2026-07-31** (§0.13) : `/v1/capacity` renvoie `enabled: false, status: unavailable` en cluster **malgré** `CAPACITY_QUEUE_ENABLED=true`. Un client qui interroge la capacité reçoit donc deux contrats publics différents selon une topologie qu'il ne connaît pas. |
| COR-020 | `[ ]` | P1 | Préserver `models.yaml` lors des mutations admin | Une mutation par l'API admin ou le dashboard conserve les commentaires du fichier, produit une sauvegarde, écrit atomiquement avec `fsync`, et laisse les permissions inchangées. **Item ajouté le 2026-08-01** (§0.14), découvert en livrant AUT-007 : `ModelRegistry._save()` réécrit le fichier entier par `yaml.dump`, ce qui efface les 55 lignes d'en-tête opérationnel du fichier livré — budget VRAM, table RAM hôte, procédure de réactivation de `minimax-m2.7` — et tous les commentaires d'entrée. Il ne sauvegarde pas, ne synchronise pas, et `NamedTemporaryFile` fait basculer le fichier en 0600 sans restaurer les permissions d'origine. Perte de données en production, aujourd'hui, sur un chemin emprunté par le dashboard. `bootstrap/registry_writer.py` montre la forme attendue : ajout textuel, reparse comparatif, refus plutôt que réécriture globale. |
| COR-021 | `[ ]` | P2 | Ne pas porter d'invariant de production par `assert` | Aucun `assert` ne garde un invariant dont la violation doit produire une erreur nommée. **Item ajouté le 2026-08-01** (§0.14), découvert en livrant AUT-017 : `runtime_resolver.ProvenanceManifest.build_number` et `to_decision()` s'appuient sur `assert match is not None` / `assert variant is not None`. Sous `python -O`, que rien n'interdit dans une unité systemd, ces gardes disparaissent et le refus explicite devient un `AttributeError` opaque. À rechercher ailleurs dans le dépôt avant de conclure. |
| COR-022 | `[ ]` | **P0** | Dénouer la chaîne de preuve du plan d'amorçage | Un plan produit par `bootstrap-plan` peut être appliqué **jusqu'à son terme** : aucune étape n'exige une preuve qu'une étape ultérieure produit. **Item ajouté le 2026-08-01** (§0.14), découvert à l'assemblage de la vague 6 — aucun chantier ne pouvait le voir seul. Le planificateur (vague 5) ordonne, par modèle, `download → write_registry → calibrate → enable_model → warmup`, puis ajoute **un seul** `smoke_test` final, décrit comme « unique et finale — elle valide la chaîne, pas un modèle en particulier ». Or le critère d'acceptation d'AUT-007 exige l'inverse : « entrée désactivée tant que la calibration **et le smoke test** n'ont pas réussi », donc une preuve **par modèle**, **avant** l'activation. Et la recette de §10 passe par `/v1/chat/completions`, qui refuse un modèle `enabled: false` (`model_disabled`). Les trois contraintes ne peuvent pas être vraies ensemble. **Bloque la sortie du jalon M2.** **Décision prise le 2026-08-01 — voie A** (DEC-010) : activation provisoire puis retour arrière si la recette échoue. Reste à implémenter, et la fenêtre d'exposition doit être une propriété **testée** : un smoke test en échec repose l'entrée en `enabled: false`, et un test échoue si ce n'est pas le cas. |

### Lot B — bootstrap automatisé

| ID | État | Priorité | Action | Critère d'acceptation |
|---|---|---|---|---|
| AUT-001 | `[x]` | P0 | Définir le schéma du plan de bootstrap | Schéma versionné, validé, sans secret, lisible avant application. **Livré le 2026-07-31** : `bootstrap/schema.py` porte le contrat (`PLAN_SCHEMA_VERSION`, `validate_plan_dict()` avec chemins de champs fautifs, `find_secret_leaks()` à deux filets — par nom de champ ET par forme de valeur —, `render_human()`/`render_json()` refusant tous deux de publier un document qui fuit), `bootstrap/planner.py` l'assemble et `python cli.py bootstrap-plan` l'expose. Aucune écriture, aucun téléchargement. Un plan **bloqué ne décrit aucune étape**, quelle qu'en soit la cause, et la validation rejette tout document qui porterait les deux. `--mode cluster` est refusé explicitement au jalon M1 : la planification des nœuds reste à faire, et un plan local présenté comme cluster serait cohérent et faux. |
| AUT-002 | `[x]` | P0 | Inventaire matériel automatique | CPU/RAM/disque/GPU/VRAM/driver/backend détectés et testés sur la matrice supportée. **Livré le 2026-07-31** : document §5 littéral, toutes les sondes injectables, `CUDA_VISIBLE_DEVICES` respecté y compris sa troncature, `--hardware-profile` validé à la frontière. Quatre issues GPU distinctes au lieu de deux — un `nvidia-smi` qui **échoue** rend une liste de backends **vide**, jamais `cpu` : c'est le refus du repli silencieux. |
| AUT-003 | `[x]` | P0 | Résolveur `llama-server` | Sélection versionnée, SHA vérifié, aucun fallback CPU silencieux. **Livré le 2026-07-31** : ordre de §6 testé étape par étape jusqu'au refus explicite, recherche backend-d'abord pour qu'une archive CPU officielle ne l'emporte jamais sur une image CUDA officielle, variante non épinglée inéligible, manifeste de provenance qui ne peut pas être incohérent par construction, `LLAMA_SERVER_MIN_BUILD` généré par `derive_min_build()` et fail-closed. Le clone superficiel de §0.10 est reconnu et nommé pour ce qu'il est. |
| AUT-004 | `[x]` | P1 | Adapter LLMfit JSON | Version figée, schéma validé, timeout, fallback manuel et tests avec sorties enregistrées. **Livré le 2026-07-31** : binaire refusé si son empreinte ne correspond pas à l'épinglage, validation stricte et bornée de la sortie JSON, timeout borné, profil manuel passant par la MÊME validation, absent = `skip` et non échec. Trois barrières structurelles empêchent une recommandation d'activer un modèle seule, testées sur l'AST du module. Intégré au parcours opérateur le même jour, après revue : `--llmfit-bin`, `--llmfit-version`, `--llmfit-sha256`, `--llmfit-timeout`, `--llmfit-profile` et `--no-llmfit` — l'adaptateur était auparavant inatteignable depuis la CLI. **Réserve** : les fixtures sont synthétiques, le dépôt amont ne publiant aucun exemple de sortie — à remplacer par de vraies captures avant production (§0.12). |
| AUT-005 | `[x]` | P0 | Créer le catalogue approuvé | Révision, fichiers, SHA, licence, paramètres et ressources pour chaque modèle proposé. **Livré le 2026-07-31** : `bootstrap/catalog.yaml`, deux entrées apache-2.0 des deux côtés de la chaîne de licence, révisions et SHA-256 **réels** (relevés sur l'API publique Hugging Face, recoupés par `X-Linked-Etag`, revérifiés à la fusion). Entrée non épinglée = **fail-closed** : listée, mais exclue de toute planification de téléchargement. L'ensemble split/`mmproj` est indivisible par construction, pas par consigne. |
| AUT-006 | `[x]` | P0 | Télécharger les modèles de façon sûre | Reprise, espace disque, fichier temporaire, SHA, renommage atomique et provenance. **Livré le 2026-08-01** : aucun fichier ne porte son nom définitif sans avoir été confronté au SHA-256 **du catalogue** — écriture en `.part` dans le même répertoire, empreinte calculée en flux, `os.replace` ensuite. Empreinte fausse : le partiel est **détruit**, jamais « réparé » ni repris plus tard comme sain ; corps tronqué : il est conservé, la reprise est légitime. Reprise conditionnée à une preuve d'origine (dépôt, révision, nom, empreinte) et au recoupement du `Content-Range`. Espace disque refusé **en amont**, marge `max(5 %, 1 Gio)`. Acceptation de licence fournie par l'opérateur, jamais déduite d'une licence permissive. `huggingface_hub` évalué puis **écarté** (requests/filelock/fsspec/tqdm dans le chemin d'un outil qui tourne avant le venv de la gateway ; et il possède le temporaire, le renommage et vérifie l'ETag du serveur là où nous voulons l'empreinte que ce catalogue a épinglée après revue). **Réserve** : `UrllibTransport`, le transport de production, n'est couvert par aucun test. |
| AUT-007 | `[x]` | P1 | Générer `models.yaml` | Entrée désactivée tant que la calibration et le smoke test n'ont pas réussi. **Livré le 2026-08-01** : l'entrée est écrite `enabled: false` sans paramètre permettant l'inverse, et l'activation est une action distincte qui exige une preuve typée, datée, fraîche, et dont les trois empreintes sont recoupées. Le fichier candidat est **validé par `ModelRegistry` lui-même** avant `os.replace` — c'est la leçon de COR-014. Ajout textuel avec reparse comparatif plutôt que `yaml.dump` global : les 55 lignes d'en-tête opérationnel du fichier livré sont préservées, et si l'ajout ne produit pas exactement le document attendu on **refuse** au lieu de se replier sur la réécriture. Une entrée modifiée par l'exploitant n'est jamais écrasée ; la calibration ne peut que **relever** `vram_gb`, jamais l'abaisser. |
| AUT-008 | `[~]` | P1 | Calibrer RAM/VRAM | Mesures avant/après, pics et marge enregistrés par fingerprint. **Mécanique livrée le 2026-08-01**, item maintenu `[~]` : les neuf étapes de §9 sont appliquées dans leur ordre littéral, les pics sont relevés **pendant** la charge par une tâche concurrente bornée trois fois, la mesure ne retombe **jamais** sur l'estimation statique en la présentant comme mesurée, le modèle est déchargé dans un `finally`, et la réutilisabilité d'une preuve est une décision testable qui dit laquelle des trois empreintes diverge. Le module **mesure et propose**, il n'écrit jamais `models.yaml`. **Pourquoi `[~]` et non `[x]`** : le module définit le contrat de ses sondes et le dépôt n'en contient **aucune implémentation** — ni `nvidia-smi`, ni chargement de modèle, ni prompt. Rien n'a jamais été mesuré. Voir AUT-017. |
| AUT-009 | `[~]` | P0 | Recette premier token | Appel public complet avec TTFT mesuré et rapport sans secrets. **Mécanique livrée le 2026-08-01**, item maintenu `[~]` : les douze points de §10 sont appliqués, un flux sans delta de contenu est un **échec** (COR-006) et `no_content` prime sur `no_done`, la réussite exige aussi le **log d'usage** — une génération non facturée n'est pas un succès —, l'identité éphémère est nettoyée dans un `finally` conformément à DEC-001, et le nom d'utilisateur est toujours généré, aucun nom réel ne pouvant entrer dans un rapport. Les codes de cause sont alignés sur `deploy/smoke_test.sh`, que ce module **ne remplace pas** : le script reste le filet lançable en incident sans venv Python et le seul à exercer le vrai TLS/nginx. **Pourquoi `[~]`** : aucun client HTTP concret n'est fourni, aucun nginx réel n'est traversé par un test, et les formes de réponse des cinq routes admin sont recopiées depuis la lecture du code, recoupées contre rien. Voir AUT-017. |
| AUT-010 | `[~]` | P1 | Pré-chauffer le modèle par défaut | Le premier utilisateur ne déclenche pas le chargement après un déploiement réussi. **Mécanique livrée le 2026-08-01**, item maintenu `[~]` : la borne d'attente est **dérivée du registre** et non inventée — le piège de §0.9, où un chargement peut demander 300 s —, un dépassement est un échec explicite et non une attente infinie, l'absence de sonde de génération **ferme** le trafic, et l'ancienne version est conservée tant que le feu vert n'est pas donné. **Pourquoi `[~]`** : rien n'appelle ce module. `main.py`, `install.sh` et `update.sh` ne le connaissent pas, et `derive_warmup_timeout_seconds()` reçoit ses valeurs de l'appelant sans que le câblage vers `model_registry` existe. |
| AUT-011 | `[x]` | P1 | Produire le rapport d'installation | Versions, empreintes, licences, matériel, modèle, performances et contrôles. **Livré le 2026-08-01** : le rapport agrège le plan exécuté et le journal d'exécution, dit lesquelles des **sept conditions du jalon M2** sont satisfaites et par quelle preuve, et **ne prétend jamais plus que ce qui a été fait** — un journal en simulation ne satisfait aucune condition d'installation. Il distingue le constat de l'hypothèse : les variantes d'artefact retenues sur hypothèse restent visibles comme telles, ce qu'un lecteur pressé prendrait sinon pour un fait vérifié. Champs récapitulatifs recalculés et comparés élément par élément, jeu de clés racine fermé, refus de rendre un document qui fuit. Indexation par **nom de champ** et non par chemin producteur : un index par chemin se briserait en silence à la première réorganisation, en rendant une liste vide qui se lit « aucune licence ». |
| AUT-012 | `[x]` | P0 | Ajouter `evaruntime doctor` | Rapport humain/JSON et exit codes couvrant secrets, runtime, GPU, modèles, ports, DB, nginx, TLS fourni et limites systemd. |
| AUT-013 | `[x]` | P1 | Inspecter les métadonnées GGUF | Architecture, tenseurs, contexte et KV alimentent une estimation conservatrice sans être présentés comme une mesure exacte. **Livré le 2026-07-31** : parseur maison en bibliothèque standard, bornes explicites sur chaque champ de longueur venu du fichier (un GGUF est une entrée non fiable), validé contre deux vrais headers récupérés par requête `Range`. Le mot « estimation » figure dans le nom des types, dans le rendu et dans la liste des facteurs ignorés. Paquet `gguf` officiel évalué puis écarté (`numpy` sur une machine vierge), conclusion écrite dans le docstring. |
| AUT-015 | `[x]` | P0 | Définir le contrat du journal d'exécution | Un plan relu depuis un fichier n'est exécutable qu'après revalidation complète ; le journal distingue fait, déjà satisfait, sauté, échoué et non tenté ; verdict et compteurs sont recalculés, jamais crus. **Item ajouté le 2026-08-01**, né de l'ouverture de la vague 6 : AUT-001 s'arrête à la publication du plan, et la revue de la vague 5 avait explicitement laissé la question à M2 — « un applicateur ne doit jamais pouvoir être convaincu d'agir par un champ dérivé que personne ne recoupe » (§0.12). Sans ce contrat, six chantiers d'exécution auraient chacun inventé le leur. |
| AUT-016 | `[x]` | P0 | Installer le runtime résolu | L'artefact est vérifié avant d'être rendu exécutable, l'extraction d'archive est défensive, la bascule est atomique, la version est relue depuis le binaire posé et confrontée à l'épinglage, l'installation est idempotente et réversible, et un manifeste de provenance est écrit à côté du binaire. **Item ajouté le 2026-08-01** : trou du backlog découvert en ouvrant la vague 6. AUT-003 **résout** quelle variante de `llama-server` installer et s'arrête là ; aucun item ne portait l'installation elle-même, alors que le jalon M2 l'exige en **première** condition (« runtime installé et vérifié »). |
| AUT-017 | `[ ]` | **P0** | Implémenter les sondes de production du bootstrap | Les contrats de sonde définis par AUT-008, AUT-009 et AUT-010 ont une implémentation réelle, et `bootstrap-apply` peut câbler les neuf actions du plan depuis la CLI. **Item ajouté le 2026-08-01** (§0.14), révélé par l'assemblage : trois modules définissent proprement le contrat de leurs sondes — lecture VRAM/RAM, chargement et déchargement de modèle, prompt court, client HTTP vers la gateway — et **le dépôt n'en contient aucune implémentation**. Conséquence directe : `bootstrap-apply` ne câble que 5 des 9 actions et refuse en code 2 tout plan réel. C'est la différence entre « la mécanique est écrite » et « la machine peut s'installer ». Couvre aussi la reconstitution d'une `RuntimeResolution` depuis un document de plan, sans laquelle `install_runtime` n'est pas câblable, et un moyen de fournir une acceptation de licence à `accept_license`. |

| AUT-014 | `[ ]` | P1 | Reconnaître un artefact déjà présent et vérifié | Quand un fichier du catalogue est **déjà présent** au chemin cible et que son SHA-256 correspond à l'entrée épinglée, le plan ne propose plus son téléchargement : l'étape devient une `verify_artifact` seule, le volume annoncé est décompté, et le motif est écrit dans le détail de l'étape. Un test doit couvrir les trois cas — absent, présent et conforme, présent mais **empreinte divergente** (qui doit rester bloquant, jamais silencieusement réutilisé). **Item ajouté le 2026-07-31** (§0.13), reproduit sur un hôte réel : le planificateur avait bien lu le header des deux GGUF présents — `local_inspection` non nulle — et proposait pourtant 837,1 Mio de téléchargement. L'information manquait au raisonnement, pas à la collecte. |

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
| SEC-001 | `[ ]` | P0 | Vendoriser les assets admin et ajouter CSP | Dashboard fonctionnel hors ligne, aucune ressource tierce, CSP testée. **Reproduit sur hôte réel le 2026-07-31** (§0.13) : le HTML servi porte 4 références externes — `cdn.jsdelivr.net` pour chart.js et `fonts.googleapis.com` pour deux polices. Sur un réseau sans sortie Internet, le dashboard d'administration est donc inutilisable au moment précis où l'on en a besoin. |
| SEC-002 | `[ ]` | P0 | Durcir l'environnement généré | Allowlist modèle, CORS explicite et build minimum visibles dans le fichier généré. |
| SEC-003 | `[ ]` | P0 | Verrouiller les dépendances | Installation reproductible, dépendances dev séparées, hashes ou artefacts contrôlés. |
| SEC-004 | `[ ]` | P0 | Rendre l'audit CVE bloquant | Politique d'exception documentée avec expiration. |
| SEC-005 | `[ ]` | P1 | Imposer l'intégrité des modèles approuvés | Aucun modèle catalogue ne charge sans SHA/provenance. |
| SEC-006 | `[ ]` | P1 | Sécuriser le data-plane cluster | Prompts chiffrés ou réseau isolé attesté et contrôlé. |
| SEC-007 | `[ ]` | P1 | Produire SBOM et attestations runtime | Chaque release et binaire redistribué possède provenance et notices. |
| SEC-009 | `[ ]` | P1 | Unifier la politique fail-closed de `LLAMA_SERVER_MIN_BUILD` | Une version de `llama-server` illisible alors qu'un build minimal est exigé refuse le démarrage, quel que soit le chemin emprunté. **Item ajouté le 2026-07-31** (§0.12), découvert en livrant AUT-003 : la politique existe en trois endroits avec deux sémantiques — `doctor` est fail-closed comme l'exige §6, `main._validate_inference_runtime` et `llama_version.enforce_llama_min_build()` ne le sont pas. Une gateway démarrée sans passer par `doctor` peut donc servir sur un binaire inattestable (cf. GHSA-8947-pfff-2f3c). Couvre aussi l'hypothèse `_VERSION_RE` : le premier motif `version\|build` de la sortie peut être une ligne d'initialisation de backend, pas la ligne de build. **Élargi le 2026-08-01** (§0.14), en livrant AUT-016 : `runtime_resolver._judge_existing_binary` accorde `reuse_existing` sur la foi d'un manifeste **qu'il ne recoupe jamais contre le binaire** — ni la version ni le commit déclarés ne sont confrontés à ce que rend `--version`, et aucune empreinte du binaire n'est comparée. Un manifeste recopié d'un autre hôte, ou survivant à un remplacement manuel du binaire, vaut donc attestation de provenance. `runtime_installer` ferme le trou de son côté en recalculant l'empreinte à chaque passe ; le résolveur reste exposé. |
| SEC-010 | `[ ]` | P2 | Rédiger la query string dans les journaux d'accès | Un nom d'utilisateur passé en paramètre de requête n'apparaît pas plus dans les journaux qu'un nom passé dans le chemin. **Item ajouté le 2026-08-01** (§0.14), découvert en livrant AUT-009 : `main._redact_path()` redacte le **chemin**, pas la **requête**, alors que `GET /admin/usage?username=…` existe et est employé par `deploy/smoke_test.sh`. Le nom y est éphémère et généré, donc sans donnée personnelle — mais un opérateur qui interroge cette route avec un vrai nom écrirait ce nom au journal, ce que SEC-008 interdit. À confirmer par lecture de `_redact_path` avant d'écrire le correctif. |
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
| OPS-011 | `[ ]` | P1 | Aligner les prérequis documentés sur ce que les scripts exigent réellement | `docs/deployment.md` §1 liste **toutes** les commandes réclamées par les préflights d'`install.sh` et d'`install-agent.sh`, et un test dérive la liste attendue du code des scripts plutôt que de la recopier — sans quoi il rate la prochaine dépendance ajoutée. **Item ajouté le 2026-07-31** (§0.13) : `install-agent.sh` exige `rsync` (préflight ligne 76, usage lignes 122 et 126), le mot n'apparaît nulle part dans la documentation, et une Debian 13 minimale ne l'installe pas — l'installation d'un nœud neuf échoue au premier écran, sur une dépendance que rien n'annonce. |
| OPS-012 | `[ ]` | P1 | Donner une échappatoire explicite au préflight GPU d'`install.sh` | `install.sh --mode local` accepte un hôte sans GPU via une option **explicite** (`--allow-no-gpu`), qui inscrit ce choix dans l'environnement généré et le fait remonter par `doctor` ; sans l'option, le refus actuel est conservé et son message dit quoi faire. Un test couvre les deux branches. **Item ajouté le 2026-07-31** (§0.13) : le préflight `command -v nvidia-smi` a bloqué **les deux** déploiements réels sur banc CPU (§0.10 puis §0.13), et les deux ont dû contourner le script. Un garde-fou que tout le monde contourne ne protège plus personne — il apprend seulement à passer outre. |
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
