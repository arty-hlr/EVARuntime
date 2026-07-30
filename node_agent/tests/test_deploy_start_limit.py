"""
Insensibilité des scripts de déploiement node-agent au start-limit systemd
(COR-017, part `node_agent`).

Constaté lors du premier déploiement réel : après plusieurs redémarrages
rapprochés, `systemctl start` s'est heurté au start-limit
(« Start request repeated too quickly »). Le service est resté `failed`, le nœud
indisponible, et une intervention manuelle a été nécessaire — le dépôt ne
contenait aucun `systemctl reset-failed`.

Le contrat vérifié ici :

1. tout `systemctl start` d'un script de déploiement est précédé d'un
   `systemctl reset-failed` (no-op sur une unité saine, donc sans risque) ;
2. l'échec du redémarrage de rollback est traité comme une INDISPONIBILITÉ,
   pas comme un simple avertissement, et n'est pas masqué par un `|| true`.

C'est une lecture des artefacts de production : ils ne sont jamais exécutés
contre l'hôte. Chaque assertion d'absence est donc doublée d'un contrôle positif
prouvant que le test voit bien quelque chose.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

DEPLOY_DIR = Path(__file__).resolve().parents[1] / "deploy"
UPDATE_SCRIPT = DEPLOY_DIR / "update-agent.sh"

START_RE = re.compile(r"^\s*systemctl\s+start\b")
RESET_RE = re.compile(r"^\s*systemctl\s+reset-failed\b")

# Fenêtre de lignes amont dans laquelle le reset-failed doit apparaître. Assez
# large pour tolérer un commentaire explicatif, assez étroite pour qu'un
# reset-failed d'un autre bloc ne compte pas.
LOOKBEHIND = 8


def _scripts() -> list[Path]:
    return sorted(DEPLOY_DIR.glob("*.sh"))


def test_deploy_scripts_are_discoverable() -> None:
    """Contrôle positif : sans script trouvé, tout ce qui suit serait inerte."""
    names = {path.name for path in _scripts()}
    assert {"install-agent.sh", "update-agent.sh"} <= names, names


@pytest.mark.parametrize("script", _scripts(), ids=lambda path: path.name)
def test_every_systemctl_start_is_preceded_by_reset_failed(script: Path) -> None:
    lines = script.read_text(encoding="utf-8").splitlines()
    starts = [index for index, line in enumerate(lines) if START_RE.match(line)]

    for index in starts:
        window = lines[max(0, index - LOOKBEHIND):index]
        assert any(RESET_RE.match(line) for line in window), (
            f"{script.name}:{index + 1} — `systemctl start` sans "
            f"`systemctl reset-failed` en amont : un service laissé `failed` par "
            f"un incident précédent ne redémarrera pas (start-limit systemd).\n"
            + "\n".join(f"  {line}" for line in window + [lines[index]])
        )


def test_at_least_one_start_is_actually_guarded() -> None:
    """
    Contrôle positif du test précédent : il est paramétré sur des fichiers et
    passerait tout autant si plus aucun `systemctl start` n'existait.
    """
    guarded = 0
    for script in _scripts():
        lines = script.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            if START_RE.match(line) and any(
                RESET_RE.match(previous)
                for previous in lines[max(0, index - LOOKBEHIND):index]
            ):
                guarded += 1
    assert guarded >= 2, (
        "update-agent.sh doit garder au moins deux `systemctl start` "
        f"(bascule et rollback); {guarded} trouvé(s)."
    )


def test_rollback_restart_failure_is_reported_as_an_outage() -> None:
    body = UPDATE_SCRIPT.read_text(encoding="utf-8")

    assert "EXIT_SERVICE_DOWN=9" in body, (
        "update-agent.sh doit exposer un code de sortie distinct pour "
        "l'indisponibilité : un rollback réussi et un nœud à terre ne peuvent "
        "pas sortir avec le même code."
    )
    assert "INDISPONIBILITÉ" in body, (
        "L'échec du redémarrage de rollback doit être signalé comme une "
        "indisponibilité, pas comme un simple avertissement."
    )

    masked = [
        line.strip()
        for line in body.splitlines()
        if START_RE.match(line) and "|| true" in line
    ]
    assert not masked, (
        f"Un `systemctl start` de rollback masque son échec : {masked}"
    )
