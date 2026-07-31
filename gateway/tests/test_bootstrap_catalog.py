"""
Tests d'AUT-005 — catalogue de modèles approuvés.

Ce que ces tests verrouillent
-----------------------------
- le CATALOGUE LIVRÉ lui-même : deux modèles, licences réellement permissives,
  non gated, révisions et SHA-256 épinglés. C'est le test qui échouera si
  quelqu'un ajoute demain une entrée Llama en la faisant passer pour Apache ;
- la validation STRICTE : champ inconnu, licence absente ou non identifiée,
  `id` non conforme, révision de forme invalide, SHA-256 approximatif — chacun
  est un rejet, jamais un avertissement ;
- le FAIL-CLOSED sur l'intégrité : une entrée non épinglée est listée mais
  produit un constat bloquant, sort de `plannable_entries()`, et
  `download_fileset()` refuse ;
- l'ENSEMBLE INDIVISIBLE de §8 : shards incomplets, séries mélangées, `mmproj`
  manquant, immuabilité — aucun chemin de code ne rend un sous-ensemble ;
- la projection vers `schema` : sérialisable JSON, sans secret, structurellement
  valide comme section de plan.

Aucun test ne touche au réseau ni au disque hors `tmp_path`.
"""
from __future__ import annotations

import copy
import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from bootstrap import catalog, schema


LIVE_CATALOG = Path(catalog.__file__).with_name("catalog.yaml")


# ── Fabrication d'un catalogue minimal valide ────────────────────────────────

def _valid_document() -> dict[str, Any]:
    return {
        "catalog_version": 1,
        "downloader": {
            "name": "huggingface_hub",
            "license_id": "apache-2.0",
            "license_url": "https://example.invalid/license",
            "notes": "client officiel",
        },
        "models": [{
            "id": "fixture-1b-q4",
            "family": "fixture",
            "display_name": "Fixture 1B",
            "description": "Entrée de test.",
            "use_cases": ["smoke_test"],
            "source": {
                "provider": "huggingface",
                "repo_id": "owner/fixture-GGUF",
                "repo_url": "https://example.invalid/fixture",
                "revision": "a" * 40,
                "revision_recorded_on": "2026-07-31",
                "files": [{
                    "name": "fixture-1b-q4_k_m.gguf",
                    "role": "weights",
                    "sha256": "b" * 64,
                    "size_bytes": 1024,
                }],
            },
            "license": {
                "base_model": {"id": "apache-2.0", "url": "https://example.invalid/apache",
                               "repo_id": "owner/fixture"},
                "fine_tune": {"id": "apache-2.0", "url": "https://example.invalid/apache",
                              "repo_id": "owner/fixture-GGUF"},
                "usage_terms": None,
                "gated": False,
                "redistribution_allowed": True,
                "operator_acceptance_required": True,
                "notes": None,
            },
            "runtime": {
                "min_llama_build": 0,
                "capabilities": ["text_generation", "streaming"],
                "requires_mmproj": False,
                "defaults": {
                    "ctx_size": 4096, "parallel": 1,
                    "cache_type_k": "f16", "cache_type_v": "f16",
                },
            },
            "resources": {
                "disk_gb": 0.5, "initial_vram_gb": 1.5,
                "initial_ram_gb": 1.5, "estimation_basis": "gguf_header",
            },
        }],
    }


def _write(tmp_path: Path, document: dict[str, Any], name: str = "catalog.yaml") -> Path:
    target = tmp_path / name
    target.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
    return target


def _load(tmp_path: Path, mutate: Any = None) -> catalog.Catalog:
    document = _valid_document()
    if mutate is not None:
        mutate(document)
    return catalog.load_catalog(_write(tmp_path, document))


def _entry(document: dict[str, Any]) -> dict[str, Any]:
    return document["models"][0]


# ── Le catalogue réellement livré ────────────────────────────────────────────

def test_le_catalogue_livre_charge_et_contient_deux_modeles() -> None:
    """Jalon M1 (§13) : « catalogue initial de deux petits modèles permissifs »."""
    loaded = catalog.load_catalog()
    assert loaded.version == catalog.CATALOG_VERSION
    assert len(loaded.entries) == 2
    assert loaded.source_path == str(LIVE_CATALOG)


