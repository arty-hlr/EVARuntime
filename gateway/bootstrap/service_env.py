"""Publication atomique des réglages runtime dans l'EnvironmentFile systemd.

Le runtime n'est réellement raccordé que lorsque la gateway et l'applicateur
désignent le même lien ``current``. Ce module retouche uniquement les deux clés
qu'un plan de bootstrap peut attester ; les secrets et les choix d'exploitation
voisins restent byte-for-byte inchangés.
"""
from __future__ import annotations

import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path


class ServiceEnvError(ValueError):
    """L'EnvironmentFile ne peut pas être modifié sans risque."""


@dataclass(frozen=True)
class RuntimeEnvironmentUpdate:
    path: Path
    binary: Path
    min_build: int
    changed: bool


_MANAGED_KEYS = ("LLAMA_SERVER_BIN", "LLAMA_SERVER_MIN_BUILD")
_ASSIGNMENT_RE = re.compile(
    r"^[ \t]*(?:export[ \t]+)?(?P<key>[A-Z][A-Z0-9_]*)[ \t]*="
)


def _read_regular(path: Path) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ServiceEnvError(f"EnvironmentFile illisible ({path}) : {exc}") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ServiceEnvError("l'EnvironmentFile doit être un fichier régulier")
        if info.st_uid not in {0, os.geteuid()}:
            raise ServiceEnvError(
                "l'EnvironmentFile n'appartient ni à root ni à l'utilisateur courant"
            )
        if info.st_mode & 0o027:
            raise ServiceEnvError(
                f"permissions trop ouvertes sur l'EnvironmentFile ({info.st_mode & 0o777:04o})"
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks), info
    finally:
        os.close(descriptor)


def _render(original: str, values: dict[str, str]) -> str:
    seen: dict[str, int] = {key: 0 for key in _MANAGED_KEYS}
    rendered: list[str] = []
    for line in original.splitlines(keepends=True):
        match = _ASSIGNMENT_RE.match(line)
        key = match.group("key") if match else None
        if key not in values:
            rendered.append(line)
            continue
        seen[key] += 1
        if seen[key] > 1:
            raise ServiceEnvError(f"clé {key} dupliquée dans l'EnvironmentFile")
        ending = "\n" if line.endswith("\n") else ""
        rendered.append(f"{key}={values[key]}{ending}")

    missing = [key for key, count in seen.items() if count == 0]
    if missing:
        if rendered and not rendered[-1].endswith("\n"):
            rendered[-1] += "\n"
        rendered.append("\n# Runtime attesté par bootstrap-apply\n")
        rendered.extend(f"{key}={values[key]}\n" for key in missing)
    return "".join(rendered)


def harden_runtime_environment(
    path: Path,
    *,
    binary: Path,
    min_build: int,
) -> RuntimeEnvironmentUpdate:
    """Raccorde atomiquement le runtime publié et son plancher de sécurité."""
    target = Path(path)
    published = Path(binary)
    if not published.is_absolute():
        raise ServiceEnvError("LLAMA_SERVER_BIN doit être un chemin absolu")
    if any(character.isspace() or ord(character) < 32 for character in str(published)):
        raise ServiceEnvError("LLAMA_SERVER_BIN contient un caractère non sûr")
    if not isinstance(min_build, int) or isinstance(min_build, bool) or min_build <= 0:
        raise ServiceEnvError(
            "LLAMA_SERVER_MIN_BUILD doit être strictement positif pour une application réelle"
        )

    original_bytes, original_info = _read_regular(target)
    try:
        original = original_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ServiceEnvError("l'EnvironmentFile n'est pas un document UTF-8") from exc

    rendered = _render(
        original,
        {
            "LLAMA_SERVER_BIN": str(published),
            "LLAMA_SERVER_MIN_BUILD": str(min_build),
        },
    )
    if rendered.encode("utf-8") == original_bytes:
        return RuntimeEnvironmentUpdate(target, published, min_build, changed=False)

    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.bootstrap-", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, stat.S_IMODE(original_info.st_mode))
        os.fchown(descriptor, original_info.st_uid, original_info.st_gid)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            descriptor = -1
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())

        current_bytes, current_info = _read_regular(target)
        identity_before = (
            original_info.st_dev,
            original_info.st_ino,
            original_info.st_uid,
            original_info.st_gid,
            stat.S_IMODE(original_info.st_mode),
        )
        identity_now = (
            current_info.st_dev,
            current_info.st_ino,
            current_info.st_uid,
            current_info.st_gid,
            stat.S_IMODE(current_info.st_mode),
        )
        if current_bytes != original_bytes or identity_now != identity_before:
            raise ServiceEnvError(
                "l'EnvironmentFile a muté concurremment ; aucune valeur n'a été écrasée"
            )

        os.replace(temporary, target)
        directory = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)

    return RuntimeEnvironmentUpdate(target, published, min_build, changed=True)


__all__ = [
    "RuntimeEnvironmentUpdate",
    "ServiceEnvError",
    "harden_runtime_environment",
]
