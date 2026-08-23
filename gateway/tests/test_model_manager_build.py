"""
Tests de `model_manager._build_manager()` — sélection du backend selon CLUSTER_MODE.

Régression COR-015 : la ligne de log « Nœuds cluster configurés » lisait
`n.node_id` alors que `NodeConfig` expose `id`. Le `", ".join(...)` étant évalué
AVANT l'appel à `log.info`, l'`AttributeError` remontait quel que soit le niveau
de log — au chargement du module, donc avant tout démarrage de service. Le mode
cluster était mort-né et aucun test ne construisait le manager en mode cluster.

Ces tests couvrent les DEUX branches (local → LocalModelManager, cluster →
ClusterManager) et vérifient explicitement le contenu du message émis, pour que
le chemin de code fautif soit réellement exercé et non seulement traversé.
"""
from __future__ import annotations

import asyncio
import logging

import pytest
import yaml

import model_manager as mm
from cluster.cluster_manager import ClusterManager


# ── Helpers ──────────────────────────────────────────────────────────────────

def _write_models_yaml(tmp_path) -> object:
    """Registre minimal valide — le .gguf n'a pas besoin d'exister sur disque."""
    cfg = tmp_path / "models.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {"models": [{"id": "modele-test", "path": "/models/modele-test.gguf", "vram_gb": 4.0}]}
        ),
        encoding="utf-8",
    )
    return cfg


def _write_nodes_yaml(tmp_path, content: str | None = None) -> object:
    """nodes.yaml minimal — deux nœuds pour exercer aussi la jointure du log."""
    path = tmp_path / "nodes.yaml"
    path.write_text(
        content
        if content is not None
        else """
nodes:
  - id: dgx-spark-a
    base_url: https://dgx-a.internal.test:9443
  - id: dgx-spark-b
    base_url: https://dgx-b.internal.test:9443
""",
        encoding="utf-8",
    )
    return path


def _apply_settings(monkeypatch, tmp_path, *, cluster_mode: str, nodes_path=None) -> None:
    """Aligne `settings` sur un environnement de test hermétique."""
    monkeypatch.setattr(mm.settings, "cluster_mode", cluster_mode)
    monkeypatch.setattr(mm.settings, "models_config_path", _write_models_yaml(tmp_path))
    monkeypatch.setattr(mm.settings, "allowed_model_dirs", [])
    monkeypatch.setattr(mm.settings, "agent_secret", "s" * 48)
    if nodes_path is not None:
        monkeypatch.setattr(mm.settings, "cluster_nodes_path", nodes_path)


def _close_node_clients(manager: ClusterManager) -> None:
    """Ferme les httpx.AsyncClient créés par _build_manager (aucune requête émise)."""
    async def _close() -> None:
        for state in manager._nodes.values():
            await state.client.close()

    asyncio.run(_close())


# ── Mode cluster ─────────────────────────────────────────────────────────────

def test_build_manager_cluster_mode_returns_cluster_manager(monkeypatch, tmp_path, caplog):
    """
    COR-015 : construire le manager en mode cluster avec un nodes.yaml minimal.

    Sur le code d'avant le correctif (`n.node_id`), ce test échoue avec
    AttributeError: 'NodeConfig' object has no attribute 'node_id'.
    """
    nodes_path = _write_nodes_yaml(tmp_path)
    _apply_settings(monkeypatch, tmp_path, cluster_mode="cluster", nodes_path=nodes_path)

    with caplog.at_level(logging.INFO, logger=mm.log.name):
        manager = mm._build_manager()

    try:
        assert isinstance(manager, ClusterManager)
        assert not isinstance(manager, mm.LocalModelManager)
        assert sorted(manager._nodes) == ["dgx-spark-a", "dgx-spark-b"]

        # La ligne de log fautive doit avoir été FORMATÉE, pas seulement atteinte :
        # on assère sur le rendu final `<id>(<base_url>)` de chaque nœud.
        messages = [rec.getMessage() for rec in caplog.records if rec.name == mm.log.name]
        node_lines = [m for m in messages if "Nœuds cluster configurés" in m]
        assert node_lines, f"ligne de log des nœuds absente — messages vus : {messages}"
        assert "dgx-spark-a(https://dgx-a.internal.test:9443)" in node_lines[0]
        assert "dgx-spark-b(https://dgx-b.internal.test:9443)" in node_lines[0]
    finally:
        _close_node_clients(manager)


def test_build_manager_cluster_mode_propagates_node_config_to_clients(
    monkeypatch, tmp_path
):
    """Les clients distants reçoivent bien l'id et l'URL lus dans nodes.yaml."""
    nodes_path = _write_nodes_yaml(
        tmp_path,
        """
nodes:
  - id: node-unique
    base_url: https://node-unique.internal.test:9443/
""",
    )
    _apply_settings(monkeypatch, tmp_path, cluster_mode="cluster", nodes_path=nodes_path)

    manager = mm._build_manager()
    try:
        client = manager._nodes["node-unique"].client
        assert client.node_id == "node-unique"
        # Slash final normalisé par load_nodes_config.
        assert client.base_url == "https://node-unique.internal.test:9443"
    finally:
        _close_node_clients(manager)


def test_build_manager_cluster_mode_refuses_placeholder_agent_secret(
    monkeypatch, tmp_path
):
    """Le garde-fou AGENT_SECRET reste actif avant toute lecture de nodes.yaml."""
    nodes_path = _write_nodes_yaml(tmp_path)
    _apply_settings(monkeypatch, tmp_path, cluster_mode="cluster", nodes_path=nodes_path)
    monkeypatch.setattr(mm.settings, "agent_secret", "CHANGE_ME_AGENT_SECRET")

    with pytest.raises(RuntimeError, match="AGENT_SECRET"):
        mm._build_manager()


def test_build_manager_cluster_mode_requires_nodes_file(monkeypatch, tmp_path):
    """Un CLUSTER_NODES_PATH inexistant échoue tôt, avec un message actionnable."""
    _apply_settings(
        monkeypatch,
        tmp_path,
        cluster_mode="cluster",
        nodes_path=tmp_path / "absent.yaml",
    )

    with pytest.raises(FileNotFoundError, match="absent.yaml"):
        mm._build_manager()


# ── Mode local (branche symétrique) ──────────────────────────────────────────

def test_build_manager_local_mode_returns_local_manager(monkeypatch, tmp_path, caplog):
    """Le mode par défaut reste le manager mono-nœud, sans lire nodes.yaml."""
    _apply_settings(
        monkeypatch,
        tmp_path,
        cluster_mode="local",
        nodes_path=tmp_path / "jamais-lu.yaml",
    )

    with caplog.at_level(logging.INFO, logger=mm.log.name):
        manager = mm._build_manager()

    assert isinstance(manager, mm.LocalModelManager)
    assert not isinstance(manager, ClusterManager)
    assert manager.registry.get("modele-test") is not None

    messages = [rec.getMessage() for rec in caplog.records if rec.name == mm.log.name]
    assert any("CLUSTER_MODE=local" in m for m in messages), messages
    assert not any("Nœuds cluster configurés" in m for m in messages), messages
