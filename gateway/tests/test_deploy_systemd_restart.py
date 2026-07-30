"""
COR-017 — un redémarrage de déploiement ne doit jamais buter sur le start-limit.

Ce module est un test de NON-RÉGRESSION de déploiement, pas un test de code.

Pourquoi ce test existe
-----------------------
Lors du premier déploiement sur deux VMs, la bascule cluster a échoué et
`update.sh` a correctement restauré `CLUSTER_MODE=local` et l'unité locale —
mais son `systemctl start` s'est heurté au start-limit systemd (« Start request
repeated too quickly »). Le service est resté `failed`, la gateway est restée
indisponible, et le rollback ne l'a signalé que par un `[WARN]` : les scripts
faisaient `systemctl start llm-gateway || true`. Rétabli à la main par
`systemctl reset-failed`.

Deux invariants en découlent :

1. tout démarrage d'unité est précédé d'un `systemctl reset-failed` (no-op sur
   une unité saine, donc sans risque) ;
2. sur un chemin de ROLLBACK, un démarrage refusé est une INDISPONIBILITÉ —
   message sans ambiguïté, commande de rétablissement, code de sortie non nul —
   et non un avertissement.

Le premier invariant est vérifié structurellement, sur les deux scripts, de
façon à rester vrai pour les démarrages qui seront ajoutés plus tard. Le second
est vérifié en EXÉCUTANT la vraie fonction `rollback_deployed_release`, extraite
verbatim de `update.sh`, avec un `systemctl` factice dans le PATH.

Aucun de ces tests ne touche l'hôte : ni systemd, ni GPU, ni root.
"""
from __future__ import annotations

import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest


GATEWAY_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_DIR = GATEWAY_ROOT / "deploy"
INSTALL_SH = DEPLOY_DIR / "install.sh"
UPDATE_SH = DEPLOY_DIR / "update.sh"

DEPLOY_SCRIPTS = (INSTALL_SH, UPDATE_SH)

# Une commande qui met une unité EN MARCHE. `enable` seul ne démarre rien et
# n'est donc pas concerné; `enable --now` si (OPS-008).
STARTS_A_UNIT = re.compile(r"^systemctl\s+(start|restart|enable\s+--now)\b")
RESETS_FAILED = re.compile(r"^systemctl\s+reset-failed\b")


def _command_lines(script: Path) -> list[tuple[int, str]]:
    """Lignes de commande du script : ni vides, ni commentaires.

    Les invocations réelles de `systemctl` sont toutes en début de commande dans
    ces scripts; les occurrences citées à l'opérateur vivent dans un `echo`/`warn`
    et ne sont donc jamais en tête de ligne.
    """
    lines: list[tuple[int, str]] = []
    for number, raw in enumerate(script.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append((number, stripped))
    return lines


def _extract_function(script: Path, name: str) -> str:
    """Extrait verbatim une fonction shell de premier niveau (`nom() {` … `}`)."""
    text = script.read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(name)}\(\) \{{\n(.*?)^\}}\n", text, re.MULTILINE | re.DOTALL)
    assert match is not None, f"Fonction {name}() introuvable dans {script.name}"
    return match.group(0)


# ── Invariant 1 : aucun démarrage sans reset-failed ───────────────────────────

@pytest.mark.parametrize("script", DEPLOY_SCRIPTS, ids=lambda p: p.name)
def test_every_unit_start_is_preceded_by_reset_failed(script: Path) -> None:
    """Tout `systemctl start|restart|enable --now` suit un `systemctl reset-failed`.

    Formulé sur la commande précédente plutôt que sur l'usage d'un helper : la
    règle reste vraie pour un futur démarrage écrit en clair, tant que la remise
    à zéro du compteur d'échecs le précède.
    """
    lines = _command_lines(script)
    starts = [
        (number, command)
        for index, (number, command) in enumerate(lines)
        if STARTS_A_UNIT.match(command)
    ]

    # Contrôle positif : sans démarrage détecté, l'assertion ci-dessous serait
    # vide et le test deviendrait inerte sans jamais échouer.
    assert starts, (
        f"Aucun démarrage d'unité trouvé dans {script.name} : le motif de "
        "recherche ne correspond plus, ce test est devenu inerte."
    )

    by_number = {number: index for index, (number, _) in enumerate(lines)}
    for number, command in starts:
        previous_index = by_number[number] - 1
        assert previous_index >= 0, f"{script.name}:{number} — démarrage en première commande"
        previous_number, previous = lines[previous_index]
        assert RESETS_FAILED.match(previous), (
            f"{script.name}:{number} — « {command} » n'est pas précédé d'un "
            f"`systemctl reset-failed` (COR-017). Commande précédente "
            f"(ligne {previous_number}) : « {previous} »."
        )


