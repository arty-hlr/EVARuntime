"""
Tests de la stratégie de venv de `node_agent/deploy/update-agent.sh` (COR-016).

Défaut d'origine, reproduit lors du premier déploiement réel sur deux VMs :
le venv était construit dans un `mktemp -d .agent-update.XXXXXX` puis DÉPLACÉ
vers `venv-agent`. Un venv n'est pas relogeable — les scripts console de `bin/`
portent un shebang absolu vers `<venv>/bin/python` — donc `bin/uvicorn`
continuait de pointer vers le staging (mode 0700, puis supprimé) :
`203/EXEC Permission denied`, cinq health-checks en échec, rollback. Le
symptôme désignait le mauvais coupable, car `ExecStartPre` utilise
`bin/python`, un lien vers l'interpréteur système qui survit au déplacement.

Ces tests exercent POUR DE VRAI la propriété cassée : un vrai venv est
construit, la stratégie du script (celle de `deploy/agent-venv-lib.sh`, sourcée
telle quelle par `update-agent.sh`) est appliquée, puis un exécutable de `bin/`
est réellement lancé. Réintroduire un déplacement de venv les fait échouer sur
`bad interpreter`.

Ni GPU, ni systemd, ni root : tout se passe dans un répertoire temporaire.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

DEPLOY_DIR = Path(__file__).resolve().parents[1] / "deploy"
VENV_LIB = DEPLOY_DIR / "agent-venv-lib.sh"
UPDATE_SCRIPT = DEPLOY_DIR / "update-agent.sh"

# Script console présent dans tout venv construit avec ensurepip. C'est le
# véritable sujet du test : `bin/python` est un lien symbolique et ne prouve
# rien, seul un script console porte le shebang absolu qui casse au déplacement.
CONSOLE_SCRIPT = "pip"


def _bash(body: str, cwd: Path) -> subprocess.CompletedProcess:
    """Exécute un fragment bash avec agent-venv-lib.sh sourcée, comme le script."""
    script = f'set -Eeuo pipefail\nsource {VENV_LIB!s}\n{body}\n'
    return subprocess.run(
        ["bash", "-c", script],
        cwd=cwd,
        capture_output=True,
        text=True,
    )


def _make_venv(path: Path) -> None:
    subprocess.run(
        [sys.executable, "-m", "venv", str(path)],
        check=True,
        capture_output=True,
    )


def _shebang(script: Path) -> str:
    """Première ligne du script console, ou une trace si le fichier a disparu."""
    try:
        return script.read_text(encoding="utf-8", errors="replace").splitlines()[0]
    except OSError as exc:
        return f"<illisible : {exc}>"


def _console_script_runs(venv_link: Path) -> subprocess.CompletedProcess:
    """
    Lance le script console via le chemin figé dans l'unité systemd.

    Un shebang cassé se manifeste soit par un code de retour non nul (le shell
    rend 126/127), soit par un ENOENT à l'exec — c'est ce que systemd rapporte
    en `203/EXEC`. Les deux sont rendus sous la même forme pour que l'assertion
    reste lisible.
    """
    argv = [str(venv_link / "bin" / CONSOLE_SCRIPT), "--version"]
    try:
        return subprocess.run(argv, capture_output=True, text=True)
    except OSError as exc:  # interpréteur du shebang absent : 203/EXEC
        return subprocess.CompletedProcess(argv, 203, "", f"{type(exc).__name__}: {exc}")


@pytest.fixture
def install_dir(tmp_path: Path) -> Path:
    target = tmp_path / "opt" / "llm-gateway"
    target.mkdir(parents=True)
    return target


# ── Contrôle positif ─────────────────────────────────────────────────────────
# Sans lui, un test d'exécutabilité peut devenir inerte : si `_make_venv` ne
# produisait plus de script console, les assertions ci-dessous n'auraient plus
# rien à observer et passeraient silencieusement.

def test_a_freshly_built_venv_has_a_runnable_console_script(install_dir: Path) -> None:
    venv = install_dir / "venv-agent"
    _make_venv(venv)
    assert (venv / "bin" / CONSOLE_SCRIPT).exists(), (
        f"Le venv de référence n'expose pas bin/{CONSOLE_SCRIPT} : les tests de "
        "relogeabilité qui suivent seraient inertes."
    )
    assert _console_script_runs(venv).returncode == 0


# ── La propriété cassée par COR-016 ──────────────────────────────────────────

def test_activated_venv_console_scripts_stay_executable(install_dir: Path) -> None:
    """
    Après la bascule, `venv-agent/bin/<script>` doit être RÉELLEMENT lançable.

    C'est exactement ce que systemd fait à chaque `ExecStart`. Avec l'ancienne
    stratégie (venv construit dans un staging puis déplacé), ce lancement échoue
    en `bad interpreter` / 203/EXEC.
    """
    venv_link = install_dir / "venv-agent"
    _make_venv(venv_link)  # agent installé par install-agent.sh : venv réel

    staged = _bash(
        f'staged="$(agent_venv_new_release_path {install_dir!s})"\n'
        f'printf %s "$staged"\n',
        cwd=install_dir,
    )
    assert staged.returncode == 0, staged.stderr
    staged_venv = Path(staged.stdout)

    # Le venv neuf est construit à l'emplacement rendu par la lib, et n'en bouge
    # plus : c'est la moitié « construction à l'emplacement final » du correctif.
    _make_venv(staged_venv)

    switched = _bash(
        f'agent_venv_activate {venv_link!s} {staged_venv!s}',
        cwd=install_dir,
    )
    assert switched.returncode == 0, switched.stderr

    result = _console_script_runs(venv_link)
    assert result.returncode == 0, (
        f"bin/{CONSOLE_SCRIPT} n'est plus lançable après la bascule "
        f"(rc={result.returncode}) — un venv a été déplacé.\n"
        f"  shebang : {_shebang(venv_link / 'bin' / CONSOLE_SCRIPT)}\n"
        f"  erreur  : {result.stderr.strip()}"
    )
    assert venv_link.is_symlink(), "venv-agent doit être un symlink après la bascule."
    assert os.path.realpath(venv_link) == os.path.realpath(staged_venv)


def test_legacy_install_is_migrated_in_place_and_can_roll_back(install_dir: Path) -> None:
    """
    Première mise à jour d'un agent installé par l'ancien install-agent.sh.

    Le venv réel est écarté une seule fois, puis RÉEXPOSÉ sous son chemin
    d'origine par le symlink de rollback : ses shebangs
    (`…/venv-agent/bin/python`) redeviennent valides. Un rollback qui laisserait
    le venv précédent hors de son chemin d'origine reproduirait le défaut.
    """
    venv_link = install_dir / "venv-agent"
    _make_venv(venv_link)
    legacy_shebang = (venv_link / "bin" / CONSOLE_SCRIPT).read_text().splitlines()[0]
    assert str(venv_link) in legacy_shebang, (
        "Le venv hérité doit bien porter un shebang absolu vers son propre chemin."
    )

    staged_venv = install_dir / "venv-agent-release-test"
    _make_venv(staged_venv)

    rolled_back = _bash(
        f'agent_venv_activate {venv_link!s} {staged_venv!s}\n'
        f'agent_venv_rollback {venv_link!s}\n'
        f'printf %s "$AGENT_VENV_PREVIOUS_TARGET"\n',
        cwd=install_dir,
    )
    assert rolled_back.returncode == 0, rolled_back.stderr
    previous = Path(rolled_back.stdout)

    assert previous.is_dir(), "Le venv précédent doit rester disponible sur disque."
    assert venv_link.is_symlink()
    assert os.path.realpath(venv_link) == os.path.realpath(previous)

    result = _console_script_runs(venv_link)
    assert result.returncode == 0, (
        "Après rollback, le venv précédent n'est plus lançable "
        f"(rc={result.returncode}) : {result.stderr}"
    )


def test_subsequent_update_never_moves_the_previous_release(install_dir: Path) -> None:
    """
    Deuxième mise à jour : on rebascule un symlink, on ne déplace rien.

    La release précédente doit rester intacte À SON EMPLACEMENT, sans quoi un
    rollback la ramènerait cassée.
    """
    venv_link = install_dir / "venv-agent"
    first = install_dir / "venv-agent-release-first"
    second = install_dir / "venv-agent-release-second"
    _make_venv(first)
    _make_venv(second)
    venv_link.symlink_to(first)

    switched = _bash(
        f'agent_venv_activate {venv_link!s} {second!s}\n'
        f'printf %s "$AGENT_VENV_PREVIOUS_TARGET"\n',
        cwd=install_dir,
    )
    assert switched.returncode == 0, switched.stderr
    assert os.path.realpath(Path(switched.stdout)) == os.path.realpath(first)

    assert (first / "bin" / CONSOLE_SCRIPT).exists(), (
        "La release précédente a été déplacée ou supprimée par la bascule."
    )
    assert _console_script_runs(venv_link).returncode == 0
    assert os.path.realpath(venv_link) == os.path.realpath(second)


# ── Le script déployé utilise bien cette stratégie ───────────────────────────
# Les tests ci-dessus valident la bibliothèque; celui-ci interdit qu'elle soit
# contournée dans update-agent.sh (le défaut d'origine était précisément un
# `mv` de venv dans ce fichier).

def test_update_agent_script_delegates_to_the_shared_venv_strategy() -> None:
    body = UPDATE_SCRIPT.read_text()

    # Contrôle positif : le fichier est bien celui que l'on croit lire.
    assert "agent-venv-lib.sh" in body, "update-agent.sh ne source plus la stratégie de venv."
    for expected in ("agent_venv_new_release_path", "agent_venv_activate", "agent_venv_rollback"):
        assert expected in body, f"update-agent.sh n'appelle plus {expected}."

    moves = [
        line.strip()
        for line in body.splitlines()
        if line.strip().startswith("mv ") and "VENV" in line
    ]
    assert not moves, f"Un venv est déplacé par update-agent.sh : {moves}"
