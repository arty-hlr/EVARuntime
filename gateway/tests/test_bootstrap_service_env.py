"""Raccord atomique du runtime bootstrap avec l'unité systemd."""
from __future__ import annotations

import os
import inspect
from pathlib import Path

import pytest

from bootstrap import service_env
import cli
from doctor import load_settings_from_env_file


def _env(path: Path) -> Path:
    path.write_text(
        "# secrets conservés\n"
        "ADMIN_SECRET=secret-qui-ne-doit-pas-bouger\n"
        "LLAMA_SERVER_BIN=/usr/local/bin/llama-server\n"
        "LLAMA_SERVER_MIN_BUILD=0\n"
        "CORS_ALLOW_ORIGINS=\n",
        encoding="utf-8",
    )
    path.chmod(0o640)
    return path


def test_hardening_updates_only_runtime_keys_and_loads_with_real_settings(tmp_path: Path) -> None:
    target = _env(tmp_path / "env")
    binary = Path("/opt/llama.cpp/current/llama-server")

    result = service_env.harden_runtime_environment(
        target, binary=binary, min_build=6120
    )

    assert result.changed is True
    text = target.read_text(encoding="utf-8")
    assert "ADMIN_SECRET=secret-qui-ne-doit-pas-bouger" in text
    assert "LLAMA_SERVER_BIN=/opt/llama.cpp/current/llama-server" in text
    assert "LLAMA_SERVER_MIN_BUILD=6120" in text
    assert "CORS_ALLOW_ORIGINS=\n" in text
    assert target.stat().st_mode & 0o777 == 0o640

    config = load_settings_from_env_file(target)
    assert config.llama_server_bin == binary
    assert config.llama_server_min_build == 6120


def test_hardening_is_idempotent(tmp_path: Path) -> None:
    target = _env(tmp_path / "env")
    binary = Path("/opt/llama.cpp/current/llama-server")
    service_env.harden_runtime_environment(target, binary=binary, min_build=6120)
    before = target.stat().st_ino

    result = service_env.harden_runtime_environment(
        target, binary=binary, min_build=6120
    )

    assert result.changed is False
    assert target.stat().st_ino == before


@pytest.mark.parametrize("min_build", [0, -1, True])
def test_hardening_refuses_an_inert_minimum(tmp_path: Path, min_build: object) -> None:
    target = _env(tmp_path / "env")
    with pytest.raises(service_env.ServiceEnvError, match="strictement positif"):
        service_env.harden_runtime_environment(
            target,
            binary=Path("/opt/llama.cpp/current/llama-server"),
            min_build=min_build,  # type: ignore[arg-type]
        )


def test_hardening_refuses_duplicate_managed_keys(tmp_path: Path) -> None:
    target = _env(tmp_path / "env")
    target.write_text(
        target.read_text(encoding="utf-8") + "LLAMA_SERVER_MIN_BUILD=99\n",
        encoding="utf-8",
    )
    with pytest.raises(service_env.ServiceEnvError, match="dupliquée"):
        service_env.harden_runtime_environment(
            target,
            binary=Path("/opt/llama.cpp/current/llama-server"),
            min_build=6120,
        )


def test_hardening_recognizes_systemd_whitespace_and_export(tmp_path: Path) -> None:
    target = _env(tmp_path / "env")
    target.write_text(
        target.read_text(encoding="utf-8")
        .replace(
            "LLAMA_SERVER_BIN=/usr/local/bin/llama-server",
            "  export LLAMA_SERVER_BIN = /usr/local/bin/llama-server",
        ),
        encoding="utf-8",
    )

    service_env.harden_runtime_environment(
        target,
        binary=Path("/opt/llama.cpp/current/llama-server"),
        min_build=6120,
    )

    lines = target.read_text(encoding="utf-8").splitlines()
    assert lines.count("LLAMA_SERVER_BIN=/opt/llama.cpp/current/llama-server") == 1


def test_hardening_refuses_a_symlink(tmp_path: Path) -> None:
    real = _env(tmp_path / "real-env")
    link = tmp_path / "env"
    link.symlink_to(real)
    with pytest.raises(service_env.ServiceEnvError, match="illisible"):
        service_env.harden_runtime_environment(
            link,
            binary=Path("/opt/llama.cpp/current/llama-server"),
            min_build=6120,
        )


def test_hardening_does_not_overwrite_a_concurrent_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _env(tmp_path / "env")
    original_reader = service_env._read_regular
    calls = 0

    def racing_reader(path: Path):
        nonlocal calls
        calls += 1
        if calls == 2:
            path.write_text(
                path.read_text(encoding="utf-8") + "OPERATOR_NOTE=keep-me\n",
                encoding="utf-8",
            )
            os.chmod(path, 0o640)
        return original_reader(path)

    monkeypatch.setattr(service_env, "_read_regular", racing_reader)
    with pytest.raises(service_env.ServiceEnvError, match="muté concurremment"):
        service_env.harden_runtime_environment(
            target,
            binary=Path("/opt/llama.cpp/current/llama-server"),
            min_build=6120,
        )
    assert "OPERATOR_NOTE=keep-me" in target.read_text(encoding="utf-8")


def test_bootstrap_apply_really_publishes_the_runtime_environment() -> None:
    """Contrôle de raccord : le module testé ci-dessus est appelé par la CLI."""
    source = inspect.getsource(cli.bootstrap_apply)
    assert "_service_env.harden_runtime_environment(" in source
    assert "outcome.exit_code() == _schema.EXIT_OK" in source
    assert "config.runtime.published_binary" in source
