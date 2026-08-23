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

La seconde moitié du module couvre le COROLLAIRE de cette stratégie (OPS-010) :
puisque plus aucun venv n'est écrasé, chaque mise à jour laisse une release
complète (~200 Mo) sur le disque du nœud. La rétention doit être bornée, et
surtout ne jamais emporter la release ACTIVE ni celle vers laquelle un retour
arrière manuel rebasculerait.

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


# ── Rétention des releases (OPS-010) ─────────────────────────────────────────
# Sans purge, chaque mise à jour laisse ~200 Mo de plus sur le nœud, sans le
# moindre message, jusqu'à saturation de /opt. Les tests ci-dessous verrouillent
# les deux bornes de la politique : ce qu'elle réclame VRAIMENT (les releases
# excédentaires disparaissent du disque) et ce qu'elle ne doit JAMAIS toucher
# (la release en service, et celle qui sert de filet au retour arrière manuel).

def _release(install_dir: Path, name: str, age_seconds: int) -> Path:
    """Une release factice datée, pour rendre l'ordre de purge déterministe.

    L'âge est imposé explicitement : deux répertoires créés dans la même seconde
    porteraient la même mtime, et `stat` ne descend pas sous la seconde.
    """
    path = install_dir / name
    path.mkdir()
    (path / "pyvenv.cfg").write_text("home = /usr\n", encoding="utf-8")
    stamp = 1_700_000_000 - age_seconds
    os.utime(path, (stamp, stamp))
    return path


def _prunable(install_dir: Path, link: Path, keep: str | None = None) -> list[Path]:
    """Ce que la rétention DÉSIGNE, sans rien supprimer (ce qu'annonce --dry-run)."""
    keep_arg = "" if keep is None else f' "{keep}"'
    result = _bash(
        f'agent_venv_prunable_releases "{install_dir!s}" "{link!s}"{keep_arg}',
        cwd=install_dir,
    )
    assert result.returncode == 0, result.stderr
    return [Path(line) for line in result.stdout.splitlines() if line]


def _prune(install_dir: Path, link: Path, keep: str | None = None) -> list[Path]:
    """Ce que la rétention SUPPRIME réellement."""
    keep_arg = "" if keep is None else f' "{keep}"'
    result = _bash(
        f'agent_venv_prune_releases "{install_dir!s}" "{link!s}"{keep_arg}',
        cwd=install_dir,
    )
    assert result.returncode == 0, result.stderr
    return [Path(line) for line in result.stdout.splitlines() if line]


def test_retention_keeps_the_active_and_previous_releases(install_dir: Path) -> None:
    """Quatre releases, la plus récente en service : deux survivent, deux partent."""
    oldest = _release(install_dir, "venv-agent-release-20260701-090000-11", age_seconds=300)
    older = _release(install_dir, "venv-agent-release-20260710-090000-22", age_seconds=200)
    previous = _release(install_dir, "venv-agent-release-20260720-090000-33", age_seconds=100)
    active = _release(install_dir, "venv-agent-release-20260730-090000-44", age_seconds=0)

    venv_link = install_dir / "venv-agent"
    venv_link.symlink_to(active)

    assert _prunable(install_dir, venv_link) == [older, oldest]
    assert _prune(install_dir, venv_link) == [older, oldest]

    assert active.is_dir(), "La release en service a été purgée."
    assert previous.is_dir(), "La release précédente doit rester : elle sert au retour arrière."
    assert not older.exists()
    assert not oldest.exists()


