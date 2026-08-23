"""
OPS-012 — échappatoire explicite au préflight GPU d'`install.sh`.

Pourquoi ce test existe
-----------------------
Le préflight `command -v nvidia-smi` a bloqué les DEUX déploiements réels sur
banc CPU (§0.10 puis §0.13), et les deux ont dû relâcher la ligne du script pour
installer. Un garde-fou que tout le monde contourne ne protège plus personne : il
apprend seulement à passer outre, et le contournement ne laisse aucune trace —
ni dans l'environnement, ni dans le diagnostic.

Ce que couvrent ces tests, dans les DEUX branches :

1. le refus, conservé, dit désormais quoi faire ;
2. `--allow-no-gpu` laisse passer ET inscrit le choix dans l'environnement ;
3. `doctor` distingue « pas de GPU, assumé » de « GPU attendu mais absent » —
   codes et statuts différents, donc verdicts différents ;
4. une renonciation périmée (GPU présent) est signalée, pas tue.

La décision bash est exercée POUR DE VRAI : `deploy_gpu_verdict` est sourcée et
appelée dans un sous-shell, avec une sonde injectée. Aucun test ne touche
l'hôte : ni systemd, ni GPU, ni root.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import doctor
from doctor import GPU_WAIVER_ENV_KEY, GpuInfo, check_gpu_inventory, gpu_waiver_declared


REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_DIR = REPO_ROOT / "gateway" / "deploy"
GPU_LIB = DEPLOY_DIR / "gpu-preflight-lib.sh"
INSTALL_SH = DEPLOY_DIR / "install.sh"
UPDATE_SH = DEPLOY_DIR / "update.sh"

# Nom de commande garanti absent du PATH : sert de « pas de GPU » déterministe.
ABSENT_PROBE = "evaruntime-nvidia-smi-absent"
# Commande garantie présente : sert de « GPU détecté » déterministe.
PRESENT_PROBE = "sh"


class _Config:
    """Configuration minimale consommée par `check_gpu_inventory`."""

    def __init__(self, cluster_mode: str = "local") -> None:
        self.cluster_mode = cluster_mode


def _gpu(index: int = 0) -> GpuInfo:
    return GpuInfo(
        index=index, uuid=f"GPU-{index}", name="NVIDIA L40S",
        memory_total_mib=46068.0, driver_version="535.1", compute_capability="8.9",
    )


def run_verdict(mode: str, allow_no_gpu: str, probe: str) -> subprocess.CompletedProcess:
    """Source la bibliothèque et appelle la vraie fonction bash."""
    return subprocess.run(
        [
            "bash", "-c",
            f'source "{GPU_LIB}"; deploy_gpu_verdict "$1" "$2" "$3"',
            "bash", mode, allow_no_gpu, probe,
        ],
        capture_output=True, text=True,
    )


# ── 1. Branche « sans l'option » : le refus est conservé et dit quoi faire ─────

def test_local_without_gpu_and_without_option_is_refused() -> None:
    result = run_verdict("local", "false", ABSENT_PROBE)
    assert result.returncode == 1, (
        "Le refus historique doit être CONSERVÉ : un hôte local sans GPU et sans "
        f"--allow-no-gpu ne s'installe pas. Sortie : {result.stdout!r}"
    )


def test_the_refusal_says_what_to_do() -> None:
    """
    Le cœur d'OPS-012 côté refus : un garde-fou muet est un garde-fou contourné.

    Le message doit nommer l'échappatoire, l'alternative pilote et l'alternative
    cluster — sans quoi l'opérateur retombe sur l'édition du script.
    """
    stderr = run_verdict("local", "false", ABSENT_PROBE).stderr
    assert "--allow-no-gpu" in stderr, "Le refus ne nomme pas l'échappatoire explicite."
    assert "--mode cluster" in stderr, "Le refus ne propose pas le parcours orchestrateur."
    assert GPU_WAIVER_ENV_KEY in stderr, (
        "Le refus ne dit pas que le choix sera tracé dans l'environnement."
    )
    assert "nvidia" in stderr.lower(), "Le refus ne mentionne pas le pilote NVIDIA."


# ── 2. Branche « avec l'option » : l'absence est assumée, pas subie ────────────

def test_allow_no_gpu_lets_a_cpu_host_through() -> None:
    result = run_verdict("local", "true", ABSENT_PROBE)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "waived"


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_shell_and_doctor_accept_the_same_truthy_waiver_values(value) -> None:
    result = run_verdict("local", value, ABSENT_PROBE)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "waived"
    assert gpu_waiver_declared({GPU_WAIVER_ENV_KEY: value})


def test_option_does_not_lie_about_a_host_that_has_a_gpu() -> None:
    """La sonde passe avant l'échappatoire : `--allow-no-gpu` ne masque pas un GPU."""
    result = run_verdict("local", "true", PRESENT_PROBE)
    assert result.returncode == 0
    assert result.stdout.strip() == "detected"


