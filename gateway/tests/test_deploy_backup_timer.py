"""
OPS-008 — le timer de sauvegarde quotidienne doit être réellement ARMÉ.

Ce module est un test de NON-RÉGRESSION de déploiement, pas un test de code.

Pourquoi ce test existe
-----------------------
`install.sh` et `update.sh` faisaient `systemctl enable llm-gateway-backup.timer`
sans `--now`. Le lien dans `timers.target` était bien créé, mais l'unité restait
`inactive` : absente de `systemctl list-timers`, elle ne déclenchait AUCUNE
sauvegarde jusqu'au prochain reboot. Sur un serveur qui ne redémarre pas, cela
signifiait zéro sauvegarde périodique, indéfiniment, sans le moindre message.

Le commentaire qui justifiait l'absence de `--now` craignait qu'un premier
`start` déclenche un rattrapage `Persistent=true` immédiat. C'est faux sur une
installation neuve : sans stamp préexistant, systemd pose le stamp sans exécuter
le job. Le cas restant — une RÉinstallation avec un stamp périmé — est traité par
l'ORDRE : dans `install.sh`, l'armement suit l'initialisation de la base, donc
une sauvegarde déclenchée aussitôt trouve toujours une base initialisée.

Aucun de ces tests ne touche l'hôte : ni systemd, ni GPU, ni root.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


GATEWAY_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_DIR = GATEWAY_ROOT / "deploy"
INSTALL_SH = DEPLOY_DIR / "install.sh"
UPDATE_SH = DEPLOY_DIR / "update.sh"
BACKUP_TIMER = DEPLOY_DIR / "llm-gateway-backup.timer"

DEPLOY_SCRIPTS = (INSTALL_SH, UPDATE_SH)

TIMER_UNIT = "llm-gateway-backup.timer"

# Armement réel : le helper qui enchaîne reset-failed + `enable --now`. Cherché
# n'importe où dans la ligne : les deux scripts l'appellent en position de
# condition (`if …; then`) pour rester non fatals.
ARMS_THE_TIMER = re.compile(rf"(?<![\w-])systemctl_enable_now {re.escape(TIMER_UNIT)}(?![\w.-])")
# Le défaut d'origine : `enable` seul, qui n'arme rien.
ENABLE_WITHOUT_NOW = re.compile(rf"^systemctl\s+enable\s+{re.escape(TIMER_UNIT)}\b")


def _command_lines(script: Path) -> list[str]:
    """Lignes de commande : ni vides, ni commentaires.

    Les rappels adressés à l'opérateur (`warn "  sudo systemctl enable --now …"`)
    ne sont jamais en tête de ligne et sont donc naturellement écartés.
    """
    return [
        line.strip()
        for line in script.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


@pytest.mark.parametrize("script", DEPLOY_SCRIPTS, ids=lambda p: p.name)
def test_backup_timer_is_armed_with_now(script: Path) -> None:
    """Les deux scripts arment le timer, jamais un `enable` sec."""
    lines = _command_lines(script)

    armed = [line for line in lines if ARMS_THE_TIMER.search(line)]
    # Contrôle positif : sans site d'armement détecté, l'assertion d'absence
    # ci-dessous serait vraie pour de mauvaises raisons.
    assert armed, (
        f"{script.name} n'arme plus {TIMER_UNIT} : le motif de recherche ne "
        "correspond plus, ce test est devenu inerte."
    )

    stale = [line for line in lines if ENABLE_WITHOUT_NOW.match(line)]
    assert not stale, (
        f"{script.name} : `enable` sans `--now` laisse {TIMER_UNIT} `inactive` "
        f"jusqu'au prochain reboot (OPS-008). Lignes fautives : {stale}"
    )


def test_enable_now_helper_exists_in_both_scripts() -> None:
    """L'armement passe par un helper qui désarme aussi le start-limit (COR-017)."""
    for script in DEPLOY_SCRIPTS:
        text = script.read_text(encoding="utf-8")
        assert "systemctl_enable_now() {" in text, f"{script.name} : helper absent"
        helper = re.search(
            r"systemctl_enable_now\(\) \{\n(.*?)\n\}", text, re.DOTALL
        )
        assert helper is not None
        body = helper.group(1)
        assert "systemctl reset-failed" in body
        assert "systemctl enable --now" in body


