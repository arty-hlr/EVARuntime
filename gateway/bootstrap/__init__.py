"""
`eva-bootstrap` — planificateur d'amorçage (Lot B, jalon M1).

Ce paquet est la couche NON privilégiée du parcours « machine vierge → premier
token » (§5 de `codex-analyse.md`). `gateway/deploy/install.sh` reste la couche
privilégiée : utilisateurs système, répertoires, venv, systemd, nginx. Ici on ne
fait qu'inventorier, résoudre, recommander et **écrire un plan** — jamais
l'appliquer. Aucun module de ce paquet ne doit acquérir de privilèges, ni écrire
hors d'un chemin explicitement fourni par l'appelant.

Découpage — un module par producteur, tous indépendants les uns des autres :

| Module               | Item    | Rôle                                          |
|----------------------|---------|-----------------------------------------------|
| `schema`             | AUT-001 | Contrat du plan : structure, validation, rendu |
| `inventory`          | AUT-002 | Inventaire matériel (CPU/RAM/disque/GPU)      |
| `runtime_resolver`   | AUT-003 | Résolution de `llama-server` et provenance    |
| `llmfit`             | AUT-004 | Adaptateur JSON du conseiller LLMfit          |
| `catalog`            | AUT-005 | Catalogue de modèles approuvés et licences    |
| `gguf_meta`          | AUT-013 | Lecture du header GGUF                        |
| `planner`            | AUT-001 | Assemblage du plan à partir des producteurs   |

Les producteurs ne s'importent pas entre eux : ils exposent chacun une fonction
de collecte et une projection `to_plan_section()` vers le contrat de `schema`.
Seul `planner` les connaît tous. Cette règle est ce qui permet de faire évoluer
un producteur — ou de le mettre en échec — sans casser les autres.
"""