def test_cluster_mode_never_requires_a_local_gpu() -> None:
    result = run_verdict("cluster", "false", ABSENT_PROBE)
    assert result.returncode == 0
    assert result.stdout.strip() == "delegated"


# ── 3. Câblage réel dans install.sh ───────────────────────────────────────────

def test_install_script_wires_the_option() -> None:
    """L'option existe, est documentée dans l'usage et passe par la bibliothèque."""
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "--allow-no-gpu) ALLOW_NO_GPU=true" in text, "L'option n'est pas parsée."
    assert "deploy_gpu_verdict" in text, (
        "install.sh ne délègue plus la décision GPU à la bibliothèque : les tests "
        "de branche ci-dessus n'exercent plus le code réellement utilisé."
    )
    assert "gpu-preflight-lib.sh" in text, "La bibliothèque n'est plus sourcée."
    assert 'deploy_set_env_value "$CONFIG_FILE" "$GPU_WAIVER_ENV_KEY" true' in text, (
        "Le choix n'est pas inscrit dans l'environnement généré : doctor ne "
        "pourra pas le distinguer d'une panne de pilote (OPS-012)."
    )


def test_update_respects_the_persisted_cpu_only_waiver() -> None:
    """Une installation CPU acceptée doit rester maintenable par update.sh."""
    text = UPDATE_SH.read_text(encoding="utf-8")
    assert 'source "$SCRIPT_DIR/deploy/gpu-preflight-lib.sh"' in text
    assert 'deploy_env_value "$CONFIG_FILE" "$GPU_WAIVER_ENV_KEY"' in text
    assert "deploy_gpu_verdict" in text
    assert 'required+=("${UPDATE_REQUIRED_COMMANDS_LOCAL[@]}")' not in text, (
        "update.sh exige encore nvidia-smi avant de pouvoir lire la dérogation persistée"
    )
    assert "dérogation persistée respectée" in text


@pytest.mark.parametrize(
    "extra_args, expected",
    [
        ([], "GPU            : exigé"),
        (["--allow-no-gpu"], "absence ASSUMÉE"),
    ],
    ids=["sans-option", "avec-option"],
)
def test_dry_run_announces_the_gpu_decision(extra_args, expected) -> None:
    """`--dry-run` exécute réellement le script et doit annoncer la décision."""
    result = subprocess.run(
        ["bash", str(INSTALL_SH), "--mode", "local", "--dry-run", *extra_args],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert expected in result.stdout, result.stdout


def test_env_key_is_the_same_on_both_sides() -> None:
    """
    Contrôle d'accord : le shell écrit la clé que Python lit.

    Deux constantes dans deux langages qui divergent silencieusement, c'est le
    scénario où l'option marche, le fichier est écrit, et doctor ne voit rien.
    """
    lib = GPU_LIB.read_text(encoding="utf-8")
    assert f'GPU_WAIVER_ENV_KEY="{GPU_WAIVER_ENV_KEY}"' in lib, (
        f"deploy/gpu-preflight-lib.sh n'écrit plus la clé {GPU_WAIVER_ENV_KEY} "
        "que doctor.GPU_WAIVER_ENV_KEY lit."
    )


# ── 4. doctor : deux diagnostics distincts, deux verdicts distincts ───────────

def test_doctor_reports_absent_gpu_as_a_warning_when_not_declared() -> None:
    """« GPU attendu mais absent » : avertissement, code de panne."""
    check = check_gpu_inventory(_Config(), None, "nvidia-smi introuvable", waived=False)
    assert check.status == "warn"
    assert check.code == "nvidia_smi_unavailable"
    assert "--allow-no-gpu" in check.message, (
        "Le diagnostic ne dit pas comment tracer une absence délibérée."
    )


def test_doctor_reports_absent_gpu_as_a_decision_when_declared() -> None:
    """« Pas de GPU, assumé » : ni le même statut, ni le même code."""
    check = check_gpu_inventory(_Config(), None, "nvidia-smi introuvable", waived=True)
    assert check.status == "skip"
    assert check.code == "gpu_absence_declared"
    assert not check.is_blocking
    assert GPU_WAIVER_ENV_KEY in check.message


def test_the_two_diagnostics_are_not_the_same_verdict() -> None:
    """
    L'exigence explicite d'OPS-012, énoncée comme une comparaison.

    Écrite ainsi, elle reste vraie même si les codes changent de nom : ce qui est
    interdit, c'est de rendre le MÊME verdict dans les deux situations.
    """
    accident = check_gpu_inventory(_Config(), None, "nvidia-smi introuvable", waived=False)
    decision = check_gpu_inventory(_Config(), None, "nvidia-smi introuvable", waived=True)
    assert (accident.status, accident.code) != (decision.status, decision.code), (
        "doctor rend le même verdict pour « GPU attendu mais absent » et « pas "
        "de GPU, assumé » : l'exploitant ne peut plus les distinguer (OPS-012)."
    )


def test_doctor_flags_a_stale_waiver() -> None:
    """Une renonciation qui a survécu au matériel qui la justifiait est signalée."""
    check = check_gpu_inventory(_Config(), [_gpu()], "", waived=True)
    assert check.status == "warn"
    assert check.code == "gpu_waiver_stale"
    assert not check.is_blocking


def test_a_real_gpu_without_waiver_still_passes() -> None:
    """Contrôle positif : le chemin nominal n'a pas été abîmé."""
    check = check_gpu_inventory(_Config(), [_gpu()], "", waived=False)
    assert check.status == "pass"
    assert check.code == "ok"


def test_cluster_mode_ignores_the_waiver() -> None:
    """En cluster, l'inventaire local n'a pas de sens : le verdict ne bouge pas."""
    for waived in (False, True):
        check = check_gpu_inventory(_Config("cluster"), None, "", waived=waived)
        assert (check.status, check.code) == ("skip", "delegated_to_nodes")


# ── 5. Lecture de la déclaration dans le fichier d'environnement ──────────────

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("true", True), ("TRUE", True), ("1", True), ("yes", True),
        ("on", True), (" true ", True),
        ("false", False), ("0", False), ("", False), ("peut-être", False),
    ],
)
def test_waiver_parsing(raw: str, expected: bool) -> None:
    assert gpu_waiver_declared({GPU_WAIVER_ENV_KEY: raw}) is expected