def test_retention_never_removes_the_active_release_even_when_it_is_the_oldest(
    install_dir: Path,
) -> None:
    """
    Le symlink fait autorité, pas la date.

    Après un retour arrière manuel vers une vieille release, la release EN
    SERVICE est la plus ancienne du répertoire. Une purge qui ne regarderait que
    les dates emporterait le venv sous les pieds de l'agent.
    """
    active = _release(install_dir, "venv-agent-release-20260601-090000-11", age_seconds=400)
    _release(install_dir, "venv-agent-release-20260710-090000-22", age_seconds=200)
    doomed = _release(install_dir, "venv-agent-release-20260715-090000-33", age_seconds=150)
    newest = _release(install_dir, "venv-agent-release-20260730-090000-44", age_seconds=0)

    venv_link = install_dir / "venv-agent"
    venv_link.symlink_to(active)

    pruned = _prune(install_dir, venv_link)

    assert active.is_dir(), "La cible du symlink a été purgée : l'agent n'a plus de venv."
    assert active not in pruned
    assert newest.is_dir(), "La release conservée en plus de l'active doit être la plus récente."
    assert doomed in pruned and not doomed.exists()


def test_retention_reclaims_the_legacy_pre_update_venv(install_dir: Path) -> None:
    """
    `venv-agent-pre-update-*` est une release comme une autre.

    C'est le venv écarté lors de la migration depuis l'ancien schéma : une fois
    sorti du quota il n'a plus aucune raison d'occuper 200 Mo indéfiniment.
    """
    legacy = _release(install_dir, "venv-agent-pre-update-20260601-090000", age_seconds=400)
    previous = _release(install_dir, "venv-agent-release-20260720-090000-33", age_seconds=100)
    active = _release(install_dir, "venv-agent-release-20260730-090000-44", age_seconds=0)

    venv_link = install_dir / "venv-agent"
    venv_link.symlink_to(active)

    assert _prune(install_dir, venv_link) == [legacy]
    assert not legacy.exists()
    assert previous.is_dir() and active.is_dir()

    # Mais tant qu'il tient dans le quota, il est conservé : juste après la
    # migration, c'est LUI la release de retour arrière.
    fresh = install_dir / "opt2"
    fresh.mkdir()
    legacy_kept = _release(fresh, "venv-agent-pre-update-20260601-090000", age_seconds=400)
    active2 = _release(fresh, "venv-agent-release-20260730-090000-44", age_seconds=0)
    link2 = fresh / "venv-agent"
    link2.symlink_to(active2)

    assert _prune(fresh, link2) == []
    assert legacy_kept.is_dir()


def test_retention_only_ever_touches_release_directories(install_dir: Path) -> None:
    """
    Rien d'autre que les releases ne doit entrer dans le champ de la purge.

    `install_dir` est `/opt/llm-gateway` : le code déployé, la sauvegarde de
    rollback et le symlink lui-même y cohabitent avec les venvs.
    """
    _release(install_dir, "venv-agent-release-20260601-090000-11", age_seconds=400)
    _release(install_dir, "venv-agent-release-20260710-090000-22", age_seconds=200)
    active = _release(install_dir, "venv-agent-release-20260730-090000-44", age_seconds=0)

    bystanders = [
        install_dir / "node_agent",
        install_dir / "gateway",
        install_dir / ".agent-rollback",
        install_dir / "venv-agent-releases-notes",  # préfixe voisin, pas une release
    ]
    for path in bystanders:
        path.mkdir()
        (path / "keep-me").write_text("", encoding="utf-8")

    venv_link = install_dir / "venv-agent"
    venv_link.symlink_to(active)

    _prune(install_dir, venv_link)

    for path in bystanders:
        assert (path / "keep-me").exists(), f"La purge a touché {path.name}, qui n'est pas une release."
    assert venv_link.is_symlink(), "Le symlink venv-agent a été emporté par la purge."


