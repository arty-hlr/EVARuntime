"""La CI ne doit pas transformer une CVE en avertissement permanent."""

from __future__ import annotations

import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import dependency_audit  # noqa: E402


def test_exception_complete_is_transmise_a_chaque_audit(tmp_path):
    exceptions_file = tmp_path / "exceptions.txt"
    exceptions_file.write_text(
        "GHSA-abcd-1234-zzzz|2026-09-01|SEC-123|Pas de chemin atteignable\n",
        encoding="utf-8",
    )
    rules = dependency_audit.load_exceptions(
        exceptions_file, today=date(2026, 8, 3)
    )
    lock = tmp_path / "requirements.lock"
    lock.write_text("example==1.0 --hash=sha256:" + "a" * 64 + "\n", encoding="utf-8")
    calls: list[list[str]] = []

    def runner(command, *, check):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    assert dependency_audit.run_audits([lock], rules, runner=runner) == 0
    assert calls and "--require-hashes" in calls[0]
    assert "--disable-pip" in calls[0]
    assert calls[0][calls[0].index("--ignore-vuln") + 1] == "GHSA-abcd-1234-zzzz"


@pytest.mark.parametrize(
    "line, expected",
    [
        ("CVE-2025-0001|2026-08-02|SEC-1|temporaire", "expirée"),
        ("CVE-2025-0001|jamais|SEC-1|temporaire", "date d'expiration"),
        ("CVE-2025-0001|2026-09-01||temporaire", "format attendu"),
    ],
)
def test_exception_expiree_ou_incomplete_bloque_avant_audit(tmp_path, line, expected):
    exceptions_file = tmp_path / "exceptions.txt"
    exceptions_file.write_text(line + "\n", encoding="utf-8")

    with pytest.raises(dependency_audit.ExceptionPolicyError, match=expected):
        dependency_audit.load_exceptions(exceptions_file, today=date(2026, 8, 3))


def test_fichier_reel_ne_contient_aucune_exception_active():
    assert dependency_audit.load_exceptions(
        dependency_audit.DEFAULT_EXCEPTIONS, today=date(2026, 8, 3)
    ) == ()