def test_waiver_absent_by_default() -> None:
    assert gpu_waiver_declared({}) is False


def _run_doctor_gpu_check(tmp_path: Path, monkeypatch, *, waiver_line: str):
    """
    Exécute le VRAI `run_doctor` sur un fichier d'environnement, sonde GPU vide.

    Sans ce bout en bout, les tests de `check_gpu_inventory` resteraient verts
    même si `run_doctor` cessait de lui transmettre la déclaration : la fonction
    saurait distinguer les deux cas, et doctor ne le ferait plus.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    env_file = tmp_path / "env"
    env_file.write_text(
        "GATEWAY_PORT=8000\nCLUSTER_MODE=local\nCUDA_VISIBLE_DEVICES=0\n" + waiver_line,
        encoding="utf-8",
    )
    env_file.chmod(0o640)

    async def _no_gpu(timeout: float = 5.0):
        return None, "nvidia-smi introuvable dans le PATH"

    monkeypatch.setattr(doctor, "probe_nvidia_smi", _no_gpu)
    monkeypatch.setattr(doctor, "DEFAULT_NGINX_CONF", tmp_path / "absent.conf")
    monkeypatch.setattr(doctor, "DEFAULT_SYSTEMD_UNIT", tmp_path / "absent.service")

    import asyncio

    report = asyncio.run(doctor.run_doctor(doctor.DoctorOptions(env_file=env_file)))
    checks = {check.name: check for check in report.checks}
    assert "gpu_inventory" in checks, "doctor ne rapporte plus d'inventaire GPU."
    return checks["gpu_inventory"]


def test_run_doctor_propagates_the_declaration(tmp_path: Path, monkeypatch) -> None:
    """Bout en bout : la clé du fichier d'environnement change le verdict rendu."""
    without = _run_doctor_gpu_check(tmp_path / "a", monkeypatch, waiver_line="")
    with_waiver = _run_doctor_gpu_check(
        tmp_path / "b", monkeypatch, waiver_line=f"{GPU_WAIVER_ENV_KEY}=true\n"
    )
    assert (without.status, without.code) == ("warn", "nvidia_smi_unavailable")
    assert (with_waiver.status, with_waiver.code) == ("skip", "gpu_absence_declared"), (
        "run_doctor ne transmet plus la déclaration à check_gpu_inventory : "
        "l'exploitant ne voit pas que cet hôte tourne sans GPU par décision."
    )


def test_doctor_reads_the_waiver_from_the_environment_file(tmp_path: Path) -> None:
    """
    Bout en bout côté lecture : le fichier écrit par install.sh est bien compris.

    On produit la ligne avec la MÊME fonction bash que l'installateur
    (`deploy_set_env_value`), puis on la relit avec le parseur de doctor : un
    désaccord de format (guillemets, espaces) se verrait ici.
    """
    env_file = tmp_path / "env"
    env_file.write_text("GATEWAY_PORT=8000\n", encoding="utf-8")
    result = subprocess.run(
        [
            "bash", "-c",
            f'source "{DEPLOY_DIR}/deploy-mode-lib.sh"; source "{GPU_LIB}"; '
            f'deploy_set_env_value "$1" "$GPU_WAIVER_ENV_KEY" true',
            "bash", str(env_file),
        ],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert gpu_waiver_declared(doctor.parse_env_file(env_file)) is True

    # Contrôle négatif : la remise à false est lue comme telle.
    subprocess.run(
        [
            "bash", "-c",
            f'source "{DEPLOY_DIR}/deploy-mode-lib.sh"; source "{GPU_LIB}"; '
            f'deploy_set_env_value "$1" "$GPU_WAIVER_ENV_KEY" false',
            "bash", str(env_file),
        ],
        check=True, capture_output=True, text=True,
    )
    assert gpu_waiver_declared(doctor.parse_env_file(env_file)) is False