def test_deploy_scripts_route_starts_through_the_helpers() -> None:
    """Les sites de démarrage connus passent bien par les helpers.

    Contrôle positif de couverture : le test ci-dessus vérifie une règle, celui-ci
    vérifie que les appels réels existent toujours et n'ont pas été supprimés.
    """
    update = UPDATE_SH.read_text(encoding="utf-8")
    install = INSTALL_SH.read_text(encoding="utf-8")

    # `(?![\\w.-])` : ne pas confondre llm-gateway avec llm-gateway-backup.timer.
    gateway_starts = re.findall(r"systemctl_start llm-gateway(?![\w.-])", update)
    assert len(gateway_starts) == 4, (
        "update.sh doit démarrer llm-gateway par systemctl_start sur ses 4 sites : "
        "rollback transactionnel, rollback de mode, rollback de snapshot, bascule nominale."
    )
    assert "systemctl_restart systemd-journald" in update
    assert "systemctl_restart systemd-journald" in install
    # install.sh ne démarre pas la gateway (l'opérateur le fait), mais une
    # réinstallation doit désarmer un start-limit hérité.
    assert "systemctl reset-failed llm-gateway.service" in install


# ── Invariant 2 : un rollback qui ne redémarre pas est une indisponibilité ────

def test_rollback_paths_never_silence_a_failed_start() -> None:
    """Aucun `|| true` ni `|| warn` sur un démarrage de chemin de rollback."""
    for name in ("rollback_failed_transaction", "rollback_deployed_release"):
        body = _extract_function(UPDATE_SH, name)
        starts = [line.strip() for line in body.splitlines() if "systemctl_start" in line]
        assert starts, f"{name}() ne démarre plus le service — test inerte."
        for line in starts:
            assert "|| true" not in line, f"{name}() : démarrage silencié par `|| true` ({line})"
            assert "|| warn" not in line, f"{name}() : démarrage silencié par `|| warn` ({line})"
        assert "service_down" in body, (
            f"{name}() doit signaler une indisponibilité si le service ne redémarre pas."
        )


def test_nominal_start_stays_non_fatal() -> None:
    """Le chemin nominal garde son échec non fatal : la readiness enchaîne le rollback."""
    update = UPDATE_SH.read_text(encoding="utf-8")
    assert re.search(
        r"^systemctl_start llm-gateway \|\| warn ", update, re.MULTILINE
    ), "La bascule nominale ne doit pas devenir fatale : elle court-circuiterait le rollback."


# ── Exécution réelle avec un systemctl factice ────────────────────────────────

FAKE_SYSTEMCTL = """#!/usr/bin/env bash
echo "$*" >> "$SYSTEMCTL_LOG"
for failing in $FAKE_SYSTEMCTL_FAIL; do
    if [[ "$1" == "$failing" ]]; then
        echo "Job for $2 failed: Start request repeated too quickly." >&2
        exit 1
    fi
done
exit 0
"""

FAKE_SLEEP = "#!/usr/bin/env bash\nexit 0\n"

FAKE_CURL = """#!/usr/bin/env bash
[[ "${FAKE_READY:-0}" == "1" ]] || exit 7
exit 0
"""


@pytest.fixture()
def shell_env(tmp_path: Path) -> dict[str, str]:
    """PATH avec `systemctl`, `curl` et `sleep` factices."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name, content in (
        ("systemctl", FAKE_SYSTEMCTL),
        ("sleep", FAKE_SLEEP),
        ("curl", FAKE_CURL),
    ):
        path = fake_bin / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env["SYSTEMCTL_LOG"] = str(tmp_path / "systemctl.log")
    env["FAKE_SYSTEMCTL_FAIL"] = ""
    env["FAKE_READY"] = "0"
    return env


def _run_shell(body: str, env: dict[str, str], tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Exécute un fragment bash après les fonctions extraites de update.sh."""
    preamble = textwrap.dedent(
        """
        set -Eeuo pipefail
        RED=''; GREEN=''; YELLOW=''; CYAN=''; NC=''
        info()    { echo "[INFO]  $*"; }
        warn()    { echo "[WARN]  $*"; }
        error()   { echo "[ERROR] $*" >&2; exit 1; }
        section() { echo "> $*"; }
        """
    )
    functions = "\n".join(
        _extract_function(UPDATE_SH, name)
        for name in ("systemctl_start", "systemctl_restart", "service_down")
    )
    script = tmp_path / "harness.sh"
    script.write_text(preamble + functions + "\n" + textwrap.dedent(body), encoding="utf-8")
    return subprocess.run(
        ["bash", str(script)], env=env, capture_output=True, text=True, timeout=60
    )


def test_systemctl_start_resets_failed_state_first(shell_env, tmp_path: Path) -> None:
    """L'ordre réel des appels : reset-failed PUIS start."""
    result = _run_shell("systemctl_start llm-gateway\n", shell_env, tmp_path)
    assert result.returncode == 0, result.stderr

    calls = Path(shell_env["SYSTEMCTL_LOG"]).read_text(encoding="utf-8").splitlines()
    assert calls == ["reset-failed llm-gateway", "start llm-gateway"]


