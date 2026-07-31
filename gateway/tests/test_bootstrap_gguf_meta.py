"""
Tests d'AUT-013 — inspection du header GGUF et estimation conservatrice.

Ce que ces tests verrouillent
-----------------------------
- l'extraction des champs que §9 déclare utiles : architecture, blocs,
  embedding, têtes KV (GQA/MQA), contexte déclaré, MoE, quantisation ;
- l'agrégation des tenseurs — volume et mélange de types, jamais la liste
  nominative ;
- la ROBUSTESSE HOSTILE : magie absente, version refusée, fichier tronqué,
  `n_kv` aberrant, `tensor_count` aberrant, chaîne de longueur absurde,
  tableau de longueur absurde. Aucun cas ne doit allouer, boucler ni lever
  autre chose qu'un `GgufError` ;
- le fait que l'estimation reste une ESTIMATION : elle se nomme ainsi, publie
  ses angles morts, et refuse de produire un total quand le KV n'est pas
  dimensionnable plutôt que de le sous-estimer ;
- le `skip` explicite quand aucun GGUF n'est présent — cas normal en CI.

Aucun vrai GGUF n'est nécessaire : tous les fichiers sont fabriqués octet par
octet par `_build_gguf()`, ce qui permet de produire exactement les corruptions
qu'un fichier téléchargé pourrait présenter.
"""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

from bootstrap import gguf_meta, schema


# ── Fabrication de GGUF synthétiques ─────────────────────────────────────────

def _u32(value: int) -> bytes:
    return struct.pack("<I", value)


def _u64(value: int) -> bytes:
    return struct.pack("<Q", value)


def _gstr(text: str) -> bytes:
    raw = text.encode("utf-8")
    return _u64(len(raw)) + raw


def _kv_str(key: str, value: str) -> bytes:
    return _gstr(key) + _u32(gguf_meta._T_STRING) + _gstr(value)


def _kv_u32(key: str, value: int) -> bytes:
    return _gstr(key) + _u32(gguf_meta._T_UINT32) + _u32(value)


def _kv_f32(key: str, value: float) -> bytes:
    return _gstr(key) + _u32(gguf_meta._T_FLOAT32) + struct.pack("<f", value)


def _kv_str_array(key: str, values: list[str]) -> bytes:
    body = b"".join(_gstr(v) for v in values)
    return _gstr(key) + _u32(gguf_meta._T_ARRAY) + _u32(gguf_meta._T_STRING) + _u64(len(values)) + body


def _kv_u32_array(key: str, values: list[int]) -> bytes:
    body = b"".join(_u32(v) for v in values)
    return _gstr(key) + _u32(gguf_meta._T_ARRAY) + _u32(gguf_meta._T_UINT32) + _u64(len(values)) + body


def _tensor(name: str, dims: list[int], ggml_type: int, offset: int = 0) -> bytes:
    out = _gstr(name) + _u32(len(dims))
    for dim in dims:
        out += _u64(dim)
    return out + _u32(ggml_type) + _u64(offset)


def _build_gguf(
    *,
    version: int = 3,
    magic: bytes = b"GGUF",
    kv_blobs: list[bytes] | None = None,
    tensor_blobs: list[bytes] | None = None,
    kv_count_override: int | None = None,
    tensor_count_override: int | None = None,
    trailing: bytes = b"",
) -> bytes:
    kvs = kv_blobs or []
    tensors = tensor_blobs or []
    head = (
        magic
        + _u32(version)
        + _u64(tensor_count_override if tensor_count_override is not None else len(tensors))
        + _u64(kv_count_override if kv_count_override is not None else len(kvs))
    )
    return head + b"".join(kvs) + b"".join(tensors) + trailing