def test_le_catalogue_livre_nutilise_que_des_licences_reellement_permissives() -> None:
    """
    La « Llama 3.2 Community License » n'est PAS permissive : elle impose des
    conditions d'usage et un seuil d'utilisateurs. Ce test existe pour qu'elle
    ne puisse pas entrer ici déguisée en licence ouverte.
    """
    for entry in catalog.load_catalog().entries:
        assert entry.license.base_model.id in ("apache-2.0", "mit"), entry.id
        assert entry.license.fine_tune.id in ("apache-2.0", "mit"), entry.id
        assert entry.license.permissive is True, entry.id
        assert entry.license.gated is False, entry.id
        assert entry.license.redistribution_allowed is True, entry.id

    # Contrôle négatif de la table : les licences communautaires y figurent bien,
    # et elles y figurent comme NON permissives.
    assert catalog._LICENSES["llama3.2"] is False
    assert catalog._LICENSES["apache-2.0"] is True


def test_le_catalogue_livre_est_entierement_epingle() -> None:
    """
    Révision de 40 hexa et SHA-256 de 64 hexa pour chaque fichier.

    Ces valeurs ont été relevées sur l'API publique Hugging Face et recoupées
    avec l'en-tête `X-Linked-Etag` du même dépôt à la même révision. Elles ne
    sont pas inventées, et ce test refuserait une valeur de remplissage.
    """
    for entry in catalog.load_catalog().entries:
        assert entry.verification == "pinned", entry.id
        assert entry.revision is not None and catalog._REVISION_RE.match(entry.revision)
        assert entry.blocking_findings == (), entry.id
        for item in entry.files:
            assert item.sha256 is not None and catalog._SHA256_RE.match(item.sha256)
            assert item.size_bytes and item.size_bytes > 0


def test_le_catalogue_livre_reste_petit_et_realiste() -> None:
    """Un modèle d'amorçage doit produire un premier token sur un hôte modeste."""
    for entry in catalog.load_catalog().entries:
        assert entry.resources.disk_gb < 2.0, entry.id
        assert entry.resources.initial_vram_gb < 4.0, entry.id
        assert entry.files.total_bytes is not None
        assert entry.files.total_bytes < 2 * 1024 ** 3, entry.id


def test_le_catalogue_livre_distingue_la_licence_du_telechargeur() -> None:
    """§8 : la licence du logiciel de téléchargement est une donnée distincte."""
    loaded = catalog.load_catalog()
    assert loaded.downloader.name == "huggingface_hub"
    assert loaded.downloader.license_id == "apache-2.0"
    # Elle ne se confond pas avec celle des modèles : c'est un objet séparé.
    assert not hasattr(loaded.entries[0].license, "downloader")


def test_le_catalogue_livre_est_distinct_du_registre_operationnel() -> None:
    """
    §8 insiste : catalogue ≠ registre. Le catalogue ne porte aucun chemin local.

    Contrôle positif : il porte bien, en revanche, un `repo_id` et une révision.
    """
    document = yaml.safe_load(LIVE_CATALOG.read_text(encoding="utf-8"))
    rendered = json.dumps(document)

    assert "repo_id" in rendered and "revision" in rendered  # contrôle positif
    for registry_only in ('"path"', '"enabled"', '"vram_gb"', '"llama_params"', '"n_gpu_layers"'):
        assert registry_only not in rendered


def test_le_catalogue_livre_ne_produit_ni_bloqueur_ni_avertissement() -> None:
    section = catalog.to_plan_section(catalog.load_catalog())
    assert section.status == "ok"
    assert section.findings == ()


# ── Chargement : cas nominal ─────────────────────────────────────────────────

def test_chargement_dun_catalogue_minimal(tmp_path: Path) -> None:
    loaded = _load(tmp_path)
    entry = loaded.entries[0]
    assert entry.id == "fixture-1b-q4"
    assert entry.plannable is True
    assert len(entry.download_fileset()) == 1