def test_systemctl_start_propagates_a_refused_start(shell_env, tmp_path: Path) -> None:
    """Un démarrage refusé remonte bien un code non nul à l'appelant."""
    shell_env["FAKE_SYSTEMCTL_FAIL"] = "start"
    result = _run_shell(
        "if systemctl_start llm-gateway; then echo VERDICT=ok; else echo VERDICT=ko; fi\n",
        shell_env,
        tmp_path,
    )
    assert "VERDICT=ko" in result.stdout
    calls = Path(shell_env["SYSTEMCTL_LOG"]).read_text(encoding="utf-8").splitlines()
    assert calls == ["reset-failed llm-gateway", "start llm-gateway"]


def test_service_down_names_the_outage_and_the_recovery_command(shell_env, tmp_path: Path) -> None:
    """L'indisponibilité est explicite et donne la commande de rétablissement."""
    result = _run_shell('service_down "Rollback terminé."\n', shell_env, tmp_path)
    assert result.returncode != 0
    assert "INDISPONIBILITÉ" in result.stderr
    assert "À TERRE" in result.stderr
    assert "sudo systemctl reset-failed llm-gateway" in result.stderr
    assert "sudo systemctl start llm-gateway" in result.stderr


# ── Exécution de la vraie fonction de rollback ────────────────────────────────

ROLLBACK_HARNESS = """
    # Dépendances de rollback_deployed_release() hors périmètre du test.
    rollback_venv()                 { echo "stub:rollback_venv"; }
    restore_code_snapshot()         { echo "stub:restore_code_snapshot $1"; }
    restore_previous_service_unit() { echo "stub:restore_unit $1"; }
    deploy_set_env_value()          { echo "stub:set_env $2=$3"; }

    PREVIOUS_MODE="local"
    EFFECTIVE_MODE="${HARNESS_EFFECTIVE_MODE:-local}"
    CONFIG_FILE="/dev/null"
    CODE_SNAPSHOT="/tmp/code-pre-update-test"
    AFTER="0123456789abcdef"
    BACKUP_FILE=""

    rollback_deployed_release "Le service ne répond pas."
"""


def _run_rollback(shell_env, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    functions = _extract_function(UPDATE_SH, "rollback_deployed_release")
    return _run_shell(functions + "\n" + ROLLBACK_HARNESS, shell_env, tmp_path)


def test_rollback_reports_an_outage_when_the_service_refuses_to_start(
    shell_env, tmp_path: Path
) -> None:
    """Le défaut d'origine : rollback restauré, service à terre, simple [WARN]."""
    shell_env["FAKE_SYSTEMCTL_FAIL"] = "start"
    result = _run_rollback(shell_env, tmp_path)

    assert result.returncode != 0
    assert "INDISPONIBILITÉ" in result.stderr
    assert "sudo systemctl reset-failed llm-gateway" in result.stderr
    # Le snapshot a bien été restauré AVANT que l'échec ne soit signalé.
    assert "stub:restore_code_snapshot" in result.stdout
    assert "stub:restore_unit" in result.stdout
    # Et surtout : pas de verdict rassurant.
    assert "Rollback réussi" not in result.stdout

    calls = Path(shell_env["SYSTEMCTL_LOG"]).read_text(encoding="utf-8").splitlines()
    assert "reset-failed llm-gateway" in calls
    assert calls.index("reset-failed llm-gateway") < calls.index("start llm-gateway")


def test_rollback_succeeds_when_the_restored_service_becomes_ready(
    shell_env, tmp_path: Path
) -> None:
    """Contrôle positif : le chemin nominal du rollback reste fonctionnel."""
    shell_env["FAKE_READY"] = "1"
    result = _run_rollback(shell_env, tmp_path)

    assert result.returncode == 1, "un rollback réussi sort quand même en erreur"
    assert "Rollback réussi" in result.stdout
    assert "INDISPONIBILITÉ" not in result.stderr


def test_rollback_reports_a_failure_when_readiness_never_comes(shell_env, tmp_path: Path) -> None:
    """Service démarré mais jamais ready : échec de rollback, pas de succès annoncé."""
    shell_env["FAKE_READY"] = "0"
    result = _run_rollback(shell_env, tmp_path)

    assert result.returncode != 0
    assert "Rollback ÉCHOUÉ" in result.stderr
    assert "Rollback réussi" not in result.stdout


def test_mode_rollback_reports_an_outage_when_the_service_refuses_to_start(
    shell_env, tmp_path: Path
) -> None:
    """Le scénario réellement observé : rollback de bascule cluster → local."""
    shell_env["HARNESS_EFFECTIVE_MODE"] = "cluster"
    shell_env["FAKE_SYSTEMCTL_FAIL"] = "start"
    result = _run_rollback(shell_env, tmp_path)

    assert result.returncode != 0
    assert "INDISPONIBILITÉ" in result.stderr
    assert "stub:set_env CLUSTER_MODE=local" in result.stdout, (
        "le mode précédent doit être restauré AVANT que l'échec soit signalé"
    )
