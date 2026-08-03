"""
OPS-011 — les prérequis documentés doivent être ceux que les scripts exigent.

Ce module est un test de NON-RÉGRESSION de déploiement, pas un test de code.

Pourquoi ce test existe
-----------------------
`node_agent/deploy/install-agent.sh` exige `rsync` : le préflight le réclame et
le script s'en sert deux fois. Le mot n'apparaissait **nulle part** dans
`docs/deployment.md`. Sur une Debian 13 minimale, `rsync` n'est pas installé :
l'installation d'un nœud neuf échouait au premier écran, sur une dépendance que
rien n'annonçait. Reproduit deux fois en déploiement réel.

Ce que ce test fait, et pourquoi il ne recopie RIEN
---------------------------------------------------
Un test qui recopierait la liste des prérequis raterait la PROCHAINE dépendance
ajoutée : il vérifierait que la documentation est cohérente avec lui-même, pas
avec les scripts. Il serait vrai et inutile.

Le mécanisme retenu tient en trois maillons, et c'est leur chaînage qui fait la
garantie :

1. **Dérivation.** Chaque script déclare ses dépendances de commandes dans un
   ou plusieurs tableaux bash nommés `*_REQUIRED_COMMANDS*` / `*_OPTIONAL_
   COMMANDS*`. `derive_declared_commands()` parse ces déclarations dans le TEXTE
   DU SCRIPT. La liste attendue n'existe donc pas dans ce fichier : elle est
   lue dans le code de production à chaque exécution.

2. **Exhaustivité du tableau.** Le maillon faible d'une dérivation, c'est une
   dépendance exprimée AILLEURS que dans le tableau. On interdit donc tout
   `command -v <nom littéral>` dans les scripts couverts : les préflights
   n'itèrent plus que sur une variable. Une dépendance ajoutée « à la main »
   fait échouer ce test, ce qui la ramène dans le tableau, donc dans la
   dérivation, donc dans l'exigence de documentation.

3. **Documentation.** Chaque commande dérivée doit apparaître, en littéral
   `` `commande` ``, dans la section 1 de `docs/deployment.md`, elle-même
   découpée mécaniquement sur ses titres.

Chaque assertion d'ABSENCE porte un contrôle positif : sans lui, un motif de
recherche devenu obsolète rendrait le test silencieusement inerte.

Aucun de ces tests ne touche l'hôte : ni systemd, ni GPU, ni root, ni réseau.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
GATEWAY_DEPLOY = REPO_ROOT / "gateway" / "deploy"
AGENT_DEPLOY = REPO_ROOT / "node_agent" / "deploy"
DEPLOYMENT_DOC = REPO_ROOT / "docs" / "deployment.md"

# Scripts dont le préflight est déclaratif et donc dérivable. Ce sont les quatre
# points d'entrée privilégiés : installer et mettre à jour, gateway et nœud.
COVERED_SCRIPTS = (
    GATEWAY_DEPLOY / "install.sh",
    GATEWAY_DEPLOY / "update.sh",
    AGENT_DEPLOY / "install-agent.sh",
    AGENT_DEPLOY / "update-agent.sh",
)

# Déclaration : NOM_REQUIRED_COMMANDS[_VARIANTE]=(cmd cmd cmd)
_ARRAY_RE = re.compile(
    r"^(?P<name>[A-Z][A-Z0-9_]*_(?:REQUIRED|OPTIONAL)_COMMANDS[A-Z0-9_]*)"
    r"=\((?P<body>[^)]*)\)",
    re.MULTILINE,
)

# `command -v <nom littéral>` : la forme qu'on interdit dans les scripts
# couverts. Un `command -v "$var"` (la boucle de préflight) ne correspond pas,
# puisqu'il commence par un guillemet ou un `$`.
_LITERAL_COMMAND_V_RE = re.compile(r"command -v\s+(?P<name>[A-Za-z][A-Za-z0-9_.+-]*)")

# Un nom de commande plausible : évite qu'un commentaire mal placé dans un
# tableau n'injecte du bruit dans la liste dérivée.
_COMMAND_NAME_RE = re.compile(r"^[a-z][a-z0-9_.+-]*$")


def _script_text(script: Path) -> str:
    return script.read_text(encoding="utf-8")


def _strip_comment(line: str) -> str:
    """Retire un commentaire de fin de ligne bash (aucun `#` dans un nom de commande)."""
    return line.split("#", 1)[0]


def derive_declared_commands(script: Path) -> dict[str, tuple[str, ...]]:
    """
    Lit les tableaux `*_REQUIRED_COMMANDS*` / `*_OPTIONAL_COMMANDS*` du script.

    C'est LE mécanisme de dérivation : la liste attendue par ce test n'est écrite
    nulle part ici, elle est extraite du code de production.
    """
    declared: dict[str, tuple[str, ...]] = {}
    for match in _ARRAY_RE.finditer(_script_text(script)):
        body = " ".join(_strip_comment(line) for line in match["body"].splitlines())
        tokens = tuple(
            token for token in body.split() if _COMMAND_NAME_RE.match(token)
        )
        declared[match["name"]] = tokens
    return declared


def all_declared_commands() -> set[str]:
    """Union des commandes déclarées par tous les scripts couverts."""
    found: set[str] = set()
    for script in COVERED_SCRIPTS:
        for commands in derive_declared_commands(script).values():
            found.update(commands)
    return found


def documented_commands() -> set[str]:
    """
    Littéraux `` `commande` `` présents dans la SECTION 1 de deployment.md.

    La section est découpée sur ses titres, pas cherchée dans tout le document :
    citer `rsync` au détour de la section 13 ne documente pas un prérequis.
    """
    text = DEPLOYMENT_DOC.read_text(encoding="utf-8")
    start = text.index("\n## 1. Prérequis")
    end = text.index("\n## 2.", start)
    section = text[start:end]
    return {
        token for token in re.findall(r"`([^`\n]+)`", section)
        if _COMMAND_NAME_RE.match(token)
    }


# ── 1. La dérivation voit quelque chose (contrôle positif du mécanisme) ────────

@pytest.mark.parametrize("script", COVERED_SCRIPTS, ids=lambda p: p.name)
def test_script_declares_its_commands_in_named_arrays(script: Path) -> None:
    """Chaque script couvert expose au moins un tableau déclaratif non vide."""
    declared = derive_declared_commands(script)
    assert declared, (
        f"{script.name} ne déclare aucun tableau *_REQUIRED_COMMANDS* : la "
        "dérivation d'OPS-011 ne voit plus rien et ce test est devenu inerte."
    )
    non_empty = {name: cmds for name, cmds in declared.items() if cmds}
    assert non_empty, (
        f"{script.name} : tous les tableaux dérivés sont vides — le parseur ne "
        f"reconnaît plus la syntaxe. Tableaux vus : {sorted(declared)}"
    )


def test_derivation_finds_the_defect_that_opened_this_item() -> None:
    """
    Contrôle positif nommé : `rsync` doit sortir de la dérivation.

    C'est la dépendance qui a fait échouer deux installations réelles. Si elle
    n'est plus dérivée, le mécanisme est cassé, quoi que disent les autres tests.
    """
    agent_commands = set()
    for commands in derive_declared_commands(AGENT_DEPLOY / "install-agent.sh").values():
        agent_commands.update(commands)
    assert "rsync" in agent_commands, (
        "install-agent.sh n'expose plus `rsync` par ses tableaux déclaratifs : "
        "soit la dépendance a disparu, soit le parseur de ce test ne la voit "
        "plus. Dans les deux cas, OPS-011 n'est plus couvert."
    )


def test_derivation_rejects_non_command_tokens() -> None:
    """Le parseur ne remonte que des noms de commande plausibles."""
    declared = all_declared_commands()
    assert declared, "Aucune commande dérivée : le parseur est inerte."
    intruders = {c for c in declared if not _COMMAND_NAME_RE.match(c)}
    assert not intruders, f"Jetons non plausibles remontés par la dérivation : {intruders}"


# ── 2. Le tableau est la SEULE expression du préflight ────────────────────────

@pytest.mark.parametrize("script", COVERED_SCRIPTS, ids=lambda p: p.name)
def test_every_probed_command_is_declared_in_an_array(script: Path) -> None:
    """
    Aucune dépendance ne s'exprime hors des tableaux.

    Sans cette règle, la dérivation resterait vraie tout en ratant la prochaine
    dépendance : il suffirait d'écrire `command -v foo || die …` à la main pour
    qu'elle échappe à la documentation.

    Deux formes légitimes coexistent, et toutes deux passent par un tableau :
    - le préflight bloquant itère sur une VARIABLE alimentée par
      `*_REQUIRED_COMMANDS*` ;
    - une sonde de fonction optionnelle (`if command -v nginx; then …`) cite le
      nom en clair, mais ce nom doit figurer dans `*_OPTIONAL_COMMANDS*`.
    """
    text = _script_text(script)

    # Contrôle positif : le script contient bien une boucle de préflight qui
    # interroge une VARIABLE. Sans lui, l'assertion ci-dessous serait vraie
    # parce que le script n'a plus de préflight du tout.
    assert re.search(r'command -v "\$', text), (
        f"{script.name} n'a plus de boucle `command -v \"$…\"` : le préflight a "
        "disparu ou changé de forme, ce test est devenu inerte."
    )

    declared: set[str] = set()
    for commands in derive_declared_commands(script).values():
        declared.update(commands)

    literals = {
        match["name"]
        for line in text.splitlines()
        if not line.strip().startswith("#")
        for match in _LITERAL_COMMAND_V_RE.finditer(_strip_comment(line))
    }
    undeclared = sorted(literals - declared)
    assert not undeclared, (
        f"{script.name} sonde des commandes non déclarées : {undeclared}. Une "
        "dépendance exprimée hors des tableaux *_REQUIRED_COMMANDS* / "
        "*_OPTIONAL_COMMANDS* échappe à la dérivation d'OPS-011, donc à la "
        "documentation. Déclarez-la dans le tableau du script."
    )


def test_undeclared_probe_would_be_caught(tmp_path: Path) -> None:
    """
    Contre-épreuve du garde-fou ci-dessus, sur un script fabriqué.

    Prouve que la règle attrape bien une dépendance ajoutée « à la main » —
    exactement le geste qui avait fait disparaître `rsync` des prérequis.
    """
    fake = tmp_path / "install-fake.sh"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        "FAKE_REQUIRED_COMMANDS=(python3)\n"
        'for c in "${FAKE_REQUIRED_COMMANDS[@]}"; do command -v "$c" || exit 1; done\n'
        'command -v rsync >/dev/null || exit 1\n',
        encoding="utf-8",
    )
    declared: set[str] = set()
    for commands in derive_declared_commands(fake).values():
        declared.update(commands)
    literals = {
        match["name"]
        for line in fake.read_text(encoding="utf-8").splitlines()
        for match in _LITERAL_COMMAND_V_RE.finditer(_strip_comment(line))
    }
    assert declared == {"python3"}
    assert sorted(literals - declared) == ["rsync"], (
        "Le garde-fou ne détecte plus une sonde non déclarée : il ne protège "
        "plus contre la régression OPS-011."
    )


# ── 3. Tout ce qui est dérivé est documenté ───────────────────────────────────

def test_documentation_section_is_readable() -> None:
    """
    Contrôle positif de l'extracteur de documentation.

    Il doit voir des littéraux, et ne pas en inventer : un jeton absent du
    document ne doit jamais ressortir comme documenté.
    """
    documented = documented_commands()
    assert documented, (
        "La section 1 de docs/deployment.md ne contient aucun littéral "
        "`commande` : le découpage de section ou le motif est cassé."
    )
    assert "python3" in documented, (
        "python3 devrait être documenté en section 1 — l'extracteur ne lit pas "
        "la bonne section."
    )
    assert "cette-commande-nexiste-pas" not in documented, (
        "L'extracteur remonte un jeton absent du document : il est trop laxiste."
    )


def test_every_declared_command_is_documented() -> None:
    """
    Le cœur d'OPS-011 : documentation ⊇ dépendances réelles des scripts.

    La liste attendue est DÉRIVÉE des tableaux bash, jamais recopiée ici. Ajouter
    une commande à un script sans l'ajouter à `docs/deployment.md` §1 fait
    échouer ce test — c'est précisément le scénario `rsync`.
    """
    declared = all_declared_commands()
    documented = documented_commands()
    missing = sorted(declared - documented)
    assert not missing, (
        "Commandes exigées par les scripts de déploiement mais ABSENTES des "
        f"prérequis de docs/deployment.md §1 : {missing}. Une installation "
        "neuve échouera au premier écran sur une dépendance que rien n'annonce "
        "(OPS-011). Ajoutez-les au tableau de la section 1."
    )