def _healthy_kvs(arch: str = "qwen2") -> list[bytes]:
    """Un modèle dense plausible : 24 blocs, GQA 14/2 têtes, contexte 32768."""
    return [
        _kv_str("general.architecture", arch),
        _kv_str("general.name", "Fixture 0.5B Instruct"),
        _kv_u32("general.file_type", 15),
        _kv_u32("general.quantization_version", 2),
        _kv_u32(f"{arch}.block_count", 24),
        _kv_u32(f"{arch}.embedding_length", 896),
        _kv_u32(f"{arch}.context_length", 32768),
        _kv_u32(f"{arch}.attention.head_count", 14),
        _kv_u32(f"{arch}.attention.head_count_kv", 2),
        _kv_f32(f"{arch}.rope.freq_base", 1000000.0),
        # Bruit volumineux et hors whitelist : doit être franchi, pas retenu.
        _kv_str_array("tokenizer.ggml.tokens", [f"tok{i}" for i in range(64)]),
        _kv_u32_array("tokenizer.ggml.token_type", [1] * 64),
        _kv_str("tokenizer.chat_template", "{% for m in messages %}{{ m }}{% endfor %}"),
    ]


def _healthy_tensors() -> list[bytes]:
    return [
        _tensor("token_embd.weight", [896, 151936], 12),   # Q4_K
        _tensor("blk.0.attn_q.weight", [896, 896], 12),
        _tensor("blk.0.attn_norm.weight", [896], 0),       # F32
        _tensor("output_norm.weight", [896], 0),
    ]


def _write(tmp_path: Path, payload: bytes, name: str = "fixture.gguf") -> Path:
    target = tmp_path / name
    target.write_bytes(payload)
    return target


# ── Extraction nominale ──────────────────────────────────────────────────────

def test_header_extrait_les_champs_utiles_de_la_section_9(tmp_path: Path) -> None:
    path = _write(tmp_path, _build_gguf(kv_blobs=_healthy_kvs(), tensor_blobs=_healthy_tensors()))
    header = gguf_meta.inspect_gguf(path)

    assert header.version == 3
    assert header.architecture == "qwen2"
    assert header.model_name == "Fixture 0.5B Instruct"
    assert header.block_count == 24
    assert header.embedding_length == 896
    assert header.head_count == 14
    assert header.head_count_kv == 2
    assert header.context_length == 32768
    assert header.rope_freq_base == pytest.approx(1000000.0)
    assert header.file_type == 15
    assert header.quantization_version == 2
    # GQA : les têtes KV sont bien celles de `head_count_kv`, pas `head_count`.
    assert header.kv_head_count == 2
    assert header.head_dim == 896 // 14


def test_key_length_declare_prime_sur_la_deduction(tmp_path: Path) -> None:
    """Une architecture qui découple la dimension de tête ne doit pas être déduite."""
    kvs = _healthy_kvs() + [_kv_u32("qwen2.attention.key_length", 128)]
    header = gguf_meta.inspect_gguf(_write(tmp_path, _build_gguf(kv_blobs=kvs)))
    assert header.key_length == 128
    assert header.head_dim == 128  # et non 896 // 14


def test_vocab_size_vient_de_la_longueur_du_tableau_pas_de_son_contenu(tmp_path: Path) -> None:
    path = _write(tmp_path, _build_gguf(kv_blobs=_healthy_kvs()))
    header = gguf_meta.inspect_gguf(path)
    assert header.vocab_size == 64


def test_metadonnees_moe_detectees(tmp_path: Path) -> None:
    kvs = _healthy_kvs("qwen3moe") + [
        _kv_u32("qwen3moe.expert_count", 128),
        _kv_u32("qwen3moe.expert_used_count", 8),
    ]
    header = gguf_meta.inspect_gguf(_write(tmp_path, _build_gguf(kv_blobs=kvs)))
    assert header.expert_count == 128
    assert header.expert_used_count == 8
    assert header.is_moe is True


def test_modele_dense_nest_pas_moe(tmp_path: Path) -> None:
    """Contrôle positif du test précédent : sans experts, `is_moe` est faux."""
    header = gguf_meta.inspect_gguf(_write(tmp_path, _build_gguf(kv_blobs=_healthy_kvs())))
    assert header.expert_count is None
    assert header.is_moe is False


# ── Inventaire agrégé ────────────────────────────────────────────────────────