def test_yaml_safe_load_obligatoire_les_tags_python_sont_refuses(tmp_path: Path) -> None:
    """Un catalogue est une configuration, pas un programme (règle du dépôt)."""
    hostile = tmp_path / "hostile.yaml"
    hostile.write_text(
        "catalog_version: 1\nmodels: !!python/object/apply:os.system ['echo pwned']\n",
        encoding="utf-8",
    )
    with pytest.raises(catalog.CatalogError, match="YAML invalide"):
        catalog.load_catalog(hostile)


def test_catalogue_absent_ou_illisible(tmp_path: Path) -> None:
    with pytest.raises(catalog.CatalogError, match="illisible"):
        catalog.load_catalog(tmp_path / "inexistant.yaml")


def test_version_de_catalogue_non_supportee(tmp_path: Path) -> None:
    with pytest.raises(catalog.CatalogError, match="non supportée"):
        _load(tmp_path, lambda d: d.__setitem__("catalog_version", 2))


def test_catalogue_vide_refuse(tmp_path: Path) -> None:
    with pytest.raises(catalog.CatalogError, match="liste non vide"):
        _load(tmp_path, lambda d: d.__setitem__("models", []))


def test_identifiants_en_double_refuses(tmp_path: Path) -> None:
    with pytest.raises(catalog.CatalogError, match="en double"):
        _load(tmp_path, lambda d: d["models"].append(copy.deepcopy(_entry(d))))


# ── Validation stricte ───────────────────────────────────────────────────────

@pytest.mark.parametrize("where, key", [
    ("root", "catalog_version_typo"),
    ("entry", "vram_gb"),
    ("source", "branch"),
    ("license", "licence"),
    ("runtime", "n_gpu_layers"),
    ("resources", "measured_vram_gb"),
    ("file", "checksum"),
    ("defaults", "flash_attn"),
])
def test_champ_inconnu_est_un_rejet_pas_un_avertissement(tmp_path: Path, where: str, key: str) -> None:
    """
    Un champ inconnu est soit une faute de frappe qui rend une contrainte
    inopérante, soit un catalogue écrit pour une autre version. Les deux
    doivent s'arrêter au chargement.
    """
    def mutate(document: dict[str, Any]) -> None:
        entry = _entry(document)
        target = {
            "root": document,
            "entry": entry,
            "source": entry["source"],
            "license": entry["license"],
            "runtime": entry["runtime"],
            "resources": entry["resources"],
            "file": entry["source"]["files"][0],
            "defaults": entry["runtime"]["defaults"],
        }[where]
        target[key] = "valeur anodine"

    with pytest.raises(catalog.CatalogError, match="champ.*inconnu"):
        _load(tmp_path, mutate)


def test_le_catalogue_valide_de_reference_passe(tmp_path: Path) -> None:
    """Contrôle positif du test précédent : sans champ ajouté, le document passe."""
    assert _load(tmp_path).entries[0].id == "fixture-1b-q4"


@pytest.mark.parametrize("bad_id", [
    "", "A-Majuscule", "avec/slash", "../evasion", "-tiret-initial",
    "tiret-final-", "x", "trop" + "-long" * 20,
])
def test_id_non_conforme_refuse(tmp_path: Path, bad_id: str) -> None:
    with pytest.raises(catalog.CatalogError):
        _load(tmp_path, lambda d: _entry(d).__setitem__("id", bad_id))


def test_licence_absente_refusee(tmp_path: Path) -> None:
    """§8 : « Un modèle sans licence identifiable doit être refusé par défaut. »"""
    with pytest.raises(catalog.CatalogError, match="doit être un objet"):
        _load(tmp_path, lambda d: _entry(d)["license"].__setitem__("base_model", None))


def test_licence_sans_identifiant_refusee(tmp_path: Path) -> None:
    with pytest.raises(catalog.CatalogError, match="chaîne non vide"):
        _load(tmp_path, lambda d: _entry(d)["license"]["base_model"].__setitem__("id", ""))


