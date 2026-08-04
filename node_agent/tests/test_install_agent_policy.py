"""Le node-agent installé ne doit jamais démarrer avec un runtime non attesté."""

from __future__ import annotations

import subprocess
from pathlib import Path

from config import AgentSettings


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "deploy" / "install-agent.sh"
CANONICAL_BINARY = Path("/opt/llama.cpp/current/llama-server")


def test_agent_default_uses_the_canonical_runtime_path() -> None:
    assert AgentSettings().llama_server_bin == CANONICAL_BINARY
    assert f"LLAMA_SERVER_BIN={CANONICAL_BINARY}" in (
        ROOT / "env.example"
    ).read_text(encoding="utf-8")


def test_installer_exposes_and_requires_a_positive_build_floor() -> None:
    help_result = subprocess.run(
        ["bash", str(INSTALLER), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert help_result.returncode == 0
    assert "--llama-server-bin" in help_result.stdout
    assert "--llama-min-build" in help_result.stdout

    source = INSTALLER.read_text(encoding="utf-8")
    assert 'LLAMA_SERVER_MIN_BUILD="${LLAMA_SERVER_MIN_BUILD:-}"' in source
    assert "(( LLAMA_SERVER_MIN_BUILD > 0 ))" in source
    assert '[[ -x "$LLAMA_SERVER_BIN" ]]' in source
    assert 'gateway/llama_version.py"' in source
    assert "LLAMA_SERVER_MIN_BUILD=$LLAMA_SERVER_MIN_BUILD" in source
