"""
COR-021 — aucun invariant de production ne doit être porté par un `assert`.

`python -O` retire toutes les instructions `assert` du bytecode, et rien
n'interdit `-O` dans une unité systemd (`ExecStart=… python -O …`,
`PYTHONOPTIMIZE=1` dans l'`EnvironmentFile`). Un garde-fou écrit `assert x is not
None` disparaît alors en silence : le refus explicite se transforme en
`AttributeError` opaque quelques lignes plus loin, au pire moment et sans nommer
la cause.

Ce test balaie **tout le code de production** des deux composants et échoue à la
première instruction `assert` rencontrée. Il porte un contrôle positif : sans lui,
un scanner cassé (mauvaise racine, filtre trop large, AST muet) resterait vert
en devenant aveugle — exactement le mode de panne que ce dépôt a déjà rencontré
sur les tests d'absence.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

# `gateway/tests/` → `gateway/` → racine du dépôt.
GATEWAY = Path(__file__).resolve().parent.parent
REPO = GATEWAY.parent

# Répertoires qui ne sont pas du code de production : les tests ont le droit
# d'employer `assert`, c'est leur langage. Les environnements virtuels et les
# caches ne sont pas notre code du tout.
EXCLUDED_DIRS = frozenset({
    "tests", ".venv", "venv", "__pycache__", ".git",
    ".pytest_cache", ".ruff_cache", ".mypy_cache",
})

PRODUCTION_ROOTS = (REPO / "gateway", REPO / "node_agent")


def production_sources() -> list[Path]:
    """Modules Python de production des deux composants, tests exclus."""
    fichiers: list[Path] = []
    for racine in PRODUCTION_ROOTS:
        if not racine.is_dir():
            continue
        for chemin in racine.rglob("*.py"):
            if EXCLUDED_DIRS & set(chemin.relative_to(racine).parts):
                continue
            fichiers.append(chemin)
    return sorted(fichiers)


def assert_lines(source: str) -> list[int]:
    """Numéros de ligne des instructions `assert` d'un source Python."""
    return [
        noeud.lineno
        for noeud in ast.walk(ast.parse(source))
        if isinstance(noeud, ast.Assert)
    ]


# ── Contrôle positif ──────────────────────────────────────────────────────────

def test_the_scanner_can_actually_see_an_assert() -> None:
    """
    Sans ce contrôle, un scanner aveugle rendrait le test d'absence inerte.

    Trois choses sont prouvées : l'AST repère un `assert`, il ne confond pas un
    appel nommé `assert_something()` avec l'instruction, et il descend dans les
    corps imbriqués (une classe, une méthode, un `try`).
    """
    assert assert_lines("assert x is not None\n") == [1]
    assert assert_lines("assertions = 3\nself.assert_called_once()\n") == []
    imbrique = (
        "class A:\n"
        "    def m(self):\n"
        "        try:\n"
        "            assert self.x\n"
        "        except Exception:\n"
        "            pass\n"
    )
    assert assert_lines(imbrique) == [4]


def test_the_scanner_actually_sees_the_repository() -> None:
    """Contrôle positif du périmètre : la liste balayée n'est ni vide ni tronquée."""
    sources = production_sources()
    noms = {chemin.name for chemin in sources}
    assert len(sources) > 40, f"périmètre suspect : {len(sources)} fichiers seulement"
    # Des modules des deux composants, et du sous-paquet visé par COR-021.
    assert {"main.py", "llama_version.py", "runtime_resolver.py"} <= noms
    assert any(chemin.parent.name == "node_agent" for chemin in sources)
    assert not any("tests" in chemin.parts for chemin in sources)


# ── L'absence proprement dite ─────────────────────────────────────────────────

@pytest.mark.parametrize(
    "source", production_sources(), ids=lambda p: str(p.relative_to(REPO))
)
def test_no_assert_carries_a_production_invariant(source: Path) -> None:
    lignes = assert_lines(source.read_text(encoding="utf-8"))
    assert not lignes, (
        f"{source.relative_to(REPO)} porte un invariant par `assert` (ligne(s) "
        f"{lignes}). Sous `python -O` il disparaît et le refus devient une erreur "
        "opaque : levez une exception nommée, ou retournez un refus explicite."
    )