def test_licence_non_identifiee_refusee(tmp_path: Path) -> None:
    """Aucun repli sur « inconnue » : on ne devine pas une licence."""
    with pytest.raises(catalog.CatalogError, match="non identifiée"):
        _load(tmp_path, lambda d: _entry(d)["license"]["base_model"].__setitem__(
            "id", "licence-maison-2027"))


def test_licence_du_fine_tune_verifiee_aussi(tmp_path: Path) -> None:
    """Les deux étages comptent : une base permissive ne blanchit pas un fine-tune."""
    with pytest.raises(catalog.CatalogError, match="non identifiée"):
        _load(tmp_path, lambda d: _entry(d)["license"]["fine_tune"].__setitem__(
            "id", "licence-maison-2027"))


def test_gated_doit_etre_un_booleen_explicite(tmp_path: Path) -> None:
    """Une absence serait lue comme « non gated » — trop permissif pour être toléré."""
    with pytest.raises(catalog.CatalogError, match="booléen explicite"):
        _load(tmp_path, lambda d: _entry(d)["license"].pop("gated"))


@pytest.mark.parametrize("bad_revision", ["main", "v1.0", "A" * 40, "a" * 39, "a" * 41])
def test_revision_de_forme_invalide_refusee(tmp_path: Path, bad_revision: str) -> None:
    """Une branche n'est pas une révision : elle bouge sous les pieds."""
    with pytest.raises(catalog.CatalogError, match="revision non conforme"):
        _load(tmp_path, lambda d: _entry(d)["source"].__setitem__("revision", bad_revision))


@pytest.mark.parametrize("bad_sha", ["deadbeef", "Z" * 64, "b" * 63, "B" * 64])
def test_sha256_de_forme_invalide_refuse(tmp_path: Path, bad_sha: str) -> None:
    with pytest.raises(catalog.CatalogError, match="sha256 non conforme"):
        _load(tmp_path, lambda d: _entry(d)["source"]["files"][0].__setitem__("sha256", bad_sha))


def test_provider_inconnu_refuse(tmp_path: Path) -> None:
    with pytest.raises(catalog.CatalogError, match="provider inconnu"):
        _load(tmp_path, lambda d: _entry(d)["source"].__setitem__("provider", "ftp-du-labo"))


def test_role_de_fichier_inconnu_refuse(tmp_path: Path) -> None:
    with pytest.raises(catalog.CatalogError, match="role inconnu"):
        _load(tmp_path, lambda d: _entry(d)["source"]["files"][0].__setitem__("role", "lora"))


def test_nom_de_fichier_avec_chemin_refuse(tmp_path: Path) -> None:
    with pytest.raises(catalog.CatalogError, match="simple nom de fichier"):
        _load(tmp_path, lambda d: _entry(d)["source"]["files"][0].__setitem__(
            "name", "../../etc/passwd"))


def test_capacite_inconnue_refusee(tmp_path: Path) -> None:
    with pytest.raises(catalog.CatalogError, match="capabilities\\[0\\] inconnu"):
        _load(tmp_path, lambda d: _entry(d)["runtime"].__setitem__("capabilities", ["telepathie"]))


def test_cache_type_inconnu_refuse(tmp_path: Path) -> None:
    with pytest.raises(catalog.CatalogError, match="cache_type_k inconnu"):
        _load(tmp_path, lambda d: _entry(d)["runtime"]["defaults"].__setitem__(
            "cache_type_k", "q3_0"))


def test_ctx_size_trop_petit_refuse(tmp_path: Path) -> None:
    with pytest.raises(catalog.CatalogError, match="ctx_size"):
        _load(tmp_path, lambda d: _entry(d)["runtime"]["defaults"].__setitem__("ctx_size", 128))


def test_ressource_negative_refusee(tmp_path: Path) -> None:
    with pytest.raises(catalog.CatalogError, match="disk_gb"):
        _load(tmp_path, lambda d: _entry(d)["resources"].__setitem__("disk_gb", -1))


# ── Fail-closed sur l'intégrité ──────────────────────────────────────────────