def test_update_repairs_an_enabled_but_inactive_timer() -> None:
    """Le cas des hôtes déjà mis à jour : `enabled` mais `inactive`.

    Sans cette réparation, la branche `enabled` ne faisait rien et le défaut
    OPS-008 survivait à toutes les mises à jour suivantes.
    """
    update = UPDATE_SH.read_text(encoding="utf-8")
    assert f"systemctl is-active --quiet {TIMER_UNIT}" in update
    assert f"systemctl_start {TIMER_UNIT}" in update


def test_timer_arming_never_aborts_an_update() -> None:
    """Un timer récalcitrant ne doit pas faire rollbacker une mise à jour saine.

    Les deux armements de `update.sh` sont en position de condition (`if`/`elif`),
    donc jamais soumis au `set -e` ni au trap ERR qui déclenche le rollback
    transactionnel.
    """
    update = UPDATE_SH.read_text(encoding="utf-8")
    for command in (f"systemctl_enable_now {TIMER_UNIT}", f"systemctl_start {TIMER_UNIT}"):
        for match in re.finditer(rf"^(\s*)(\S+ )?{re.escape(command)}", update, re.MULTILINE):
            line = match.group(0).strip()
            assert line.startswith(("if ", "elif ")), (
                f"« {line} » doit rester en position de condition : sinon un échec "
                "d'armement du timer déclenche le rollback transactionnel."
            )


def test_install_arms_the_timer_after_database_initialisation() -> None:
    """L'ORDRE est la garantie : armement APRÈS l'initialisation de la base.

    `Persistent=true` fait rattraper une occurrence manquée au premier `start`
    lorsqu'un stamp périmé existe (cas d'une réinstallation). Armer après
    l'étape 9 garantit qu'une telle sauvegarde trouve une base initialisée.
    """
    lines = INSTALL_SH.read_text(encoding="utf-8").splitlines()

    db_init = next(
        (index for index, line in enumerate(lines) if "asyncio.run(database.init_db())" in line),
        None,
    )
    arming = next(
        (index for index, line in enumerate(lines) if ARMS_THE_TIMER.search(line)),
        None,
    )
    # Contrôle positif : les deux repères doivent exister.
    assert db_init is not None, "Initialisation de la DB introuvable dans install.sh"
    assert arming is not None, "Armement du timer introuvable dans install.sh"

    assert arming > db_init, (
        "install.sh arme le timer de sauvegarde AVANT d'initialiser la base : un "
        "rattrapage Persistent=true sauvegarderait une base inexistante (OPS-008)."
    )


def test_timer_unit_still_justifies_the_ordering_constraint() -> None:
    """Le raisonnement ci-dessus repose sur `Persistent=` — vérifier qu'il tient.

    Si l'unité perdait `Persistent=true`, la contrainte d'ordre deviendrait
    inutile; si elle gagnait un `OnBootSec`/`OnActiveSec`, elle deviendrait
    au contraire insuffisante. Dans les deux cas ce fichier doit être relu.
    """
    unit = BACKUP_TIMER.read_text(encoding="utf-8")
    assert re.search(r"^Persistent=true$", unit, re.MULTILINE), (
        "Persistent= a changé : revoir la justification d'ordre d'OPS-008."
    )
    assert re.search(r"^OnCalendar=\*-\*-\* 03:15:00$", unit, re.MULTILINE)
    assert not re.search(r"^On(Active|Boot|Unit\w+)Sec=", unit, re.MULTILINE), (
        "Un déclencheur relatif ferait tourner une sauvegarde peu après l'armement : "
        "la contrainte d'ordre d'OPS-008 devrait être renforcée."
    )
