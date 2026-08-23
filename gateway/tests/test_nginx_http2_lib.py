"""Rendu conditionnel de HTTP/2 dans la configuration nginx livrée (OPS-009).

La conf livrée est neutre : un `listen` nu, valide sur toute la plage de
versions supportées, mais sans HTTP/2. Ce sont les scripts de déploiement qui
écrivent la forme adaptée au nginx réellement installé. Ces tests exercent
`deploy/nginx-lib.sh` pour de vrai, avec un faux `nginx -v` en `PATH`, parce
que le défaut d'origine ne se voyait qu'à l'exécution sur l'hôte cible.

Rappel des versions, vérifié le 2026-07-30 :

    < 1.25.1   « http2 on; » n'existe pas → unknown directive, nginx ne démarre
               pas. Seule « listen … ssl http2 » active HTTP/2, sans warning.
    >= 1.25.1  « listen … ssl http2 » est déprécié → warning à chaque `nginx -t`
               et à chaque reload. « http2 on; » est la forme recommandée.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

DEPLOY_DIR = Path(__file__).resolve().parent.parent / "deploy"
NGINX_LIB = DEPLOY_DIR / "nginx-lib.sh"
NGINX_CONF = DEPLOY_DIR / "nginx.conf"


def _directives(contenu: str) -> str:
    """Lignes actives de la conf, commentaires retirés.

    Indispensable pour toute assertion d'absence : « ssl http2 » apparaît dans
    le commentaire d'en-tête qui explique justement pourquoi il ne faut pas
    l'employer. Chercher la chaîne brute rendrait le test faussement rouge.
    """
    lignes = [ligne for ligne in contenu.splitlines() if not ligne.lstrip().startswith("#")]
    return "\n".join(lignes)


def _fake_nginx(tmp_path: Path, version: str | None) -> Path:
    """Pose un faux `nginx` en PATH. `version=None` → aucun nginx installé."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    if version is not None:
        fake = bin_dir / "nginx"
        # `nginx -v` écrit sur STDERR : le faux doit se comporter pareil,
        # sinon le test validerait une lecture que la vraie commande ne permet pas.
        fake.write_text(
            "#!/usr/bin/env bash\n"
            f'echo "nginx version: nginx/{version} (Ubuntu)" >&2\n'
        )
        fake.chmod(0o755)
    return bin_dir


def _render(tmp_path: Path, version: str | None) -> tuple[str, str]:
    """Rend la conf via la lib et retourne (contenu, forme HTTP/2 annoncée)."""
    bin_dir = _fake_nginx(tmp_path, version)
    dst = tmp_path / "rendered.conf"
    marker = tmp_path / "form.txt"
    script = (
        f'set -euo pipefail\n'
        f'source "{NGINX_LIB}"\n'
        f'nginx_render_conf "{NGINX_CONF}" "{dst}"\n'
        f'printf "%s" "$NGINX_HTTP2_FORM" > "{marker}"\n'
    )
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        # PATH réduit au faux nginx + les outils de base (sed, sort, head).
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
    )
    assert result.returncode == 0, f"nginx_render_conf a échoué : {result.stderr}"
    return dst.read_text(), marker.read_text()


def test_la_conf_livree_est_neutre_et_lisible():
    """Contrôle positif : sans lui, tous les tests ci-dessous seraient vides."""
    contenu = NGINX_CONF.read_text()
    assert "listen 443 ssl;" in contenu
    assert "listen [::]:443 ssl;" in contenu
    assert "# http2 on;" in contenu
    # La conf livrée n'active HTTP/2 sous aucune forme : c'est ce qui la rend
    # valide de nginx 1.18 à 1.29.
    assert "ssl http2" not in _directives(contenu)


@pytest.mark.parametrize("version", ["1.18.0", "1.22.1", "1.24.0", "1.25.0"])
def test_nginx_ancien_recoit_la_forme_historique(tmp_path, version):
    """< 1.25.1 : « listen … ssl http2 », la seule qui active HTTP/2 sans casser."""
    contenu, forme = _render(tmp_path, version)
    assert "listen 443 ssl http2;" in contenu
    assert "listen [::]:443 ssl http2;" in contenu
    # La directive moderne doit rester commentée : décommentée, nginx refuserait
    # de démarrer sur ces versions.
    assert "# http2 on;" in contenu
    assert "http2 on;" not in _directives(contenu)
    assert forme == "listen … ssl http2"


@pytest.mark.parametrize("version", ["1.25.1", "1.26.2", "1.29.0"])
def test_nginx_recent_recoit_la_directive_moderne(tmp_path, version):
    """>= 1.25.1 : « http2 on; », sans le paramètre déprécié qui fait du bruit."""
    contenu, forme = _render(tmp_path, version)
    actives = _directives(contenu)
    assert "http2 on;" in actives
    assert "# http2 on;" not in contenu
    # Le paramètre déprécié ne doit apparaître dans aucune directive : c'est
    # exactement le défaut OPS-009 reproduit sur Debian 13.
    assert "ssl http2" not in actives
    assert forme == "http2 on;"


def test_sans_nginx_la_conf_est_copiee_telle_quelle(tmp_path):
    """Aucun nginx détectable : on ne devine pas, on livre la forme neutre."""
    contenu, forme = _render(tmp_path, None)
    assert contenu == NGINX_CONF.read_text()
    assert forme == "aucune (HTTP/1.1)"


def test_une_version_illisible_retombe_sur_la_forme_qui_demarre_partout(tmp_path):
    """Sur un doute, jamais la forme qui empêche nginx de démarrer.

    Une version non reconnue ne doit pas produire « http2 on; » : sur un nginx
    ancien, cela transformerait un avertissement cosmétique en service mort.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "nginx"
    fake.write_text('#!/usr/bin/env bash\necho "nginx version: inconnue" >&2\n')
    fake.chmod(0o755)
    dst = tmp_path / "rendered.conf"
    script = (
        f'set -euo pipefail\nsource "{NGINX_LIB}"\n'
        f'nginx_render_conf "{NGINX_CONF}" "{dst}"\n'
    )
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stderr
    assert "http2 on;" not in _directives(dst.read_text())


def test_les_deux_scripts_de_deploiement_utilisent_la_lib():
    """Un `cp` direct réintroduirait la perte de HTTP/2 sans rien casser."""
    trouves = 0
    for nom in ("install.sh", "update.sh"):
        script = (DEPLOY_DIR / nom).read_text()
        assert "nginx-lib.sh" in script, f"{nom} ne source pas nginx-lib.sh"
        assert "nginx_render_conf" in script, f"{nom} n'appelle pas nginx_render_conf"
        # Le `cp` direct de la conf ne doit plus exister : il court-circuiterait
        # le rendu conditionnel.
        assert "cp \"$SCRIPT_DIR/deploy/nginx.conf\"" not in script, (
            f"{nom} copie encore nginx.conf directement (OPS-009)"
        )
        trouves += 1
    assert trouves == 2, "contrôle positif : les deux scripts doivent être lus"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash requis")
def test_la_lib_a_une_syntaxe_valide():
    result = subprocess.run(["bash", "-n", str(NGINX_LIB)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