def test_entree_sans_sha256_est_listee_mais_bloquee(tmp_path: Path) -> None:
    """
    `sha256: null` est la SEULE conduite acceptable quand on n'a pas la vraie
    valeur — et elle doit coûter le droit de planifier le téléchargement.
    """
    loaded = _load(tmp_path, lambda d: _entry(d)["source"]["files"][0].__setitem__("sha256", None))
    entry = loaded.entries[0]

    assert entry.verification == "pending"
    assert entry.plannable is False
    assert loaded.plannable_entries() == ()
    # Listée, pas escamotée : l'opérateur doit pouvoir la voir pour la corriger.
    assert loaded.get("fixture-1b-q4") is entry

    codes = [f.code for f in entry.blocking_findings]
    assert codes == ["catalogue_entree_non_epinglee"]
    assert entry.blocking_findings[0].level == "fail"


def test_entree_sans_revision_est_bloquee(tmp_path: Path) -> None:
    loaded = _load(tmp_path, lambda d: _entry(d)["source"].__setitem__("revision", None))
    entry = loaded.entries[0]
    assert entry.verification == "pending"
    assert entry.plannable is False
    assert "révision" in entry.blocking_findings[0].message


def test_le_message_de_blocage_dit_comment_epingler(tmp_path: Path) -> None:
    """Un bloqueur qui n'explique pas la sortie transforme l'opérateur en devin."""
    loaded = _load(tmp_path, lambda d: _entry(d)["source"].__setitem__("revision", None))
    message = loaded.entries[0].blocking_findings[0].message

    assert "huggingface.co/api/models/owner/fixture-GGUF" in message
    assert "lfs.sha256" in message
    assert "N'inventez" in message


def test_telechargement_dune_entree_non_epinglee_refuse(tmp_path: Path) -> None:
    """Fail-closed : le refus est dans le code, pas seulement dans la documentation."""
    loaded = _load(tmp_path, lambda d: _entry(d)["source"].__setitem__("revision", None))
    with pytest.raises(catalog.CatalogError, match="n'est pas planifiable"):
        loaded.entries[0].download_fileset()


def test_entree_gated_bloquee(tmp_path: Path) -> None:
    loaded = _load(tmp_path, lambda d: _entry(d)["license"].__setitem__("gated", True))
    entry = loaded.entries[0]
    assert entry.plannable is False
    assert "catalogue_modele_gated" in [f.code for f in entry.blocking_findings]
    with pytest.raises(catalog.CatalogError, match="n'est pas planifiable"):
        entry.download_fileset()


def test_licence_non_permissive_est_un_avertissement_pas_un_blocage(tmp_path: Path) -> None:
    """
    Une licence identifiée mais restrictive reste téléchargeable : c'est à
    l'organisation de trancher, pas au planificateur. Elle doit en revanche
    être signalée sans ambiguïté.
    """
    loaded = _load(tmp_path, lambda d: _entry(d)["license"]["fine_tune"].__setitem__(
        "id", "llama3.2"))
    entry = loaded.entries[0]

    assert entry.license.permissive is False
    assert entry.plannable is True
    assert [f.code for f in entry.advisory_findings] == ["catalogue_licence_non_permissive"]
    assert entry.advisory_findings[0].level == "warn"


def test_redistribution_interdite_signalee(tmp_path: Path) -> None:
    loaded = _load(tmp_path, lambda d: _entry(d)["license"].__setitem__(
        "redistribution_allowed", False))
    codes = [f.code for f in loaded.entries[0].advisory_findings]
    assert "catalogue_redistribution_interdite" in codes


def test_une_entree_saine_ne_produit_aucun_constat(tmp_path: Path) -> None:
    """Contrôle positif des tests de constats : sans défaut, la liste est vide."""
    assert _load(tmp_path).entries[0].findings == ()


# ── Ensemble indivisible (§8) ────────────────────────────────────────────────

def _shard_files(count: int, total: int = 3) -> list[dict[str, Any]]:
    return [{
        "name": f"fixture-{index:05d}-of-{total:05d}.gguf",
        "role": "weights_shard",
        "sha256": f"{index:x}" * 64,
        "size_bytes": 1024,
    } for index in range(1, count + 1)]


