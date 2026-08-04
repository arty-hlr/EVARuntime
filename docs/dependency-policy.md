# Politique des dépendances Python

Les fichiers `gateway/requirements.txt`, `node_agent/requirements.txt` et
`requirements-dev.txt` expriment les contraintes maintenues par les humains.
Les installations, mises à jour et jobs CI consomment exclusivement leurs
fichiers `*.lock`, résolus pour Python 3.11 et porteurs d'une empreinte SHA-256
pour chaque distribution.

## Mettre à jour une dépendance

Depuis la racine du dépôt, avec `uv` :

```bash
uv pip compile gateway/requirements.txt --universal --python-version 3.11 \
  --generate-hashes --output-file gateway/requirements.lock
uv pip compile node_agent/requirements.txt --universal --python-version 3.11 \
  --generate-hashes --output-file node_agent/requirements.lock
uv pip compile requirements-dev.txt --universal --python-version 3.11 \
  --generate-hashes --output-file requirements-dev.lock
```

La PR doit montrer le diff des versions, exécuter les deux suites et laisser le
job `dependency-audit` vert. Les scripts de production utilisent
`pip --require-hashes`; modifier uniquement un fichier de contraintes n'affecte
donc jamais silencieusement un hôte.

## Vulnérabilités et exceptions

`scripts/dependency_audit.py` audite les deux lockfiles avec `pip-audit`. Toute
vulnérabilité rend la CI rouge. Une exception est un dernier recours et doit être
ajoutée dans `.github/dependency-audit-exceptions.txt` sous la forme :

```text
GHSA-xxxx-xxxx-xxxx|2026-08-31|SEC-123|raison technique et chemin non atteignable
```

Chaque exception exige un identifiant, une date d'expiration ISO, une référence
de suivi et une justification. Le runner refuse les lignes expirées, incomplètes
ou dupliquées avant même d'appeler l'auditeur. Une exception doit être supprimée
dès qu'une version corrigée est disponible ; repousser sa date demande une
nouvelle revue explicite. Le dépôt ne contient actuellement aucune exception.
