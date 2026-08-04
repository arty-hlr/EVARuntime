"""Le layout réellement déployé doit contenir tous les packages importés."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
GATEWAY_ROOT = REPO_ROOT / "gateway"
LAYOUT_LIB = GATEWAY_ROOT / "deploy" / "code-layout-lib.sh"
INSTALL_SH = GATEWAY_ROOT / "deploy" / "install.sh"
UPDATE_SH = GATEWAY_ROOT / "deploy" / "update.sh"


def _layout(function: str, *paths: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            "-c",
            f'source "$1"; shift; {function} "$@"',
            "bash",
            str(LAYOUT_LIB),
            *(str(path) for path in paths),
        ],
        capture_output=True,
        text=True,
    )


def test_layout_deploye_charge_bootstrap_et_la_politique_du_registre(tmp_path):
    """
    Reproduit la copie de production, puis importe depuis la CIBLE uniquement.

    Un test lancé depuis le checkout verrait toujours `gateway/bootstrap` et
    masquerait exactement la régression qui a échappé à la PR initiale.
    """
    cible = tmp_path / "opt" / "llm-gateway"
    result = _layout("deploy_sync_gateway_code", GATEWAY_ROOT, cible)
    assert result.returncode == 0, result.stderr

    assert (cible / "bootstrap" / "__init__.py").is_file()
    assert (cible / "bootstrap" / "catalog.yaml").is_file()
    assert (cible / "cluster" / "__init__.py").is_file()
    assert (cible / "requirements.lock").read_bytes() == (
        GATEWAY_ROOT / "requirements.lock"
    ).read_bytes()

    imported = subprocess.run(
        [
            sys.executable,
            "-c",
            "from bootstrap import registry_writer; "
            "import model_registry; "
            "assert model_registry._text_write_policy() is registry_writer",
        ],
        cwd=cible,
        capture_output=True,
        text=True,
    )
    assert imported.returncode == 0, imported.stderr

    cli_contract = subprocess.run(
        [
            sys.executable,
            "-c",
            "from typer.main import get_command; "
            "import cli; "
            "command = get_command(cli.app).commands['bootstrap-apply']; "
            "options = {option for parameter in command.params "
            "for option in getattr(parameter, 'opts', ())}; "
            "assert '--env-file' in options; "
            "assert '--runtime-root' in options; "
            "assert '--option-absente' not in options",
        ],
        cwd=cible,
        capture_output=True,
        text=True,
    )
    # Rich injecte des séquences ANSI dans l'aide sur GitHub Actions. Inspecter
    # le contrat Click vérifie les options sans dépendre du rendu du terminal.
    assert cli_contract.returncode == 0, cli_contract.stderr


def test_synchronisation_retire_un_module_obsolete(tmp_path):
    cible = tmp_path / "opt" / "llm-gateway"
    assert _layout("deploy_sync_gateway_code", GATEWAY_ROOT, cible).returncode == 0
    obsolete = cible / "bootstrap" / "ancien_module.py"
    obsolete.write_text("raise RuntimeError('obsolete')\n", encoding="utf-8")

    result = _layout("deploy_sync_gateway_code", GATEWAY_ROOT, cible)
    assert result.returncode == 0, result.stderr
    assert not obsolete.exists(), "un module retiré de la release reste importable"


def test_installation_neuve_recoit_tous_les_artefacts_operateur(tmp_path):
    cible = tmp_path / "opt" / "llm-gateway"
    result = _layout("deploy_sync_gateway_operational_files", GATEWAY_ROOT, cible)
    assert result.returncode == 0, result.stderr

    expected = {
        "llm-gateway-backup.sh",
        "smoke_test.sh",
        "deploy-mode-lib.sh",
        "nginx-lib.sh",
        "runtime-variants.yaml.example",
    }
    assert {path.name for path in (cible / "deploy").iterdir()} == expected

    help_result = subprocess.run(
        ["bash", str(cible / "deploy" / "smoke_test.sh"), "--help"],
        capture_output=True,
        text=True,
    )
    assert help_result.returncode == 0, help_result.stderr
    assert "--base-url" in help_result.stdout


def test_snapshot_et_rollback_restaurent_bootstrap(tmp_path):
    installe = tmp_path / "opt" / "llm-gateway"
    snapshot = tmp_path / "snapshot"
    assert _layout("deploy_sync_gateway_code", GATEWAY_ROOT, installe).returncode == 0
    assert _layout(
        "deploy_sync_gateway_operational_files", GATEWAY_ROOT, installe
    ).returncode == 0
    original = (installe / "bootstrap" / "registry_writer.py").read_text(encoding="utf-8")
    lock_original = (installe / "requirements.lock").read_bytes()
    smoke_original = (installe / "deploy" / "smoke_test.sh").read_text(encoding="utf-8")

    snap = _layout("deploy_snapshot_gateway_code", installe, snapshot)
    assert snap.returncode == 0, snap.stderr
    (installe / "bootstrap" / "registry_writer.py").write_text(
        "raise RuntimeError('release cassée')\n", encoding="utf-8"
    )
    (installe / "bootstrap" / "module_nouveau.py").write_text("", encoding="utf-8")
    (installe / "requirements.lock").write_text("release cassée\n", encoding="utf-8")
    (installe / "deploy" / "smoke_test.sh").write_text(
        "exit 99\n", encoding="utf-8"
    )

    restore = _layout("deploy_restore_gateway_code", snapshot, installe)
    assert restore.returncode == 0, restore.stderr
    assert (installe / "bootstrap" / "registry_writer.py").read_text(encoding="utf-8") == original
    assert not (installe / "bootstrap" / "module_nouveau.py").exists()
    assert (installe / "requirements.lock").read_bytes() == lock_original
    assert (installe / "deploy" / "smoke_test.sh").read_text(encoding="utf-8") == smoke_original


def test_install_et_update_utilisent_le_layout_partage():
    install = INSTALL_SH.read_text(encoding="utf-8")
    update = UPDATE_SH.read_text(encoding="utf-8")

    assert 'source "$SCRIPT_DIR/deploy/code-layout-lib.sh"' in install
    assert 'deploy_sync_gateway_code "$SCRIPT_DIR" "$INSTALL_DIR"' in install
    assert 'deploy_sync_gateway_operational_files "$SCRIPT_DIR" "$INSTALL_DIR"' in install
    assert 'source "$SCRIPT_DIR/deploy/code-layout-lib.sh"' in update
    assert 'deploy_sync_gateway_code "$SCRIPT_DIR" "$INSTALL_DIR"' in update
    assert 'deploy_sync_gateway_operational_files "$SCRIPT_DIR" "$INSTALL_DIR"' in update
    assert 'deploy_snapshot_gateway_code "$INSTALL_DIR" "$CODE_SNAPSHOT"' in update
    assert 'deploy_restore_gateway_code "$snapshot" "$INSTALL_DIR"' in update


def test_layout_refuse_une_cible_dangereuse_ou_identique():
    identique = _layout("deploy_sync_gateway_code", GATEWAY_ROOT, GATEWAY_ROOT)
    assert identique.returncode != 0
    assert "dangereuses ou identiques" in identique.stderr

    racine = _layout("deploy_sync_gateway_code", GATEWAY_ROOT, Path("/"))
    assert racine.returncode != 0
    assert "dangereuses ou identiques" in racine.stderr