def test_serie_de_shards_complete_acceptee(tmp_path: Path) -> None:
    loaded = _load(tmp_path, lambda d: _entry(d)["source"].__setitem__("files", _shard_files(3)))
    assert len(loaded.entries[0].download_fileset()) == 3


def test_serie_de_shards_incomplete_refusee(tmp_path: Path) -> None:
    """
    Deux shards sur trois annoncés : le nom porte le total, on le confronte.

    C'est le cœur de « ensemble indivisible » : sans ce contrôle, un catalogue
    amputé produirait un téléchargement partiel qui ne chargerait jamais.
    """
    with pytest.raises(catalog.CatalogError, match="ensemble de shards incomplet"):
        _load(tmp_path, lambda d: _entry(d)["source"].__setitem__("files", _shard_files(2)))


def test_shards_de_series_differentes_refuses(tmp_path: Path) -> None:
    def mutate(document: dict[str, Any]) -> None:
        files = _shard_files(2, total=2)
        files[1]["name"] = "autre-00002-of-00002.gguf"
        _entry(document)["source"]["files"] = files

    with pytest.raises(catalog.CatalogError, match="séries différentes"):
        _load(tmp_path, mutate)


def test_shards_annoncant_des_totaux_differents_refuses(tmp_path: Path) -> None:
    def mutate(document: dict[str, Any]) -> None:
        files = _shard_files(2, total=2)
        files[1]["name"] = "fixture-00002-of-00009.gguf"
        _entry(document)["source"]["files"] = files

    with pytest.raises(catalog.CatalogError, match="totaux différents"):
        _load(tmp_path, mutate)


def test_shard_au_nom_non_conforme_refuse(tmp_path: Path) -> None:
    def mutate(document: dict[str, Any]) -> None:
        files = _shard_files(1, total=1)
        files[0]["name"] = "fixture-part1.gguf"
        _entry(document)["source"]["files"] = files

    with pytest.raises(catalog.CatalogError, match="nom non conforme"):
        _load(tmp_path, mutate)


def test_shards_et_monolithe_ne_coexistent_pas(tmp_path: Path) -> None:
    def mutate(document: dict[str, Any]) -> None:
        entry = _entry(document)
        entry["source"]["files"] = entry["source"]["files"] + _shard_files(1, total=1)

    with pytest.raises(catalog.CatalogError, match="ne peut pas coexister"):
        _load(tmp_path, mutate)


def test_mmproj_requis_mais_absent_refuse(tmp_path: Path) -> None:
    """Poids et projecteur multimodal forment un ensemble indivisible (§8)."""
    with pytest.raises(catalog.CatalogError, match="requires_mmproj"):
        _load(tmp_path, lambda d: _entry(d)["runtime"].__setitem__("requires_mmproj", True))


def test_mmproj_requis_et_present_accepte(tmp_path: Path) -> None:
    """Contrôle positif : la contrainte n'est pas un refus permanent."""
    def mutate(document: dict[str, Any]) -> None:
        entry = _entry(document)
        entry["runtime"]["requires_mmproj"] = True
        entry["runtime"]["capabilities"] = ["text_generation", "vision"]
        entry["source"]["files"].append({
            "name": "mmproj-fixture-f16.gguf", "role": "mmproj",
            "sha256": "c" * 64, "size_bytes": 512,
        })

    fileset = _load(tmp_path, mutate).entries[0].download_fileset()
    assert {f.role for f in fileset} == {"weights", "mmproj"}


def test_mmproj_seul_refuse(tmp_path: Path) -> None:
    def mutate(document: dict[str, Any]) -> None:
        entry = _entry(document)
        entry["runtime"]["requires_mmproj"] = True
        entry["source"]["files"] = [{
            "name": "mmproj-fixture-f16.gguf", "role": "mmproj",
            "sha256": "c" * 64, "size_bytes": 512,
        }]

    with pytest.raises(catalog.CatalogError, match="aucun fichier de poids"):
        _load(tmp_path, mutate)


