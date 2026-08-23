"""
Tests des réglages de type liste chargés depuis l'environnement (COR-014).

Défaut corrigé. pydantic-settings décode un champ *complexe* — donc `list[str]` —
comme du JSON directement dans la source d'environnement, AVANT l'exécution du
moindre validateur. Deux conséquences, sur le chemin de production (systemd
charge `EnvironmentFile`, donc de vraies variables d'environnement) :

- `ALLOWED_MODEL_DIRS=/models,/data/models` — la syntaxe documentée par
  `.env.example` et `docs/deployment.md` — faisait échouer le démarrage sur
  `SettingsError` ;
- `ALLOWED_MODEL_DIRS=` — la valeur **livrée** dans `.env.example` — échouait
  aussi. Copier `.env.example` vers `/etc/llm-gateway/env` produisait donc un
  service mort.

Le validateur `mode="before"` de `cors_allow_origins` existait déjà mais ne
s'exécutait jamais, pour la même raison.

Ces tests exercent les deux sources (variables d'environnement et fichier
dotenv), puisque l'une sert la production et l'autre le développement.
"""
from __future__ import annotations

import pytest

from config import Settings, split_list_setting

# Secrets valides, pour que seuls les champs de liste soient sous test.
_BASE_ENV = {
    "ADMIN_SECRET": "secret-admin-de-test-1234567890abcd",
    "INTERNAL_API_KEY": "secret-interne-de-test-1234567890abcd",
}


def _settings(monkeypatch, **env: str) -> Settings:
    """Construit un Settings depuis de vraies variables d'environnement."""
    for key, value in {**_BASE_ENV, **env}.items():
        monkeypatch.setenv(key, value)
    # `_env_file=None` : on teste la source d'environnement, pas un .env local
    # qui traînerait dans le répertoire de travail.
    return Settings(_env_file=None)


# ── Normalisation pure ────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("/models,/data/models", ["/models", "/data/models"]),
        ("/models, /data/models ", ["/models", "/data/models"]),
        ("/models", ["/models"]),
        ("", []),
        ("   ", []),
        (",,", []),
        ('["/a", "/b"]', ["/a", "/b"]),
        ('[]', []),
        (["/deja", "/une/liste"], ["/deja", "/une/liste"]),
        ([], []),
    ],
)
def test_split_list_setting(raw: object, expected: list[str]) -> None:
    assert split_list_setting(raw, "CHAMP") == expected


def test_split_list_setting_rejects_invalid_json() -> None:
    with pytest.raises(ValueError, match="tableau JSON invalide"):
        split_list_setting('["oups"', "CHAMP")


def test_split_list_setting_rejects_non_string_json_items() -> None:
    with pytest.raises(ValueError, match="liste de chaînes"):
        split_list_setting('[1, 2]', "CHAMP")


def test_split_list_setting_rejects_json_object() -> None:
    """
    Un objet JSON doit échouer, pas devenir un élément CSV.

    Sinon l'allowlist contiendrait une entrée qui ne correspond à aucun
    répertoire : le contrôle de sécurité serait inerte, silencieusement.
    """
    with pytest.raises(ValueError, match="pas un objet"):
        split_list_setting('{"a": 1}', "CHAMP")


# ── ALLOWED_MODEL_DIRS ────────────────────────────────────────────────────────

def test_allowed_model_dirs_csv_from_environment(monkeypatch) -> None:
    """La syntaxe documentée doit fonctionner : c'est la reproduction du défaut."""
    s = _settings(monkeypatch, ALLOWED_MODEL_DIRS="/models,/data/models")
    assert s.allowed_model_dirs == ["/models", "/data/models"]


def test_allowed_model_dirs_empty_value_from_environment(monkeypatch) -> None:
    """La valeur livrée telle quelle dans .env.example ne doit pas tuer le service."""
    s = _settings(monkeypatch, ALLOWED_MODEL_DIRS="")
    assert s.allowed_model_dirs == []


def test_allowed_model_dirs_absent_defaults_to_empty(monkeypatch) -> None:
    monkeypatch.delenv("ALLOWED_MODEL_DIRS", raising=False)
    s = _settings(monkeypatch)
    assert s.allowed_model_dirs == []


