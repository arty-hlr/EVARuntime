#!/usr/bin/env python3
"""Audit CVE bloquant avec exceptions rares, explicites et expirables."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCKS = (
    REPO_ROOT / "gateway" / "requirements.lock",
    REPO_ROOT / "node_agent" / "requirements.lock",
)
DEFAULT_EXCEPTIONS = REPO_ROOT / ".github" / "dependency-audit-exceptions.txt"
_VULNERABILITY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$")


class ExceptionPolicyError(ValueError):
    """Le fichier d'exceptions ne respecte pas la politique du dépôt."""


@dataclass(frozen=True)
class AuditException:
    vulnerability_id: str
    expires_on: date
    tracking_reference: str
    justification: str


def load_exceptions(path: Path, *, today: date | None = None) -> tuple[AuditException, ...]:
    """Charge les exceptions et refuse toute dette expirée ou ambiguë."""
    current = today or date.today()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ExceptionPolicyError(f"fichier d'exceptions illisible : {path}: {exc}") from exc

    rules: list[AuditException] = []
    seen: set[str] = set()
    for line_number, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = [part.strip() for part in stripped.split("|", 3)]
        if len(parts) != 4 or not all(parts):
            raise ExceptionPolicyError(
                f"{path}:{line_number}: format attendu ID|YYYY-MM-DD|REFERENCE|JUSTIFICATION"
            )
        vulnerability_id, expiry_text, reference, justification = parts
        if not _VULNERABILITY_ID.fullmatch(vulnerability_id):
            raise ExceptionPolicyError(
                f"{path}:{line_number}: identifiant de vulnérabilité invalide"
            )
        try:
            expiry = date.fromisoformat(expiry_text)
        except ValueError as exc:
            raise ExceptionPolicyError(
                f"{path}:{line_number}: date d'expiration ISO invalide"
            ) from exc
        if expiry < current:
            raise ExceptionPolicyError(
                f"{path}:{line_number}: exception {vulnerability_id} expirée le {expiry}"
            )
        normalized = vulnerability_id.upper()
        if normalized in seen:
            raise ExceptionPolicyError(
                f"{path}:{line_number}: exception dupliquée pour {vulnerability_id}"
            )
        seen.add(normalized)
        rules.append(AuditException(vulnerability_id, expiry, reference, justification))
    return tuple(rules)


def audit_command(lock: Path, exceptions: Sequence[AuditException]) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "pip_audit",
        "--require-hashes",
        "--disable-pip",
    ]
    for rule in exceptions:
        command.extend(("--ignore-vuln", rule.vulnerability_id))
    command.extend(("-r", str(lock)))
    return command


def run_audits(
    locks: Sequence[Path],
    exceptions: Sequence[AuditException],
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> int:
    for lock in locks:
        if not lock.is_file():
            print(f"Lockfile introuvable : {lock}", file=sys.stderr)
            return 2
        completed = runner(audit_command(lock, exceptions), check=False)
        if completed.returncode != 0:
            return completed.returncode
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("locks", nargs="*", type=Path, default=list(DEFAULT_LOCKS))
    parser.add_argument("--exceptions", type=Path, default=DEFAULT_EXCEPTIONS)
    args = parser.parse_args(argv)
    try:
        exceptions = load_exceptions(args.exceptions)
    except ExceptionPolicyError as exc:
        print(f"Politique d'exceptions refusée : {exc}", file=sys.stderr)
        return 2
    return run_audits(args.locks, exceptions)


if __name__ == "__main__":
    raise SystemExit(main())