def test_fichier_declare_deux_fois_refuse(tmp_path: Path) -> None:
    def mutate(document: dict[str, Any]) -> None:
        files = _entry(document)["source"]["files"]
        files.append(copy.deepcopy(files[0]))

    with pytest.raises(catalog.CatalogError, match="deux fois"):
        _load(tmp_path, mutate)


def test_lensemble_de_fichiers_est_immuable_et_total(tmp_path: Path) -> None:
    """
    Aucun chemin de code ne permet de ne retenir qu'une partie de l'ensemble.

    Trois propriétés indépendantes : la structure est gelée, le conteneur est un
    tuple, et le seul accesseur orienté téléchargement rend TOUT l'ensemble sans
    accepter le moindre filtre.
    """
    def mutate(document: dict[str, Any]) -> None:
        _entry(document)["source"]["files"] = _shard_files(3)

    entry = _load(tmp_path, mutate).entries[0]
    fileset = entry.download_fileset()

    assert isinstance(fileset.files, tuple)
    with pytest.raises(dataclasses.FrozenInstanceError):
        fileset.files = ()  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.files = catalog.FileSet(files=fileset.files)  # type: ignore[misc]

    # `download_fileset()` n'accepte aucun argument : pas de filtre possible.
    with pytest.raises(TypeError):
        entry.download_fileset("weights")  # type: ignore[call-arg]

    assert len(fileset) == 3
    assert [f.name for f in fileset] == [f.name for f in entry.files]


def test_un_fileset_partiel_ne_peut_pas_etre_construit_a_la_main() -> None:
    """Même en contournant le YAML, l'invariant tient : il est dans `FileSet`."""
    complete = tuple(
        catalog.CatalogFile(name=f["name"], role=f["role"], sha256=f["sha256"])
        for f in _shard_files(3)
    )
    assert len(catalog.FileSet(files=complete)) == 3  # contrôle positif
    with pytest.raises(catalog.CatalogError, match="incomplet"):
        catalog.FileSet(files=complete[:2])


def test_fileset_vide_refuse() -> None:
    with pytest.raises(catalog.CatalogError, match="vide"):
        catalog.FileSet(files=())


# ── Projection vers le plan ──────────────────────────────────────────────────

def test_section_conforme_au_contrat_de_schema(tmp_path: Path) -> None:
    section = catalog.to_plan_section(_load(tmp_path))
    assert section.name == schema.SECTION_CATALOG == catalog.SECTION_NAME
    assert section.version == catalog.SECTION_VERSION == 1
    assert section.status == "ok"

    plan = schema.BootstrapPlan(
        generated_at="2026-07-31T00:00:00Z", mode="local", sections=(section,)
    )
    assert schema.validate_plan_dict(plan.to_dict()) == ()
    assert plan.applicable is True


def test_section_serialisable_json_et_sans_secret(tmp_path: Path) -> None:
    """
    Contrôle positif : la projection contient des données ET aucune fuite.

    Sans le contrôle positif, `find_secret_leaks({})` serait vide et ce test
    resterait vert sur une section devenue muette.
    """
    data = catalog.to_plan_section(_load(tmp_path)).to_dict()

    assert data["data"]["models"] and data["data"]["counts"]["total"] == 1  # contrôle positif
    json.dumps(data)
    assert schema.find_secret_leaks(data) == ()

    # Contrôle négatif du détecteur : injecté, un jeton DOIT être vu.
    polluted = copy.deepcopy(data)
    polluted["data"]["models"][0]["note"] = "hf_" + "z" * 30
    assert schema.find_secret_leaks(polluted) != ()


def test_le_catalogue_livre_se_projette_sans_secret() -> None:
    data = catalog.to_plan_section(catalog.load_catalog()).to_dict()
    assert len(data["data"]["models"]) == 2  # contrôle positif
    assert schema.find_secret_leaks(data) == ()
    json.dumps(data)


