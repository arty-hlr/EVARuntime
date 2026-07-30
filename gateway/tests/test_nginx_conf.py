"""
Tests de l'artefact nginx réellement livré (`gateway/deploy/nginx.conf`).

nginx n'étant pas installé sur les runners, on ne peut pas s'appuyer sur
`nginx -t` : ces tests parsent le fichier livré et vérifient les invariants qui,
sur un hôte réel, produiraient un warning au reload (OPS-009) ou un 504 côté
client (COR-009).

Règle CLAUDE.md appliquée partout ici : tout test qui assère une ABSENCE porte un
contrôle positif prouvant qu'il voit encore quelque chose — sinon il devient
inerte le jour où le parsing casse, sans jamais échouer.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SHIPPED_NGINX_CONF = REPO_ROOT / "gateway" / "deploy" / "nginx.conf"
DEPLOYMENT_DOC = REPO_ROOT / "docs" / "deployment.md"


# ── Socle nginx supporté ──────────────────────────────────────────────────────
# `docs/deployment.md` §1 : nginx 1.18+ (Ubuntu 22.04 LTS → 1.18, Ubuntu 24.04
# LTS → 1.24). La conf livrée doit donc démarrer sur 1.18 ET ne produire aucun
# warning de dépréciation sur les versions récentes (Debian 13 → 1.26+).

# Paramètres de `listen` dépréciés ou retirés, avec la version concernée.
DEPRECATED_LISTEN_PARAMS = {
    "http2": "déprécié depuis nginx 1.25.1 — utiliser la directive « http2 on; »",
    "spdy": "retiré depuis nginx 1.9.5",
}

# Directives interdites dans l'artefact livré, avec la raison.
FORBIDDEN_DIRECTIVES = {
    # Dépréciées / retirées : warning ou erreur sur les nginx récents.
    "ssl": "déprécié depuis nginx 1.15.0, retiré en 1.25.1 — utiliser « listen … ssl »",
    "http2_push": "déprécié depuis nginx 1.25.1",
    "http2_push_preload": "déprécié depuis nginx 1.25.1",
    "http2_max_field_size": "déprécié depuis nginx 1.19.7",
    "http2_max_header_size": "déprécié depuis nginx 1.19.7",
    "http2_max_requests": "déprécié depuis nginx 1.19.7",
    "http2_recv_timeout": "déprécié depuis nginx 1.19.7",
    # Trop RÉCENTE : « unknown directive » sur tout le socle supporté (< 1.25.1),
    # donc refus de démarrer sur Ubuntu 22.04 et 24.04. Elle est livrée
    # commentée, à décommenter par l'opérateur (docs/deployment.md §8).
    "http2": "n'existe qu'à partir de nginx 1.25.1 — casse le socle supporté "
             "(Ubuntu 22.04 → 1.18, Ubuntu 24.04 → 1.24)",
}


def parse_statements(text: str) -> list[tuple[str, tuple[str, ...]]]:
    """
    Statements nginx `(directive, arguments)`, commentaires retirés.

    Volontairement plus grossier que `doctor.parse_nginx_servers` : ici on veut
    voir CHAQUE occurrence, y compris les deux `listen` d'un même bloc, que
    l'aplatissement « dernière gagnante » de doctor masquerait.
    """
    without_comments = re.sub(r"#[^\n]*", "", text)
    statements: list[tuple[str, tuple[str, ...]]] = []
    for chunk in re.split(r"[;{}]", without_comments):
        words = chunk.split()
        if words:
            statements.append((words[0], tuple(words[1:])))
    return statements


def find_deprecated(text: str) -> list[str]:
    """Liste lisible des directives dépréciées/incompatibles trouvées."""
    problems: list[str] = []
    for directive, args in parse_statements(text):
        if directive in FORBIDDEN_DIRECTIVES:
            problems.append(f"{directive} — {FORBIDDEN_DIRECTIVES[directive]}")
        if directive == "listen":
            for arg in args:
                if arg in DEPRECATED_LISTEN_PARAMS:
                    problems.append(
                        f"listen … {arg} — {DEPRECATED_LISTEN_PARAMS[arg]}"
                    )
    return problems


@pytest.fixture(scope="module")
def conf_text() -> str:
    return SHIPPED_NGINX_CONF.read_text(encoding="utf-8")


# ── OPS-009 : aucune directive dépréciée ──────────────────────────────────────

def test_le_parsing_voit_reellement_la_configuration(conf_text):
    """
    Contrôle positif des tests ci-dessous.

    Sans lui, un `parse_statements` cassé rendrait « aucune directive dépréciée »
    trivialement vrai et le garde-fou deviendrait inerte sans jamais échouer.
    """
    statements = parse_statements(conf_text)
    assert len(statements) > 50, "parsing muet : la conf livrée n'est pas vide"

    directives = {name for name, _ in statements}
    assert {"listen", "server_name", "proxy_pass", "ssl_certificate"} <= directives

    listens = [args for name, args in statements if name == "listen"]
    # 443, [::]:443, 80, [::]:80 — les deux `listen` du bloc HTTPS doivent être
    # vus SÉPARÉMENT, c'est tout l'intérêt de ce parseur-ci.
    assert len(listens) == 4, listens


def test_la_conf_livree_n_emploie_aucune_directive_depreciee(conf_text):
    """OPS-009 : `nginx -t` ne doit produire aucun warning de dépréciation."""
    assert find_deprecated(conf_text) == []


def test_le_detecteur_de_depreciation_voit_le_defaut_dorigine(conf_text):
    """
    Contrôle positif du détecteur lui-même : on réintroduit le défaut OPS-009
    (`listen … ssl http2`) et il doit être signalé. Un détecteur qui ne détecte
    rien passerait le test précédent en silence.
    """
    regressed = conf_text.replace("listen 443 ssl;", "listen 443 ssl http2;")
    assert regressed != conf_text
    problems = find_deprecated(regressed)
    assert any("http2" in p for p in problems), problems

    # Et l'inverse : la directive moderne casse le socle < 1.25.1.
    too_recent = conf_text.replace("# http2 on;", "http2 on;")
    assert too_recent != conf_text
    assert any(p.startswith("http2 —") for p in find_deprecated(too_recent))


def test_http2_reste_disponible_en_une_ligne_a_decommenter(conf_text):
    """
    Le compromis OPS-009 sacrifie HTTP/2 par défaut : il doit rester activable
    sans réécrire la conf, et le mode d'emploi doit être documenté.
    """
    assert re.search(r"^\s*#\s*http2 on;\s*$", conf_text, re.MULTILINE)
    doc = DEPLOYMENT_DOC.read_text(encoding="utf-8")
    assert "http2 on;" in doc
    assert "1.25.1" in doc
