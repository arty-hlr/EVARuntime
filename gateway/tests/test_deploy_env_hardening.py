"""
SEC-002 — les trois durcissements doivent exister dans l'environnement généré.

Pourquoi ce test existe
-----------------------
`install.sh` générait `/etc/llm-gateway/env` sans `ALLOWED_MODEL_DIRS`, sans
`CORS_ALLOW_ORIGINS` et sans `LLAMA_SERVER_MIN_BUILD`. Absents du fichier, ces
trois réglages n'existaient pas pour l'exploitant : rien ne les nommait, rien ne
disait quoi y mettre. L'allowlist était même **impossible à activer** avant
COR-014 — une valeur CSV faisait échouer le démarrage sur `SettingsError`.

Ce que ce test fait, et pourquoi ce n'est pas un `grep`
-------------------------------------------------------
Vérifier par `grep` que trois lignes existent ne prouve rien : c'est précisément
comme cela qu'on livre un durcissement qui empêche le service de démarrer. On
RENDU le fichier avec la vraie fonction bash de production
(`deploy_render_env_file`, sourcée de `gateway/deploy/env-template-lib.sh`), puis
on le CHARGE avec la vraie classe `Settings` — la même que celle qu'exécute le
service au démarrage, via le même `load_settings_from_env_file` que `doctor`.

Le piège que ce test verrouille
-------------------------------
`ModelRegistry` valide l'allowlist sur TOUTES les entrées du registre, activées
ou non. Le registre livré déclare des chemins hors de `/models` : écrire
naïvement `ALLOWED_MODEL_DIRS=/models` rendrait une installation neuve NON
DÉMARRABLE. Le test charge donc aussi le registre livré avec l'allowlist
générée, et exige que le couple fonctionne — un durcissement qui casse le
produit n'est pas un durcissement.

Aucun test ne touche l'hôte : ni systemd, ni GPU, ni root.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from config import Settings
from doctor import load_settings_from_env_file
from model_registry import ModelRegistry


REPO_ROOT = Path(__file__).resolve().parents[2]
GATEWAY_ROOT = REPO_ROOT / "gateway"
DEPLOY_DIR = GATEWAY_ROOT / "deploy"
ENV_LIB = DEPLOY_DIR / "env-template-lib.sh"
INSTALL_SH = DEPLOY_DIR / "install.sh"
UPDATE_SH = DEPLOY_DIR / "update.sh"
SHIPPED_REGISTRY = GATEWAY_ROOT / "models.yaml"

# Les trois durcissements de SEC-002. Volontairement écrits ici : ce sont les
# NOMS des réglages exigés par l'item, pas une liste dérivée d'un script.
HARDENING_KEYS = ("ALLOWED_MODEL_DIRS", "CORS_ALLOW_ORIGINS", "LLAMA_SERVER_MIN_BUILD")


def render_env(target_dir: Path, registry: Path = SHIPPED_REGISTRY) -> Path:
    """Produit le VRAI fichier d'environnement, par la vraie fonction bash."""
    target_dir.mkdir(parents=True, exist_ok=True)
    env_file = target_dir / "env"
    result = subprocess.run(
        [
            "bash", "-c",
            f'source "{ENV_LIB}"; deploy_render_env_file "$1" "$2" "$3" "$4" "$5" "$6" "$7" "$8"',
            "bash",
            str(env_file),
            str(target_dir / "lib"),        # data_dir
            str(target_dir / "log"),        # log_dir
            str(target_dir / "etc"),        # config_dir
            "/models",                      # models_dir
            str(registry),
            "llmgw-internal-cle-de-test-suffisamment-longue",
            "secret-admin-de-test-suffisamment-long",
        ],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    return env_file


def env_keys(env_file: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


# ── 1. Les trois clés existent, en clair, dans le fichier généré ──────────────

@pytest.mark.parametrize("key", HARDENING_KEYS)
def test_hardening_key_is_present_in_the_generated_file(key: str, tmp_path: Path) -> None:
    """
    Critère d'acceptation de SEC-002 : « visibles dans le fichier généré ».

    C'est le seul assert de forme du module ; tous les suivants prouvent que ces
    valeurs sont réellement exploitables.
    """
    values = env_keys(render_env(tmp_path))
    assert key in values, (
        f"{key} absent de l'environnement généré : le réglage n'existe pas pour "
        "l'exploitant, rien ne le nomme et rien ne dit quoi y mettre (SEC-002)."
    )


# ── 2. Le fichier généré CHARGE réellement dans la classe Settings ────────────

def test_generated_environment_loads_with_the_real_settings_class(tmp_path: Path) -> None:
    """
    Le service démarrerait-il avec ce fichier ?

    `load_settings_from_env_file` est le chemin qu'emprunte `doctor`, et il
    construit la MÊME `Settings` que le service. Un durcissement mal typé —
    l'histoire d'`ALLOWED_MODEL_DIRS` avant COR-014 — ferait échouer ici avec la
    même `SettingsError` qu'au démarrage.
    """
    config = load_settings_from_env_file(render_env(tmp_path))
    assert isinstance(config, Settings)


def test_allowlist_is_parsed_as_a_real_list(tmp_path: Path) -> None:
    """
    La valeur CSV posée est normalisée en liste, pas laissée en chaîne.

    C'est la régression COR-014 : annotée `list[str]`, la clé faisait échouer le
    démarrage sur la syntaxe CSV pourtant documentée.
    """
    config = load_settings_from_env_file(render_env(tmp_path))
    assert isinstance(config.allowed_model_dirs, list)
    assert config.allowed_model_dirs, (
        "L'allowlist est vide : elle n'impose alors AUCUNE restriction de "
        "répertoire, et le durcissement est inerte (SEC-002)."
    )
    assert "/models" in config.allowed_model_dirs


def test_cors_is_closed_by_default(tmp_path: Path) -> None:
    """
    CORS explicite : aucune origine navigateur par défaut.

    L'API est consommée par des clients serveur, que CORS ne concerne pas ; le
    dashboard admin est servi depuis la même origine. `*` autoriserait n'importe
    quelle page web à parler à la gateway.
    """
    config = load_settings_from_env_file(render_env(tmp_path))
    assert config.cors_allow_origins == [], (
        f"CORS ouvert par défaut : {config.cors_allow_origins!r}. Le défaut "
        "attendu est « aucune origine » (SEC-002)."
    )


def test_min_build_is_present_and_typed(tmp_path: Path) -> None:
    """Le plancher de version existe et se charge comme un entier."""
    config = load_settings_from_env_file(render_env(tmp_path))
    assert isinstance(config.llama_server_min_build, int)


def test_min_build_comment_tells_how_to_pin_it(tmp_path: Path) -> None:
    """
    À 0 le garde-fou est inerte : le fichier doit dire comment le lever.

    Une clé posée à une valeur neutre sans mode d'emploi ne durcit rien ; c'est
    le commentaire qui rend le réglage actionnable, et doctor qui le rappelle
    (code `min_build_not_enforced`).
    """
    text = render_env(tmp_path).read_text(encoding="utf-8")
    assert "llama-server --version" in text, (
        "Le fichier ne dit pas comment relever LLAMA_SERVER_MIN_BUILD."
    )
    assert "GHSA-8947-pfff-2f3c" in text, (
        "Le fichier ne dit pas contre quoi ce plancher protège."
    )


# ── 3. Le durcissement ne casse pas le produit ────────────────────────────────

def test_generated_allowlist_accepts_the_shipped_registry(tmp_path: Path) -> None:
    """
    LE piège de SEC-002, verrouillé.

    `ModelRegistry` valide l'allowlist sur toutes les entrées, activées ou non.
    Le registre livré déclare des chemins hors de /models : une allowlist écrite
    « /models » en dur rendrait l'installation neuve NON DÉMARRABLE. On charge
    donc le registre livré avec l'allowlist générée.
    """
    config = load_settings_from_env_file(render_env(tmp_path))
    registry = ModelRegistry(
        config_path=SHIPPED_REGISTRY,
        allowed_model_dirs=list(config.allowed_model_dirs),
    )
    assert registry.list_all(), "Le registre livré est vide : contrôle inerte."


def test_a_naive_allowlist_would_have_broken_the_install() -> None:
    """
    Contrôle positif du test précédent : le piège est bien réel.

    Sans cette contre-épreuve, le test ci-dessus resterait vert même si la
    validation d'allowlist cessait d'exister, et ne prouverait plus rien.
    """
    with pytest.raises(Exception) as excinfo:
        ModelRegistry(config_path=SHIPPED_REGISTRY, allowed_model_dirs=["/models"])
    assert "autorisé" in str(excinfo.value)


def test_allowlist_derivation_covers_every_declared_path(tmp_path: Path) -> None:
    """
    La dérivation lit le registre, elle ne devine pas.

    Registre fabriqué avec un répertoire inattendu : l'allowlist doit le couvrir,
    sinon la génération n'est vraie que pour le registre livré d'aujourd'hui.
    """
    registry = tmp_path / "custom.yaml"
    registry.write_text(
        "models:\n"
        '  - id: "a"\n'
        '    path: "/srv/gguf/a.gguf"\n'
        "    vram_gb: 1.0\n"
        "    enabled: false\n",
        encoding="utf-8",
    )
    config = load_settings_from_env_file(render_env(tmp_path / "out", registry))
    assert "/srv/gguf" in config.allowed_model_dirs, (
        f"Répertoire déclaré par le registre non couvert : "
        f"{config.allowed_model_dirs}. La génération casserait le démarrage."
    )
    assert "/models" in config.allowed_model_dirs, (
        "Le répertoire de modèles créé par l'installateur a disparu de l'allowlist."
    )


# ── 4. Câblage réel dans les scripts ──────────────────────────────────────────

def test_install_script_uses_the_shared_renderer() -> None:
    """Sans ce câblage, tout ce module testerait du code que personne n'exécute."""
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "env-template-lib.sh" in text, "install.sh ne source plus la bibliothèque."
    assert "deploy_render_env_file" in text, (
        "install.sh ne rend plus l'environnement par la fonction partagée : les "
        "tests ci-dessus n'exercent plus le fichier réellement généré."
    )
    assert 'cat > "$CONFIG_FILE"' not in text, (
        "Un second rendu en ligne subsiste dans install.sh : deux sources de "
        "vérité pour le même fichier, dont une non testée."
    )


def test_update_script_reports_missing_hardening() -> None:
    """
    `update.sh` ne régénère pas l'environnement : il doit au moins le SIGNALER.

    Sur un hôte installé avant SEC-002, les clés resteraient absentes
    indéfiniment et silencieusement. Elles ne sont pas ajoutées d'autorité —
    poser « CORS_ALLOW_ORIGINS= » pendant une mise à jour couperait un client
    navigateur existant.
    """
    text = UPDATE_SH.read_text(encoding="utf-8")
    assert "DEPLOY_HARDENING_KEYS" in text, (
        "update.sh ne contrôle pas la présence des durcissements SEC-002 : sur un "
        "hôte antérieur, ils resteraient absents sans que rien ne le dise."
    )
    assert "env-template-lib.sh" in text, "update.sh ne source plus la bibliothèque."


def test_hardening_key_list_is_shared_between_shell_and_tests() -> None:
    """
    Contrôle d'accord : la liste bash couvre bien les trois clés attendues.

    Deux listes qui divergent silencieusement, c'est un `update.sh` qui contrôle
    deux clés sur trois sans que personne ne le voie.
    """
    lib = ENV_LIB.read_text(encoding="utf-8")
    declared = lib.split("DEPLOY_HARDENING_KEYS=(", 1)[1].split(")", 1)[0].split()
    assert sorted(declared) == sorted(HARDENING_KEYS), (
        f"DEPLOY_HARDENING_KEYS={declared} ne couvre pas les trois durcissements "
        f"de SEC-002 {HARDENING_KEYS}."
    )