def test_retention_keep_is_configurable_and_never_falls_below_the_active_release(
    install_dir: Path,
) -> None:
    """Le quota est réglable (EVA_AGENT_VENV_KEEP), mais 0 ne peut pas exister."""
    releases = [
        _release(install_dir, f"venv-agent-release-2026070{index}-090000-{index}", age_seconds=age)
        for index, age in enumerate((400, 300, 200, 100, 0))
    ]
    venv_link = install_dir / "venv-agent"
    venv_link.symlink_to(releases[-1])

    assert _prunable(install_dir, venv_link, keep="4") == [releases[0]]

    # Une valeur inutilisable retombe sur la valeur par défaut plutôt que de
    # purger au hasard; et dans tous les cas la release active survit.
    for absurd in ("0", "-1", "", "beaucoup"):
        assert venv_link.resolve() not in _prunable(install_dir, venv_link, keep=absurd), (
            f"keep={absurd!r} désigne la release en service."
        )


def test_retention_leaves_the_activated_venv_runnable(install_dir: Path) -> None:
    """
    Bout à bout, avec de VRAIS venvs : bascule, puis purge.

    Le venv en service doit rester lançable après la purge — c'est ce que fait
    systemd au premier `ExecStart` qui suit la mise à jour. Une purge qui
    emporterait la cible du symlink se verrait ici, et nulle part ailleurs.
    """
    venv_link = install_dir / "venv-agent"
    previous = install_dir / "venv-agent-release-20260720-090000-33"
    _make_venv(previous)
    os.utime(previous, (1_699_999_900, 1_699_999_900))
    stale = _release(install_dir, "venv-agent-release-20260601-090000-11", age_seconds=400)
    venv_link.symlink_to(previous)

    staged = _bash(
        f'staged="$(agent_venv_new_release_path "{install_dir!s}")"\nprintf %s "$staged"\n',
        cwd=install_dir,
    )
    assert staged.returncode == 0, staged.stderr
    staged_venv = Path(staged.stdout)
    _make_venv(staged_venv)

    switched = _bash(
        f'agent_venv_activate "{venv_link!s}" "{staged_venv!s}"\n'
        f'agent_venv_prune_releases "{install_dir!s}" "{venv_link!s}" 2\n',
        cwd=install_dir,
    )
    assert switched.returncode == 0, switched.stderr

    result = _console_script_runs(venv_link)
    assert result.returncode == 0, (
        f"bin/{CONSOLE_SCRIPT} n'est plus lançable après la purge (rc={result.returncode}).\n"
        f"  shebang : {_shebang(venv_link / 'bin' / CONSOLE_SCRIPT)}\n"
        f"  erreur  : {result.stderr.strip()}"
    )
    assert (previous / "bin" / CONSOLE_SCRIPT).exists(), (
        "La release précédente a été purgée : plus aucun retour arrière possible."
    )
    assert not stale.exists(), "La release excédentaire n'a pas été purgée."


def test_update_agent_script_applies_a_bounded_retention() -> None:
    """La stratégie de rétention ne doit pas rester une bibliothèque inutilisée."""
    body = UPDATE_SCRIPT.read_text()

    assert "agent_venv_prune_releases" in body, (
        "update-agent.sh ne purge aucun venv de release : chaque mise à jour "
        "laisse ~200 Mo sur le nœud."
    )
    assert "agent_venv_prunable_releases" in body, (
        "update-agent.sh n'annonce pas la purge en --dry-run."
    )
    # La purge n'a de sens qu'une fois la mise à jour validée : avant, la release
    # précédente est encore ce vers quoi le rollback rebascule.
    lines = body.splitlines()
    prune_line = next(
        index for index, line in enumerate(lines)
        if "agent_venv_prune_releases" in line and not line.strip().startswith("#")
    )
    # `ROLLBACK_READY=false` apparaît deux fois : l'initialisation en tête de
    # script, puis le désarmement qui acte le succès. C'est le second qui borne.
    success_line = max(
        index for index, line in enumerate(lines)
        if line.strip() == "ROLLBACK_READY=false"
    )
    assert prune_line > success_line, (
        "La purge précède la validation de la mise à jour : elle peut supprimer "
        "la release vers laquelle le rollback rebascule."
    )