def test_allowed_model_dirs_json_array_from_environment(monkeypatch) -> None:
    """Le format JSON historiquement imposé reste accepté (rétro-compatibilité)."""
    s = _settings(monkeypatch, ALLOWED_MODEL_DIRS='["/models", "/data/models"]')
    assert s.allowed_model_dirs == ["/models", "/data/models"]


def test_allowed_model_dirs_is_a_list_not_a_string(monkeypatch) -> None:
    """
    Le type après validation doit rester une liste.

    `ModelRegistry` itère dessus pour construire son allowlist : une chaîne
    produirait une allowlist caractère par caractère, donc une allowlist inerte.
    """
    s = _settings(monkeypatch, ALLOWED_MODEL_DIRS="/models")
    assert isinstance(s.allowed_model_dirs, list)


# ── CORS_ALLOW_ORIGINS ────────────────────────────────────────────────────────

def test_cors_csv_from_environment(monkeypatch) -> None:
    s = _settings(monkeypatch, CORS_ALLOW_ORIGINS="https://a.fr,https://b.fr")
    assert s.cors_allow_origins == ["https://a.fr", "https://b.fr"]


def test_cors_single_value_from_environment(monkeypatch) -> None:
    """Exemple de production de docs/deployment.md : une seule origine."""
    s = _settings(monkeypatch, CORS_ALLOW_ORIGINS="https://app.univ-pau.fr")
    assert s.cors_allow_origins == ["https://app.univ-pau.fr"]


def test_cors_wildcard_default(monkeypatch) -> None:
    monkeypatch.delenv("CORS_ALLOW_ORIGINS", raising=False)
    s = _settings(monkeypatch)
    assert s.cors_allow_origins == ["*"]


def test_cors_empty_value_yields_no_origin(monkeypatch) -> None:
    """Vide = aucune origine autorisée, pas un joker implicite."""
    s = _settings(monkeypatch, CORS_ALLOW_ORIGINS="")
    assert s.cors_allow_origins == []


# ── Source dotenv ─────────────────────────────────────────────────────────────

def test_csv_also_works_from_dotenv_file(tmp_path, monkeypatch) -> None:
    """
    Le chemin développement (`.env`) doit se comporter comme la production.

    La même `SettingsError` frappait les deux sources ; les deux doivent être
    couvertes, sinon la régression peut revenir par une seule d'entre elles.
    """
    for key in ("ALLOWED_MODEL_DIRS", "CORS_ALLOW_ORIGINS"):
        monkeypatch.delenv(key, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"ADMIN_SECRET={_BASE_ENV['ADMIN_SECRET']}\n"
        f"INTERNAL_API_KEY={_BASE_ENV['INTERNAL_API_KEY']}\n"
        "ALLOWED_MODEL_DIRS=/models,/data/models\n"
        "CORS_ALLOW_ORIGINS=https://a.fr,https://b.fr\n",
        encoding="utf-8",
    )
    s = Settings(_env_file=env_file)
    assert s.allowed_model_dirs == ["/models", "/data/models"]
    assert s.cors_allow_origins == ["https://a.fr", "https://b.fr"]


def test_shipped_env_example_is_loadable(monkeypatch) -> None:
    """
    Le fichier d'exemple livré doit produire une configuration chargeable.

    C'est le cœur du défaut : un opérateur qui copie `.env.example` vers
    `/etc/llm-gateway/env` obtenait un service qui refusait de démarrer.
    """
    from pathlib import Path

    example = Path(__file__).resolve().parent.parent / ".env.example"
    assert example.is_file(), "gateway/.env.example est attendu dans le dépôt"

    for key in ("ALLOWED_MODEL_DIRS", "CORS_ALLOW_ORIGINS"):
        monkeypatch.delenv(key, raising=False)
    # Les secrets d'exemple sont des placeholders refusés par les validateurs :
    # on les fournit valides pour n'exercer que le décodage des listes.
    for key, value in _BASE_ENV.items():
        monkeypatch.setenv(key, value)

    s = Settings(_env_file=example)
    assert isinstance(s.allowed_model_dirs, list)
    assert isinstance(s.cors_allow_origins, list)
