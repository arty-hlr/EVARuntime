"""
Tests de `ALLOWED_MODELS` chargé depuis l'environnement (COR-014).

Défaut corrigé. pydantic-settings décode un champ *complexe* — donc `list[str]` —
comme du JSON directement dans la source d'environnement, AVANT l'exécution du
moindre validateur. Le validateur `split_models` existait mais ne s'exécutait
jamais.

Conséquence sur le chemin de production : `deploy/env.example` livre
`ALLOWED_MODELS=llama-3.1-8b-instruct,qwen-9b`, et l'unité systemd charge ce
fichier via `EnvironmentFile`. La gateway étudiante n'a pas de script
d'installation (OPS-005), donc l'opérateur copie ce fichier à la main comme le
README le lui demande — et obtenait un service qui refusait de démarrer sur
`SettingsError`.

Le type après validation doit rester une **liste** : `policy.py` teste
l'appartenance (`requested_model not in settings.allowed_models`). Sur une
chaîne, ce test deviendrait une correspondance de sous-chaîne, donc une faille
d'autorisation — un modèle nommé `qwen` serait accepté parce que `qwen-9b` est
autorisé.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from config import Settings, split_list_setting

# Secrets valides (≥ 32 caractères, non placeholder) : seuls les champs de liste
# sont sous test ici.
_BASE_ENV = {
    "UPSTREAM_API_KEY": "cle-upstream-de-test-1234567890abcdef",
    "AUDIT_HMAC_SECRET": "secret-hmac-audit-de-test-1234567890ab",
}


def _settings(monkeypatch, **env: str) -> Settings:
    for key, value in {**_BASE_ENV, **env}.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)


# ── Normalisation pure ────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("llama-3.1-8b-instruct,qwen-9b", ["llama-3.1-8b-instruct", "qwen-9b"]),
        ("llama-3.1-8b-instruct, qwen-9b ", ["llama-3.1-8b-instruct", "qwen-9b"]),
        ("qwen-9b", ["qwen-9b"]),
        ("", []),
        ('["a", "b"]', ["a", "b"]),
        (["deja", "liste"], ["deja", "liste"]),
    ],
)
def test_split_list_setting(raw: object, expected: list[str]) -> None:
    assert split_list_setting(raw, "ALLOWED_MODELS") == expected


def test_split_list_setting_rejects_invalid_json() -> None:
    with pytest.raises(ValueError, match="tableau JSON invalide"):
        split_list_setting('["oups"', "ALLOWED_MODELS")


def test_split_list_setting_rejects_json_object() -> None:
    with pytest.raises(ValueError, match="pas un objet"):
        split_list_setting('{"a": 1}', "ALLOWED_MODELS")


# ── Chargement depuis l'environnement ─────────────────────────────────────────

def test_allowed_models_csv_from_environment(monkeypatch) -> None:
    """Reproduction du défaut : la syntaxe livrée doit charger."""
    s = _settings(monkeypatch, ALLOWED_MODELS="llama-3.1-8b-instruct,qwen-9b")
    assert s.allowed_models == ["llama-3.1-8b-instruct", "qwen-9b"]


def test_allowed_models_default(monkeypatch) -> None:
    monkeypatch.delenv("ALLOWED_MODELS", raising=False)
    s = _settings(monkeypatch)
    assert s.allowed_models == ["llama-3.1-8b-instruct", "qwen-9b"]


def test_allowed_models_json_array(monkeypatch) -> None:
    s = _settings(monkeypatch, ALLOWED_MODELS='["qwen-9b"]')
    assert s.allowed_models == ["qwen-9b"]


def test_allowed_models_is_a_list_not_a_string(monkeypatch) -> None:
    """
    Garde d'autorisation : le type doit rester une liste.

    Sur une chaîne, l'appartenance de `policy.py` deviendrait une correspondance
    de sous-chaîne et autoriserait des modèles non déclarés.
    """
    s = _settings(monkeypatch, ALLOWED_MODELS="qwen-9b")
    assert isinstance(s.allowed_models, list)
    assert "qwen" not in s.allowed_models
    assert "qwen-9b" in s.allowed_models


def test_shipped_deploy_env_example_is_loadable(monkeypatch) -> None:
    """
    `deploy/env.example` doit produire une configuration chargeable.

    C'est le fichier que le README demande de copier vers
    `/etc/llm-gateway-student/env`, et il livre une valeur CSV.
    """
    example = Path(__file__).resolve().parent.parent / "deploy" / "env.example"
    assert example.is_file(), "gateway-student/deploy/env.example est attendu"

    monkeypatch.delenv("ALLOWED_MODELS", raising=False)
    for key, value in _BASE_ENV.items():
        monkeypatch.setenv(key, value)

    s = Settings(_env_file=example)
    assert isinstance(s.allowed_models, list)
    assert s.allowed_models, "l'exemple livré déclare au moins un modèle"
