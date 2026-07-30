"""
OPS-010 — rétention bornée des venvs de release de la gateway.

Ce module est un test de NON-RÉGRESSION de déploiement, pas un test de code.

Pourquoi ce test existe
-----------------------
`update.sh` construit chaque venv à son emplacement DÉFINITIF
(`venv-release-<commit>-<horodatage>`) et fait basculer le symlink
`/opt/llm-gateway/venv`, qui est le chemin figé dans l'unité systemd. C'est ce
qui rend le rollback instantané, mais le corollaire est qu'aucune release n'est
jamais écrasée : chaque mise à jour LAISSE une arborescence complète sur le
disque, plus un éventuel `venv-pre-update-*` issu de la migration depuis
l'ancien schéma. Aucune purge n'existait — un serveur mis à jour régulièrement
finissait par saturer `/opt` en silence.

La politique appliquée par `deploy/venv-retention-lib.sh` a deux bornes, et les
deux comptent autant :
  - ce qu'elle réclame : les releases excédentaires disparaissent VRAIMENT ;
  - ce qu'elle ne doit JAMAIS toucher : la release en service (la cible du
    symlink, quel que soit son âge) et celle qui sert de filet au retour arrière
    manuel documenté dans docs/deployment.md.

Le node-agent applique la même politique sur ses propres releases; sa
non-régression vit dans `node_agent/tests/test_update_agent_venv.py`.

Aucun de ces tests ne touche l'hôte : ni systemd, ni GPU, ni root. Tout se passe
dans un répertoire temporaire.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

GATEWAY_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_DIR = GATEWAY_ROOT / "deploy"
RETENTION_LIB = DEPLOY_DIR / "venv-retention-lib.sh"
UPDATE_SH = DEPLOY_DIR / "update.sh"


def _bash(body: str, cwd: Path) -> subprocess.CompletedProcess:
    """Exécute un fragment bash avec venv-retention-lib.sh sourcée, comme update.sh."""
    script = f'set -Eeuo pipefail\nsource {RETENTION_LIB!s}\n{body}\n'
    return subprocess.run(["bash", "-c", script], cwd=cwd, capture_output=True, text=True)


@pytest.fixture
def install_dir(tmp_path: Path) -> Path:
    target = tmp_path / "opt" / "llm-gateway"
    target.mkdir(parents=True)
    return target


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
        f'gateway_venv_prunable_releases "{install_dir!s}" "{link!s}"{keep_arg}',
        cwd=install_dir,
    )
    assert result.returncode == 0, result.stderr
    return [Path(line) for line in result.stdout.splitlines() if line]


def _prune(install_dir: Path, link: Path, keep: str | None = None) -> list[Path]:
    """Ce que la rétention SUPPRIME réellement."""
    keep_arg = "" if keep is None else f' "{keep}"'
    result = _bash(
        f'gateway_venv_prune_releases "{install_dir!s}" "{link!s}"{keep_arg}',
        cwd=install_dir,
    )
    assert result.returncode == 0, result.stderr
    return [Path(line) for line in result.stdout.splitlines() if line]


# ── La politique de rétention ────────────────────────────────────────────────

def test_retention_keeps_the_active_and_previous_releases(install_dir: Path) -> None:
    """Quatre releases, la plus récente en service : deux survivent, deux partent."""
    oldest = _release(install_dir, "venv-release-a1b2c3d4e5f6-20260701090000", age_seconds=300)
    older = _release(install_dir, "venv-release-b2c3d4e5f6a1-20260710090000", age_seconds=200)
    previous = _release(install_dir, "venv-release-c3d4e5f6a1b2-20260720090000", age_seconds=100)
    active = _release(install_dir, "venv-release-d4e5f6a1b2c3-20260730090000", age_seconds=0)

    venv_link = install_dir / "venv"
    venv_link.symlink_to(active)

    assert _prunable(install_dir, venv_link) == [older, oldest]
    assert _prune(install_dir, venv_link) == [older, oldest]

    assert active.is_dir(), "La release en service a été purgée."
    assert previous.is_dir(), "La release précédente doit rester : elle sert au retour arrière."
    assert not older.exists()
    assert not oldest.exists()


def test_retention_orders_by_date_not_by_name(install_dir: Path) -> None:
    """
    Le nom d'une release de gateway commence par un HASH DE COMMIT.

    Un tri lexicographique sur le nom purgerait donc au hasard. Ici, la release
    la plus récente porte le nom qui trie en dernier : seul un ordre par date de
    modification conserve les bonnes.
    """
    doomed = _release(install_dir, "venv-release-ffffffffffff-20260701090000", age_seconds=300)
    previous = _release(install_dir, "venv-release-000000000000-20260720090000", age_seconds=100)
    active = _release(install_dir, "venv-release-111111111111-20260730090000", age_seconds=0)

    venv_link = install_dir / "venv"
    venv_link.symlink_to(active)

    assert _prune(install_dir, venv_link) == [doomed]
    assert previous.is_dir() and active.is_dir()


def test_retention_never_removes_the_active_release_even_when_it_is_the_oldest(
    install_dir: Path,
) -> None:
    """
    Le symlink fait autorité, pas la date.

    Après un retour arrière manuel vers une vieille release, la release EN
    SERVICE est la plus ancienne du répertoire. Une purge qui ne regarderait que
    les dates emporterait le venv sous les pieds de la gateway.
    """
    active = _release(install_dir, "venv-release-a1b2c3d4e5f6-20260601090000", age_seconds=400)
    _release(install_dir, "venv-release-b2c3d4e5f6a1-20260710090000", age_seconds=200)
    doomed = _release(install_dir, "venv-release-c3d4e5f6a1b2-20260715090000", age_seconds=150)
    newest = _release(install_dir, "venv-release-d4e5f6a1b2c3-20260730090000", age_seconds=0)

    venv_link = install_dir / "venv"
    venv_link.symlink_to(active)

    pruned = _prune(install_dir, venv_link)

    assert active.is_dir(), "La cible du symlink a été purgée : la gateway n'a plus de venv."
    assert active not in pruned
    assert newest.is_dir(), "La release conservée en plus de l'active doit être la plus récente."
    assert doomed in pruned and not doomed.exists()


def test_retention_reclaims_the_legacy_pre_update_venv(install_dir: Path) -> None:
    """
    `venv-pre-update-*` est une release comme une autre.

    C'est le venv écarté lors de la migration vers le schéma symlink : une fois
    sorti du quota il n'a plus aucune raison d'occuper le disque indéfiniment.
    """
    legacy = _release(install_dir, "venv-pre-update-20260601-090000", age_seconds=400)
    previous = _release(install_dir, "venv-release-c3d4e5f6a1b2-20260720090000", age_seconds=100)
    active = _release(install_dir, "venv-release-d4e5f6a1b2c3-20260730090000", age_seconds=0)

    venv_link = install_dir / "venv"
    venv_link.symlink_to(active)

    assert _prune(install_dir, venv_link) == [legacy]
    assert not legacy.exists()
    assert previous.is_dir() and active.is_dir()


def test_retention_keeps_the_legacy_venv_while_it_is_the_rollback_target(
    install_dir: Path,
) -> None:
    """
    Juste après la migration, `venv-pre-update-*` EST la release de retour arrière.

    Le purger là serait exactement l'inverse du besoin : il n'existe alors aucune
    autre version vers laquelle rebasculer.
    """
    legacy = _release(install_dir, "venv-pre-update-20260601-090000", age_seconds=400)
    active = _release(install_dir, "venv-release-d4e5f6a1b2c3-20260730090000", age_seconds=0)

    venv_link = install_dir / "venv"
    venv_link.symlink_to(active)

    assert _prune(install_dir, venv_link) == []
    assert legacy.is_dir()


def test_retention_only_ever_touches_release_directories(install_dir: Path) -> None:
    """
    Rien d'autre que les releases ne doit entrer dans le champ de la purge.

    `install_dir` est `/opt/llm-gateway` : le code déployé, `deploy/` et le
    symlink lui-même y cohabitent avec les venvs.
    """
    _release(install_dir, "venv-release-a1b2c3d4e5f6-20260601090000", age_seconds=400)
    _release(install_dir, "venv-release-b2c3d4e5f6a1-20260710090000", age_seconds=200)
    active = _release(install_dir, "venv-release-d4e5f6a1b2c3-20260730090000", age_seconds=0)

    bystanders = [
        install_dir / "cluster",
        install_dir / "static",
        install_dir / "deploy",
        install_dir / "venv-releases-notes",  # préfixe voisin, pas une release
    ]
    for path in bystanders:
        path.mkdir()
        (path / "keep-me").write_text("", encoding="utf-8")

    venv_link = install_dir / "venv"
    venv_link.symlink_to(active)

    _prune(install_dir, venv_link)

    for path in bystanders:
        assert (path / "keep-me").exists(), (
            f"La purge a touché {path.name}, qui n'est pas une release."
        )
    assert venv_link.is_symlink(), "Le symlink venv a été emporté par la purge."


def test_retention_leaves_a_never_migrated_install_untouched(install_dir: Path) -> None:
    """
    Installation jamais mise à jour : `venv` est un vrai répertoire, seul.

    Il n'y a alors aucune release à purger, et surtout rien à confondre avec le
    venv en service.
    """
    real_venv = install_dir / "venv"
    real_venv.mkdir()
    (real_venv / "pyvenv.cfg").write_text("home = /usr\n", encoding="utf-8")

    assert _prune(install_dir, real_venv) == []
    assert (real_venv / "pyvenv.cfg").exists()


def test_retention_keep_is_configurable_and_never_falls_below_the_active_release(
    install_dir: Path,
) -> None:
    """Le quota est réglable (EVA_GATEWAY_VENV_KEEP), mais 0 ne peut pas exister."""
    releases = [
        _release(install_dir, f"venv-release-{index:012d}-2026070{index}090000", age_seconds=age)
        for index, age in enumerate((400, 300, 200, 100, 0))
    ]
    venv_link = install_dir / "venv"
    venv_link.symlink_to(releases[-1])

    assert _prunable(install_dir, venv_link, keep="4") == [releases[0]]

    # Une valeur inutilisable retombe sur la valeur par défaut plutôt que de
    # purger au hasard; et dans tous les cas la release active survit.
    for absurd in ("0", "-1", "", "beaucoup"):
        assert venv_link.resolve() not in _prunable(install_dir, venv_link, keep=absurd), (
            f"keep={absurd!r} désigne la release en service."
        )


# ── Le script déployé applique bien cette politique ──────────────────────────

def test_update_script_applies_a_bounded_retention() -> None:
    """La rétention ne doit pas rester une bibliothèque inutilisée."""
    body = UPDATE_SH.read_text(encoding="utf-8")

    assert "venv-retention-lib.sh" in body, "update.sh ne source plus la politique de rétention."
    assert "gateway_venv_prune_releases" in body, (
        "update.sh ne purge aucun venv de release : chaque mise à jour laisse une "
        "arborescence complète sur le disque."
    )
    assert "gateway_venv_prunable_releases" in body, "update.sh n'annonce pas la purge en --dry-run."


def test_update_script_prunes_only_after_the_release_is_validated() -> None:
    """
    La purge suit la recette du premier token, elle ne la précède pas.

    Purger plus tôt supprimerait la release vers laquelle `rollback_venv`
    rebascule — le rollback rendrait alors la gateway définitivement inopérante.
    """
    lines = UPDATE_SH.read_text(encoding="utf-8").splitlines()

    prune_line = next(
        index for index, line in enumerate(lines)
        if "gateway_venv_prune_releases" in line and not line.strip().startswith("#")
    )
    smoke_line = max(
        index for index, line in enumerate(lines)
        if "run_smoke_test" in line and not line.strip().startswith("#")
    )
    rollback_lines = [
        index for index, line in enumerate(lines)
        if "rollback_deployed_release " in line and not line.strip().startswith("#")
    ]

    assert prune_line > smoke_line, (
        "La purge précède la recette du premier token : elle peut supprimer la "
        "release vers laquelle un rollback rebascule."
    )
    assert prune_line > max(rollback_lines), (
        "La purge précède un chemin de rollback : celui-ci rebasculerait vers une "
        "release déjà supprimée."
    )