def test_une_entree_bloquee_met_la_section_en_echec(tmp_path: Path) -> None:
    """
    Le blocage doit être porté par le STATUT de section.

    `schema.BootstrapPlan.blockers` ne collecte les constats `fail` que des
    sections elles-mêmes en `fail` : un constat bloquant dans une section `warn`
    serait invisible du verdict. Ce test verrouille la conséquence — le plan
    devient inapplicable.
    """
    section = catalog.to_plan_section(
        _load(tmp_path, lambda d: _entry(d)["source"].__setitem__("revision", None))
    )
    assert section.status == "fail"

    plan = schema.BootstrapPlan(
        generated_at="2026-07-31T00:00:00Z", mode="local", sections=(section,)
    )
    assert plan.applicable is False
    assert plan.exit_code() == schema.EXIT_BLOCKED
    assert "catalogue_entree_non_epinglee" in [f.code for f in plan.blockers]


def test_selection_restreint_la_projection() -> None:
    loaded = catalog.load_catalog()
    chosen = loaded.entries[1].id
    section = catalog.to_plan_section(loaded, selected_ids=[chosen])
    assert [m["id"] for m in section.data["models"]] == [chosen]
    assert section.data["counts"]["total"] == 1


def test_selection_dun_identifiant_absent_refusee() -> None:
    with pytest.raises(catalog.CatalogError, match="absent"):
        catalog.to_plan_section(catalog.load_catalog(), selected_ids=["modele-fantome"])


def test_selection_vide_met_la_section_en_echec() -> None:
    section = catalog.to_plan_section(catalog.load_catalog(), selected_ids=[])
    assert section.status == "fail"
    assert "catalogue_vide" in [f.code for f in section.findings]


def test_le_volume_de_telechargement_ne_compte_que_les_entrees_planifiables(tmp_path: Path) -> None:
    section = catalog.to_plan_section(
        _load(tmp_path, lambda d: _entry(d)["source"].__setitem__("revision", None))
    )
    assert section.data["estimated_download_bytes"] == 0

    sain = catalog.to_plan_section(_load(tmp_path))
    assert sain.data["estimated_download_bytes"] == 1024  # contrôle positif


def test_les_ressources_sont_presentees_comme_des_estimations(tmp_path: Path) -> None:
    """§9 : une estimation ne doit jamais être lue comme une mesure."""
    data = catalog.to_plan_section(_load(tmp_path)).data
    assert "ESTIMATIONS" in data["avertissement_ressources"]
    resources = data["models"][0]["resources"]
    assert resources["kind"] == "estimation"
    assert "non mesurées" in resources["avertissement"]
    assert "calibration" in resources["avertissement"]
    assert resources["estimation_basis"] == "gguf_header"


def test_la_section_publie_les_distinctions_de_licence_de_la_section_8(tmp_path: Path) -> None:
    data = catalog.to_plan_section(_load(tmp_path)).data
    assert data["downloader"]["license_id"] == "apache-2.0"

    license_block = data["models"][0]["license"]
    for required in ("base_model", "fine_tune", "usage_terms", "gated",
                     "redistribution_allowed", "operator_acceptance_required"):
        assert required in license_block

    source = data["models"][0]["source"]
    assert source["revision"] == "a" * 40
    assert source["files"][0]["sha256"] == "b" * 64
    assert data["models"][0]["verification"] == "pinned"


def test_le_module_ne_telecharge_ni_necrit_rien(tmp_path: Path) -> None:
    """
    AUT-005 lit et valide ; AUT-006 (téléchargement sûr) n'est pas dans cette vague.

    Contrôle positif : le module importe bien `yaml` et `schema`, donc l'absence
    constatée ensuite porte sur un module réellement inspecté.
    """
    source = Path(catalog.__file__).read_text(encoding="utf-8")
    assert "import yaml" in source and "from . import schema" in source  # contrôle positif

    for forbidden in ("import requests", "import httpx", "urllib.request",
                      "subprocess", "open(", "write_text", "mkdir"):
        assert forbidden not in source, forbidden

    before = sorted(p.name for p in tmp_path.iterdir())
    catalog.to_plan_section(catalog.load_catalog())
    assert sorted(p.name for p in tmp_path.iterdir()) == before