def test_inventaire_des_tenseurs_agrege_les_volumes_par_type(tmp_path: Path) -> None:
    path = _write(tmp_path, _build_gguf(kv_blobs=_healthy_kvs(), tensor_blobs=_healthy_tensors()))
    inventory = gguf_meta.inspect_gguf(path).tensors

    assert inventory is not None
    assert inventory.count == 4
    assert inventory.total_elements == 896 * 151936 + 896 * 896 + 896 + 896
    assert set(inventory.elements_by_type) == {"Q4_K", "F32"}
    assert inventory.elements_by_type["F32"] == 896 + 896
    # Q4_K : 256 éléments par bloc de 144 octets.
    q4k_elements = 896 * 151936 + 896 * 896
    assert inventory.bytes_by_type["Q4_K"] == ((q4k_elements + 255) // 256) * 144
    assert inventory.complete is True


def test_inventaire_nest_pas_nominatif(tmp_path: Path) -> None:
    """
    Le nom des tenseurs ne doit apparaître nulle part dans la projection.

    Contrôle positif : le test vérifie d'abord que la projection contient bien
    quelque chose de reconnaissable (le compte et un type), faute de quoi une
    assertion d'absence sur un dictionnaire vide serait toujours verte.
    """
    path = _write(tmp_path, _build_gguf(kv_blobs=_healthy_kvs(), tensor_blobs=_healthy_tensors()))
    rendered = str(gguf_meta.inspect_gguf(path).to_dict())

    assert "Q4_K" in rendered and "'count': 4" in rendered  # contrôle positif
    assert "token_embd.weight" not in rendered
    assert "blk.0.attn_q.weight" not in rendered


def test_type_ggml_inconnu_rend_lagregat_partiel_sans_echouer(tmp_path: Path) -> None:
    tensors = _healthy_tensors() + [_tensor("blk.1.futur.weight", [128], 250)]
    header = gguf_meta.inspect_gguf(
        _write(tmp_path, _build_gguf(kv_blobs=_healthy_kvs(), tensor_blobs=tensors))
    )
    assert header.tensors is not None
    assert header.tensors.unknown_type_count == 1
    assert header.tensors.complete is False
    assert header.tensors.total_bytes is None


# ── Robustesse hostile ───────────────────────────────────────────────────────

def test_fichier_vide_refuse(tmp_path: Path) -> None:
    with pytest.raises(gguf_meta.GgufError, match="trop court"):
        gguf_meta.inspect_gguf(_write(tmp_path, b""))


def test_fichier_absent_refuse(tmp_path: Path) -> None:
    with pytest.raises(gguf_meta.GgufError, match="illisible"):
        gguf_meta.inspect_gguf(tmp_path / "jamais-telecharge.gguf")


def test_magie_absente_refusee(tmp_path: Path) -> None:
    payload = _build_gguf(magic=b"NOPE", kv_blobs=_healthy_kvs())
    with pytest.raises(gguf_meta.GgufError, match="magie GGUF absente"):
        gguf_meta.inspect_gguf(_write(tmp_path, payload))


def test_gguf_big_endian_refuse_explicitement(tmp_path: Path) -> None:
    payload = _build_gguf(magic=b"FUGG", kv_blobs=_healthy_kvs())
    with pytest.raises(gguf_meta.GgufError, match="big-endian"):
        gguf_meta.inspect_gguf(_write(tmp_path, payload))


@pytest.mark.parametrize("version", [1, 4, 0xFFFFFFFF])
def test_version_non_supportee_refusee(tmp_path: Path, version: int) -> None:
    payload = _build_gguf(version=version, kv_blobs=_healthy_kvs())
    with pytest.raises(gguf_meta.GgufError, match="non supportée"):
        gguf_meta.inspect_gguf(_write(tmp_path, payload))


def test_fichier_tronque_en_plein_header(tmp_path: Path) -> None:
    payload = _build_gguf(kv_blobs=_healthy_kvs(), tensor_blobs=_healthy_tensors())
    truncated = payload[: len(payload) // 2]
    with pytest.raises(gguf_meta.GgufError, match="tronqué|au-delà de la fenêtre"):
        gguf_meta.inspect_gguf(_write(tmp_path, truncated))


def test_kv_count_aberrant_refuse_avant_toute_boucle(tmp_path: Path) -> None:
    """Le compteur vient du fichier : il est plafonné AVANT de servir d'itérateur."""
    payload = _build_gguf(kv_blobs=_healthy_kvs(), kv_count_override=2**63)
    with pytest.raises(gguf_meta.GgufError, match="metadata_kv_count aberrant"):
        gguf_meta.inspect_gguf(_write(tmp_path, payload))


def test_tensor_count_aberrant_refuse_avant_toute_boucle(tmp_path: Path) -> None:
    payload = _build_gguf(kv_blobs=_healthy_kvs(), tensor_count_override=2**40)
    with pytest.raises(gguf_meta.GgufError, match="tensor_count aberrant"):
        gguf_meta.inspect_gguf(_write(tmp_path, payload))


def test_chaine_de_longueur_absurde_refusee_sans_allocation(tmp_path: Path) -> None:
    """
    Une clé annonce 2^60 octets dans un fichier de quelques centaines d'octets.

    Le plafond `MAX_STRING_BYTES` doit trancher avant `take()`, donc avant toute
    tentative d'allocation.
    """
    hostile = _u64(2**60) + b"x" * 8 + _u32(gguf_meta._T_UINT32) + _u32(1)
    payload = _build_gguf(kv_blobs=[hostile], kv_count_override=1)
    with pytest.raises(gguf_meta.GgufError, match="au-delà du plafond"):
        gguf_meta.inspect_gguf(_write(tmp_path, payload))


def test_valeur_chaine_de_longueur_absurde_refusee(tmp_path: Path) -> None:
    hostile = _gstr("general.name") + _u32(gguf_meta._T_STRING) + _u64(2**40)
    payload = _build_gguf(kv_blobs=[hostile], kv_count_override=1)
    with pytest.raises(gguf_meta.GgufError, match="au-delà du plafond"):
        gguf_meta.inspect_gguf(_write(tmp_path, payload))


def test_tableau_de_longueur_absurde_refuse_sans_allocation(tmp_path: Path) -> None:
    hostile = (
        _gstr("tokenizer.ggml.tokens")
        + _u32(gguf_meta._T_ARRAY)
        + _u32(gguf_meta._T_STRING)
        + _u64(2**50)
    )
    payload = _build_gguf(kv_blobs=[hostile], kv_count_override=1)
    with pytest.raises(gguf_meta.GgufError, match="au-delà du plafond"):
        gguf_meta.inspect_gguf(_write(tmp_path, payload))


def test_tableau_de_longueur_credible_mais_menteuse_bute_sur_la_fenetre(tmp_path: Path) -> None:
    """
    Longueur sous le plafond mais très supérieure au contenu réel.

    C'est le cas que le plafond seul ne rattrape pas : la borne de fenêtre doit
    faire le travail, et la boucle doit se terminer.
    """
    hostile = (
        _gstr("tokenizer.ggml.tokens")
        + _u32(gguf_meta._T_ARRAY)
        + _u32(gguf_meta._T_STRING)
        + _u64(1_000_000)
    )
    payload = _build_gguf(kv_blobs=[hostile], kv_count_override=1)
    with pytest.raises(gguf_meta.GgufError, match="au-delà de la fenêtre"):
        gguf_meta.inspect_gguf(_write(tmp_path, payload))


def test_tableau_de_type_non_franchissable_refuse(tmp_path: Path) -> None:
    """Un tableau de tableaux ne peut pas être franchi sûrement : on refuse."""
    hostile = (
        _gstr("bidon")
        + _u32(gguf_meta._T_ARRAY)
        + _u32(gguf_meta._T_ARRAY)
        + _u64(4)
    )
    payload = _build_gguf(kv_blobs=[hostile], kv_count_override=1)
    with pytest.raises(gguf_meta.GgufError, match="non franchissable"):
        gguf_meta.inspect_gguf(_write(tmp_path, payload))


def test_type_de_metadonnee_inconnu_refuse(tmp_path: Path) -> None:
    hostile = _gstr("bidon") + _u32(999) + _u64(0)
    payload = _build_gguf(kv_blobs=[hostile], kv_count_override=1)
    with pytest.raises(gguf_meta.GgufError, match="type de métadonnée inconnu"):
        gguf_meta.inspect_gguf(_write(tmp_path, payload))


def test_tenseur_a_dimensions_aberrantes_refuse(tmp_path: Path) -> None:
    hostile = _gstr("blk.0.hostile") + _u32(2**31) + _u32(0) + _u64(0)
    payload = _build_gguf(
        kv_blobs=_healthy_kvs(), tensor_blobs=[hostile], tensor_count_override=1
    )
    with pytest.raises(gguf_meta.GgufError, match="dimensions"):
        gguf_meta.inspect_gguf(_write(tmp_path, payload))


def test_aucune_exception_hors_gguferror_sur_entree_aleatoire(tmp_path: Path) -> None:
    """
    Balayage : chaque troncature du fichier sain lève `GgufError` et rien d'autre.

    C'est la propriété qui compte réellement en production — un `struct.error`
    ou un `MemoryError` remonterait jusqu'au planificateur.
    """
    payload = _build_gguf(kv_blobs=_healthy_kvs(), tensor_blobs=_healthy_tensors())
    survivors = 0
    for cut in range(24, len(payload), 37):
        target = _write(tmp_path, payload[:cut], name=f"cut-{cut}.gguf")
        try:
            gguf_meta.inspect_gguf(target)
            survivors += 1
        except gguf_meta.GgufError:
            pass
    # Contrôle positif : le fichier entier, lui, doit rester lisible.
    assert gguf_meta.inspect_gguf(_write(tmp_path, payload, name="entier.gguf")).block_count == 24
    assert survivors == 0


# ── Estimation ───────────────────────────────────────────────────────────────

def test_estimation_du_cache_kv_suit_le_contexte_et_le_parallelisme(tmp_path: Path) -> None:
    header = gguf_meta.inspect_gguf(_write(tmp_path, _build_gguf(kv_blobs=_healthy_kvs())))

    base = gguf_meta.estimate_footprint(
        header, gguf_meta.EstimationInputs(ctx_size=4096, parallel=1)
    )
    double_ctx = gguf_meta.estimate_footprint(
        header, gguf_meta.EstimationInputs(ctx_size=8192, parallel=1)
    )
    double_par = gguf_meta.estimate_footprint(
        header, gguf_meta.EstimationInputs(ctx_size=4096, parallel=2)
    )

    assert base.kv_cache_bytes is not None
    # 4096 slots × 24 blocs × 2 têtes KV × 64 de dimension × (2 + 2) octets f16.
    assert base.kv_cache_bytes == 4096 * 24 * 2 * (896 // 14) * 4
    assert double_ctx.kv_cache_bytes == 2 * base.kv_cache_bytes
    assert double_par.kv_cache_bytes == 2 * base.kv_cache_bytes


def test_cache_quantise_coute_moins_que_f16(tmp_path: Path) -> None:
    header = gguf_meta.inspect_gguf(_write(tmp_path, _build_gguf(kv_blobs=_healthy_kvs())))
    f16 = gguf_meta.estimate_footprint(header, gguf_meta.EstimationInputs(cache_type_k="f16", cache_type_v="f16"))
    q8 = gguf_meta.estimate_footprint(header, gguf_meta.EstimationInputs(cache_type_k="q8_0", cache_type_v="q8_0"))
    assert q8.kv_cache_bytes is not None and f16.kv_cache_bytes is not None
    assert q8.kv_cache_bytes < f16.kv_cache_bytes


def test_cache_type_inconnu_refuse(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cache_type_k inconnu"):
        gguf_meta.EstimationInputs(cache_type_k="q3_0")


def test_pas_de_total_quand_le_kv_nest_pas_dimensionnable(tmp_path: Path) -> None:
    """
    Sans blocs ni têtes, l'estimation refuse un total plutôt que de le minorer.

    Une estimation amputée du cache KV serait une SOUS-estimation : c'est
    exactement l'erreur qui fait échouer un chargement en production.
    """
    minimal = [_kv_str("general.architecture", "mystere")]
    header = gguf_meta.inspect_gguf(_write(tmp_path, _build_gguf(kv_blobs=minimal)))
    estimate = gguf_meta.estimate_footprint(header)

    assert estimate.kv_cache_bytes is None
    assert estimate.total_bytes is None
    assert estimate.total_gb is None
    assert any("non estimable" in note for note in estimate.notes)


def test_ctx_superieur_au_contexte_declare_est_signale(tmp_path: Path) -> None:
    header = gguf_meta.inspect_gguf(_write(tmp_path, _build_gguf(kv_blobs=_healthy_kvs())))
    trop = gguf_meta.estimate_footprint(header, gguf_meta.EstimationInputs(ctx_size=65536))
    ok = gguf_meta.estimate_footprint(header, gguf_meta.EstimationInputs(ctx_size=4096))

    assert any("supérieur au contexte déclaré" in note for note in trop.notes)
    assert not any("supérieur au contexte déclaré" in note for note in ok.notes)


def test_moe_signale_le_report_sur_la_ram_hote(tmp_path: Path) -> None:
    kvs = _healthy_kvs("qwen3moe") + [
        _kv_u32("qwen3moe.expert_count", 128),
        _kv_u32("qwen3moe.expert_used_count", 8),
    ]
    header = gguf_meta.inspect_gguf(_write(tmp_path, _build_gguf(kv_blobs=kvs)))
    estimate = gguf_meta.estimate_footprint(header)
    assert any("cpu_moe" in note for note in estimate.notes)


def test_la_marge_de_securite_majore_le_total(tmp_path: Path) -> None:
    header = gguf_meta.inspect_gguf(_write(tmp_path, _build_gguf(kv_blobs=_healthy_kvs())))
    sans = gguf_meta.estimate_footprint(header, gguf_meta.EstimationInputs(safety_margin=0.0))
    avec = gguf_meta.estimate_footprint(header, gguf_meta.EstimationInputs(safety_margin=0.5))
    assert sans.total_bytes is not None and avec.total_bytes is not None
    assert avec.total_bytes > sans.total_bytes
    assert avec.total_bytes == int(sans.total_bytes * 1.5)


def test_les_poids_sont_la_taille_du_fichier(tmp_path: Path) -> None:
    """Le fichier majore les tenseurs et il est mesuré, pas déduit d'une table."""
    payload = _build_gguf(kv_blobs=_healthy_kvs(), tensor_blobs=_healthy_tensors())
    path = _write(tmp_path, payload)
    estimate = gguf_meta.estimate_footprint(gguf_meta.inspect_gguf(path))
    assert estimate.weights_bytes == len(payload)


def test_lestimation_se_presente_comme_une_estimation(tmp_path: Path) -> None:
    """
    §9 : l'estimation ne doit jamais passer pour une mesure, et ses angles morts
    doivent apparaître DANS LE RENDU, pas seulement dans le code.
    """
    header = gguf_meta.inspect_gguf(_write(tmp_path, _build_gguf(kv_blobs=_healthy_kvs())))
    payload = gguf_meta.estimate_footprint(header).to_dict()

    assert payload["kind"] == "estimation"
    assert "PAS une mesure" in payload["avertissement"]
    assert payload["facteurs_ignores"] == list(gguf_meta.FACTEURS_IGNORES)
    for expected in ("Flash Attention", "fragmentation", "llama.cpp"):
        assert any(expected in factor for factor in payload["facteurs_ignores"])


def test_le_rendu_humain_publie_les_facteurs_ignores(tmp_path: Path) -> None:
    path = _write(tmp_path, _build_gguf(kv_blobs=_healthy_kvs(), tensor_blobs=_healthy_tensors()))
    text = gguf_meta.render_human(gguf_meta.inspect_paths([path]))

    assert "ESTIMATIONS" in text and "pas des mesures" in text
    assert "CE QUE CETTE ESTIMATION NE VOIT PAS" in text
    for factor in gguf_meta.FACTEURS_IGNORES:
        assert factor in text


# ── Agrégation et cas « machine vierge » ─────────────────────────────────────

def test_aucun_gguf_local_donne_un_skip_explicite(tmp_path: Path) -> None:
    """Cas normal en CI et sur une machine vierge : ce n'est PAS un échec."""
    inspection = gguf_meta.inspect_paths([tmp_path / "absent.gguf", tmp_path / "autre.gguf"])
    assert inspection.status == "skip"
    assert inspection.entries == ()
    assert [f.level for f in inspection.findings] == ["info"]
    assert inspection.findings[0].code == "gguf_absent"


def test_seul_gguf_present_illisible_est_un_fail(tmp_path: Path) -> None:
    corrompu = _write(tmp_path, b"GGUF" + b"\x00" * 40, name="corrompu.gguf")
    inspection = gguf_meta.inspect_paths([corrompu])
    assert inspection.status == "fail"
    assert [f.code for f in inspection.findings] == ["gguf_illisible"]
    assert inspection.entries[0]["readable"] is False


def test_melange_lisible_et_illisible_degrade_en_warn(tmp_path: Path) -> None:
    sain = _write(tmp_path, _build_gguf(kv_blobs=_healthy_kvs()), name="sain.gguf")
    casse = _write(tmp_path, b"GGUF" + b"\x00" * 40, name="casse.gguf")
    inspection = gguf_meta.inspect_paths([sain, casse])
    assert inspection.status == "warn"
    assert len(inspection.entries) == 2


def test_inspection_reussie_est_ok(tmp_path: Path) -> None:
    sain = _write(tmp_path, _build_gguf(kv_blobs=_healthy_kvs()))
    inspection = gguf_meta.inspect_paths([sain])
    assert inspection.status == "ok"
    assert inspection.findings == ()


def test_projection_json_sans_secret_et_serialisable(tmp_path: Path) -> None:
    """
    Contrôle positif : la projection contient bien des données, et aucune fuite.

    Sans le contrôle positif, `find_secret_leaks({})` serait vide et le test
    resterait vert sur une projection devenue muette.
    """
    import json

    sain = _write(tmp_path, _build_gguf(kv_blobs=_healthy_kvs(), tensor_blobs=_healthy_tensors()))
    data = gguf_meta.inspect_paths([sain]).to_plan_data()

    assert data["status"] == "ok" and data["files"]  # contrôle positif
    json.dumps(data)  # doit être sérialisable sans encodeur maison
    assert schema.find_secret_leaks(data) == ()

    # Contrôle négatif du détecteur : injecté, un token DOIT être vu.
    polluted = dict(data)
    polluted["note"] = "hf_" + "a" * 30
    assert schema.find_secret_leaks(polluted) != ()


def test_inspection_ne_modifie_pas_le_fichier(tmp_path: Path) -> None:
    payload = _build_gguf(kv_blobs=_healthy_kvs(), tensor_blobs=_healthy_tensors())
    path = _write(tmp_path, payload)
    before = path.stat().st_mtime_ns
    gguf_meta.inspect_gguf(path)
    assert path.read_bytes() == payload
    assert path.stat().st_mtime_ns == before


def test_findings_of_dedoublonne(tmp_path: Path) -> None:
    casse = _write(tmp_path, b"GGUF" + b"\x00" * 40, name="a.gguf")
    autre = _write(tmp_path, b"GGUF" + b"\x00" * 40, name="b.gguf")
    merged = gguf_meta.findings_of([
        gguf_meta.inspect_paths([casse]), gguf_meta.inspect_paths([autre])
    ])
    assert [f.code for f in merged] == ["gguf_illisible"]
