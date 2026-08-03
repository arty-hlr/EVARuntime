"""
AUT-003 — tests du résolveur `llama-server` et du manifeste de provenance.

Trois familles d'assertions portent le critère d'acceptation « sélection
versionnée, SHA vérifié, aucun fallback CPU silencieux » :

- l'**ordre de résolution** de §6, une étape par test, refus explicite compris ;
- le **manifeste** qui ne peut pas être incohérent (sha manquant, digest croisé,
  backend GPU sans option de build) ;
- l'**absence de repli CPU tacite** : le drapeau `degraded` et son constat `warn`
  sont testés séparément, de sorte que supprimer l'un ou l'autre casse le rouge.

Les tests d'absence (pas de secret, pas d'import réseau, pas de constat de repli)
portent chacun un contrôle positif : sans lui, ils resteraient verts en devenant
aveugles.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import io
import json
import tarfile
from pathlib import Path

import pytest
import yaml

from bootstrap import applier as ap
from bootstrap import execution as ex
from bootstrap import install_report as ir
from bootstrap import runtime_installer as ri
from bootstrap import runtime_resolver as rr
from bootstrap import schema
from llama_version import LlamaVersion

SHA_A = "a" * 64
SHA_B = "b" * 64
# Empreinte du binaire POSÉ (bloc `install:` du manifeste), distincte de celle de
# l'archive téléchargée (`artifact_sha256`, bloc `runtime:`).
SHA_BINARY = "d" * 64
SHA_OTHER = "e" * 64
DIGEST = "sha256:" + "c" * 64
COMMIT = "0123456789abcdef0123456789abcdef01234567"
NOW = "2026-07-31T09:00:00+00:00"


# ── Fabriques ─────────────────────────────────────────────────────────────────

def make_release(*, version: str = "b6800", floor: int = 6700) -> rr.ReleasePolicy:
    return rr.ReleasePolicy(pinned_version=version, pinned_commit=COMMIT, security_floor_build=floor)


def make_policy(variants: tuple[rr.ArtifactVariant, ...], **kwargs) -> rr.ResolverPolicy:
    release = kwargs.pop("release", None) or make_release()
    return rr.ResolverPolicy(release=release, variants=variants, **kwargs)


def nvidia_profile() -> rr.HardwareProfile:
    return rr.HardwareProfile(
        platform="linux-x86_64",
        backend_candidates=("cuda12", "cpu"),
        gpu_vendor="nvidia",
        driver_version="580.65.06",
        cuda_major=12,
        gpu_count=1,
    )


def cpu_profile() -> rr.HardwareProfile:
    return rr.HardwareProfile(platform="linux-x86_64", backend_candidates=("cpu",))


def variant(source: str, backend: str = "cuda12", **kwargs) -> rr.ArtifactVariant:
    defaults: dict = {
        "platform": "linux-x86_64",
        "evidence": rr.EVIDENCE_SPEC,
        "evidence_note": "Variante de test.",
    }
    if source == rr.SOURCE_OFFICIAL_CONTAINER:
        defaults["container_digest"] = DIGEST
    elif source != rr.SOURCE_LOCAL_BUILD:
        defaults["artifact_sha256"] = SHA_A
    defaults.update(kwargs)
    return rr.ArtifactVariant(source=source, backend=backend, **defaults)


def resolve(profile: rr.HardwareProfile, policy: rr.ResolverPolicy, **kwargs) -> rr.RuntimeResolution:
    return asyncio.run(rr.resolve_runtime(profile, policy, installed_at=NOW, **kwargs))


def probing(build: int | None, raw: str = "version: ?", commit: str | None = None):
    """Sonde `--version` injectée : le résolveur ne lance aucun sous-processus en test."""
    async def _probe(_binary: Path) -> LlamaVersion:
        return LlamaVersion(build=build, raw=raw, commit=commit)
    return _probe


def digesting(empreinte: str | None):
    """Empreinte du binaire posé, injectée : aucun fichier réel n'est lu en test."""
    async def _digest(_binary: Path) -> str | None:
        return empreinte
    return _digest


def posed_manifest(**kwargs) -> rr.ProvenanceManifest:
    """Manifeste cohérent avec le binaire simulé : build 6750, commit COMMIT."""
    defaults: dict = {
        "version": "b6750", "commit": COMMIT, "source": rr.SOURCE_LOCAL_BUILD,
        "backend": "cuda12", "platform": "linux-x86_64", "installed_at": NOW,
        "build_options": {"GGML_CUDA": True},
    }
    defaults.update(kwargs)
    return rr.ProvenanceManifest(**defaults)


def codes(resolution: rr.RuntimeResolution) -> set[str]:
    return {f.code for f in resolution.findings}


# ── Contrat de producteur (AUT-001) ───────────────────────────────────────────

def test_producer_contract_matches_schema():
    assert rr.SECTION_NAME == schema.SECTION_RUNTIME
    assert rr.SECTION_NAME in schema.SECTION_NAMES
    assert isinstance(rr.SECTION_VERSION, int) and rr.SECTION_VERSION >= 1


def test_plan_section_validates_inside_a_plan():
    """Une section produite ici doit traverser `validate_plan_dict` sans erreur."""
    resolution = resolve(nvidia_profile(), make_policy((variant(rr.SOURCE_OFFICIAL_RELEASE),)))
    plan = schema.BootstrapPlan(
        generated_at=NOW,
        mode="dry-run",
        sections=(rr.to_plan_section(resolution),),
        steps=rr.to_plan_steps(resolution),
        decisions=(rr.to_decision(resolution),),
    )
    assert schema.validate_plan_dict(plan.to_dict()) == ()


# ── Manifeste de provenance (§6) ──────────────────────────────────────────────

def test_manifest_matches_section_6_fields():
    manifest = rr.ProvenanceManifest(
        version="b6800", commit=COMMIT, source=rr.SOURCE_OFFICIAL_RELEASE,
        backend="cuda12", platform="linux-x86_64", artifact_sha256=SHA_A, installed_at=NOW,
    )
    assert set(manifest.to_dict()) == {
        "project", "version", "commit", "source", "backend", "platform",
        "artifact_sha256", "container_digest", "build_options", "installed_at",
    }
    assert manifest.to_dict()["project"] == "ggml-org/llama.cpp"
    assert manifest.build_number == 6800
    assert manifest.is_verifiable is True


def test_manifest_yaml_round_trip():
    manifest = rr.ProvenanceManifest(
        version="b6800", commit=COMMIT, source=rr.SOURCE_LOCAL_BUILD,
        backend="cuda12", platform="linux-x86_64", installed_at=NOW,
        build_options={"GGML_CUDA": True, "GGML_NATIVE": False},
    )
    document = yaml.safe_load(manifest.to_yaml())
    assert rr.validate_manifest_document(document) == ()
    assert rr.manifest_from_document(document) == manifest


def test_official_release_without_sha_is_rejected():
    with pytest.raises(rr.ProvenanceError, match="artifact_sha256"):
        rr.ProvenanceManifest(
            version="b6800", commit=COMMIT, source=rr.SOURCE_OFFICIAL_RELEASE,
            backend="cpu", platform="linux-x86_64", installed_at=NOW,
        )


def test_official_container_without_digest_is_rejected():
    with pytest.raises(rr.ProvenanceError, match="container_digest"):
        rr.ProvenanceManifest(
            version="b6800", commit=COMMIT, source=rr.SOURCE_OFFICIAL_CONTAINER,
            backend="cuda12", platform="linux-x86_64", installed_at=NOW,
        )


def test_manifest_cannot_carry_both_fingerprints():
    """Une image n'a pas de sha d'archive, une archive n'a pas de digest d'image."""
    with pytest.raises(rr.ProvenanceError):
        rr.ProvenanceManifest(
            version="b6800", commit=COMMIT, source=rr.SOURCE_OFFICIAL_CONTAINER,
            backend="cuda12", platform="linux-x86_64", installed_at=NOW,
            container_digest=DIGEST, artifact_sha256=SHA_A,
        )
    with pytest.raises(rr.ProvenanceError):
        rr.ProvenanceManifest(
            version="b6800", commit=COMMIT, source=rr.SOURCE_OFFICIAL_RELEASE,
            backend="cpu", platform="linux-x86_64", installed_at=NOW,
            artifact_sha256=SHA_A, container_digest=DIGEST,
        )


def test_gpu_build_without_its_cmake_flag_is_rejected():
    """Un `backend: cuda12` sans GGML_CUDA décrit un binaire CPU déguisé."""
    with pytest.raises(rr.ProvenanceError, match="GGML_CUDA"):
        rr.ProvenanceManifest(
            version="b6800", commit=COMMIT, source=rr.SOURCE_LOCAL_BUILD,
            backend="cuda12", platform="linux-x86_64", installed_at=NOW,
            build_options={"GGML_CUDA": False},
        )
    # Contrôle positif : la même construction, drapeau posé, est acceptée.
    rr.ProvenanceManifest(
        version="b6800", commit=COMMIT, source=rr.SOURCE_LOCAL_BUILD,
        backend="cuda12", platform="linux-x86_64", installed_at=NOW,
        build_options={"GGML_CUDA": True},
    )


def test_local_build_without_options_is_rejected():
    with pytest.raises(rr.ProvenanceError, match="build_options"):
        rr.ProvenanceManifest(
            version="b6800", commit=COMMIT, source=rr.SOURCE_LOCAL_BUILD,
            backend="cpu", platform="linux-x86_64", installed_at=NOW,
        )


@pytest.mark.parametrize("field_name,value", [
    ("version", "6800"),          # tag sans « b » : non comparable à --version
    ("version", "master"),
    ("commit", "nope"),
    ("source", "quelque-part"),
    ("backend", "cuda99"),
    ("platform", "Linux X86"),
    ("installed_at", ""),
])
def test_manifest_rejects_malformed_fields(field_name, value):
    kwargs = {
        "version": "b6800", "commit": COMMIT, "source": rr.SOURCE_OFFICIAL_RELEASE,
        "backend": "cpu", "platform": "linux-x86_64", "artifact_sha256": SHA_A,
        "installed_at": NOW,
    }
    kwargs[field_name] = value
    with pytest.raises(rr.ProvenanceError):
        rr.ProvenanceManifest(**kwargs)


def test_validate_manifest_document_rejects_foreign_shapes():
    assert rr.validate_manifest_document("pas un objet")
    assert rr.validate_manifest_document({"autre": {}})
    assert rr.validate_manifest_document({"runtime": {"version": "b1"}})


# ── Politique de release et LLAMA_SERVER_MIN_BUILD ────────────────────────────

def test_min_build_is_derived_from_the_release_policy():
    assert rr.derive_min_build(make_release(version="b6800", floor=6700)) == 6700
    assert rr.derive_min_build(make_release(version="b6800", floor=0)) == 0


def test_policy_cannot_pin_a_build_below_its_own_security_floor():
    with pytest.raises(rr.ProvenanceError, match="plancher de sécurité"):
        rr.ReleasePolicy(pinned_version="b6600", pinned_commit=COMMIT, security_floor_build=6700)


def test_absent_floor_is_reported_as_an_inert_guardrail():
    resolution = resolve(
        cpu_profile(),
        make_policy((variant(rr.SOURCE_LOCAL_BUILD, "cpu"),), release=make_release(floor=0)),
    )
    assert "min_build_not_enforced" in codes(resolution)
    assert resolution.status == "warn"


# ── Ordre de résolution de §6, une étape par test ─────────────────────────────

def test_step1_official_release_wins_when_it_covers_the_backend():
    policy = make_policy((
        variant(rr.SOURCE_LOCAL_BUILD),
        variant(rr.SOURCE_EVARUNTIME_BUILD, artifact_sha256=SHA_B),
        variant(rr.SOURCE_OFFICIAL_CONTAINER),
        variant(rr.SOURCE_OFFICIAL_RELEASE),
    ), allow_container=True)
    resolution = resolve(nvidia_profile(), policy)
    assert resolution.resolved is True
    assert resolution.variant.source == rr.SOURCE_OFFICIAL_RELEASE
    assert resolution.manifest.artifact_sha256 == SHA_A


def test_step2_pinned_container_wins_when_no_native_artifact_covers_cuda():
    """Cas NVIDIA Linux de §6 : pas d'archive CUDA, mais une image server-cuda."""
    policy = make_policy((
        variant(rr.SOURCE_OFFICIAL_RELEASE, "cpu"),   # archive CPU seulement
        variant(rr.SOURCE_OFFICIAL_CONTAINER),
        variant(rr.SOURCE_LOCAL_BUILD),
    ), allow_container=True)
    resolution = resolve(nvidia_profile(), policy)
    assert resolution.variant.source == rr.SOURCE_OFFICIAL_CONTAINER
    assert resolution.backend == "cuda12"
    assert resolution.manifest.container_digest == DIGEST
    assert resolution.manifest.artifact_sha256 is None
    # Le gestionnaire de serveurs ne sait pas encore piloter un conteneur.
    assert "container_backend_unsupported" in codes(resolution)


def test_step3_evaruntime_ci_artifact_wins_when_container_is_refused():
    policy = make_policy((
        variant(rr.SOURCE_OFFICIAL_CONTAINER),
        variant(rr.SOURCE_EVARUNTIME_BUILD, artifact_sha256=SHA_B),
        variant(rr.SOURCE_LOCAL_BUILD),
    ), allow_container=False)
    resolution = resolve(nvidia_profile(), policy)
    assert resolution.variant.source == rr.SOURCE_EVARUNTIME_BUILD
    assert any("allow_container=False" in r for r in resolution.rejected)


def test_step4_local_build_is_the_last_resort_before_refusal():
    policy = make_policy((variant(rr.SOURCE_LOCAL_BUILD),))
    resolution = resolve(nvidia_profile(), policy)
    assert resolution.variant.source == rr.SOURCE_LOCAL_BUILD
    assert resolution.manifest.build_options["GGML_CUDA"] is True
    assert resolution.manifest.build_options["GGML_NATIVE"] is False
    # Pas d'empreinte avant la construction : le manifeste est explicitement incomplet.
    assert "local_build_manifest_pending" in codes(resolution)


def test_step5_explicit_refusal_when_no_variant_is_safe():
    policy = make_policy((variant(rr.SOURCE_LOCAL_BUILD),), allow_local_build=False)
    resolution = resolve(nvidia_profile(), policy)
    assert resolution.resolved is False
    assert resolution.status == "fail"
    assert resolution.variant is None and resolution.manifest is None
    assert "runtime_unresolved" in codes(resolution) or "cpu_fallback_refused" in codes(resolution)
    # Un refus doit dire ce qu'il a écarté, sinon il n'est pas actionnable.
    assert resolution.rejected


def test_refusal_is_a_result_not_an_exception():
    """Le refus traverse la projection vers le plan sans lever."""
    policy = make_policy((), allow_local_build=False)
    resolution = resolve(nvidia_profile(), policy)
    section = rr.to_plan_section(resolution)
    assert section.status == "fail"
    assert rr.to_plan_steps(resolution) == ()
    assert rr.to_decision(resolution).choice == "aucun — refus explicite"


def test_unpinned_variants_are_skipped_with_their_reason():
    """« SHA vérifié » : une archive sans empreinte n'est pas installée en attendant."""
    policy = make_policy((
        rr.ArtifactVariant(
            source=rr.SOURCE_OFFICIAL_RELEASE, backend="cuda12", platform="linux-x86_64",
            evidence=rr.EVIDENCE_SPEC, evidence_note="Sans empreinte.",
        ),
        variant(rr.SOURCE_LOCAL_BUILD),
    ))
    resolution = resolve(nvidia_profile(), policy)
    assert resolution.variant.source == rr.SOURCE_LOCAL_BUILD
    assert any("non épinglé" in r for r in resolution.rejected)


def test_unpinned_container_image_is_skipped():
    policy = make_policy((
        rr.ArtifactVariant(
            source=rr.SOURCE_OFFICIAL_CONTAINER, backend="cuda12", platform="linux-x86_64",
            evidence=rr.EVIDENCE_SPEC, evidence_note="Tag mouvant.",
        ),
    ), allow_container=True)
    resolution = resolve(nvidia_profile(), policy)
    assert resolution.resolved is False
    assert any("digest" in r for r in resolution.rejected)


def test_backend_preference_order_is_honoured():
    profile = rr.HardwareProfile(
        platform="linux-x86_64", backend_candidates=("cuda13", "cuda12", "cpu"),
        gpu_vendor="nvidia", gpu_count=1,
    )
    policy = make_policy((variant(rr.SOURCE_LOCAL_BUILD, "cuda12"), variant(rr.SOURCE_LOCAL_BUILD, "cuda13")))
    assert resolve(profile, policy).backend == "cuda13"


def test_source_order_outranks_backend_preference():
    """
    §6 ordonne les SOURCES ; l'inventaire ordonne les backends. Entre les deux,
    c'est la source qui prime : une archive officielle vérifiée pour le second
    backend préféré vaut mieux qu'un build local pour le premier.
    """
    profile = rr.HardwareProfile(
        platform="linux-x86_64", backend_candidates=("cuda13", "cuda12"),
        gpu_vendor="nvidia", gpu_count=1,
    )
    policy = make_policy((
        variant(rr.SOURCE_LOCAL_BUILD, "cuda13"),
        variant(rr.SOURCE_OFFICIAL_RELEASE, "cuda12"),
    ))
    resolution = resolve(profile, policy)
    assert resolution.variant.source == rr.SOURCE_OFFICIAL_RELEASE
    assert resolution.backend == "cuda12"


def test_unknown_platform_yields_an_explicit_refusal():
    profile = rr.HardwareProfile(platform="plan9-x86_64", backend_candidates=("cpu",))
    resolution = resolve(profile, make_policy(rr.DEFAULT_VARIANTS))
    assert resolution.resolved is False
    assert "runtime_unresolved" in codes(resolution)


def test_empty_backend_candidates_is_a_blocker_not_a_cpu_guess():
    profile = rr.HardwareProfile(platform="linux-x86_64", backend_candidates=())
    resolution = resolve(profile, make_policy(rr.DEFAULT_VARIANTS))
    assert resolution.resolved is False
    assert "no_backend_candidate" in codes(resolution)


# ── Aucun repli CPU silencieux — cœur du critère d'acceptation ────────────────

def test_cpu_fallback_is_refused_by_default_when_a_gpu_was_targeted():
    policy = make_policy((variant(rr.SOURCE_LOCAL_BUILD, "cpu"),))
    resolution = resolve(nvidia_profile(), policy)
    assert resolution.resolved is False
    assert resolution.degraded is False
    assert "cpu_fallback_refused" in codes(resolution)
    assert resolution.status == "fail"


def test_explicit_cpu_fallback_is_marked_degraded():
    policy = make_policy((variant(rr.SOURCE_LOCAL_BUILD, "cpu"),), allow_cpu_fallback=True)
    resolution = resolve(nvidia_profile(), policy)
    assert resolution.resolved is True
    assert resolution.backend == "cpu"
    assert resolution.degraded is True, "un CPU retenu face à un GPU visé DOIT être marqué dégradé"


def test_degraded_fallback_carries_a_warning_that_says_what_is_lost():
    """Si ce marquage disparaît, le repli redevient silencieux : le test doit rougir."""
    policy = make_policy((variant(rr.SOURCE_LOCAL_BUILD, "cpu"),), allow_cpu_fallback=True)
    resolution = resolve(nvidia_profile(), policy)

    fallback = [f for f in resolution.findings if f.code == "cpu_fallback_degraded"]
    assert fallback, "le repli CPU dégradé doit produire un constat cpu_fallback_degraded"
    assert fallback[0].level in ("warn", "fail")
    assert "TTFT" in fallback[0].message
    assert resolution.status in ("warn", "fail")
    # Visible sans lire les constats : résumé, section et étape le disent aussi.
    assert "DÉGRADÉ" in resolution.summary
    assert rr.to_plan_section(resolution).data["degraded"] is True
    assert any("dégradation assumée" in step.detail for step in rr.to_plan_steps(resolution))
    assert "repli CPU assumé" in rr.to_decision(resolution).rationale


def test_cpu_only_host_is_not_reported_as_degraded():
    """Contrôle positif inclus : la même assertion voit bien le cas dégradé ailleurs."""
    policy = make_policy((variant(rr.SOURCE_LOCAL_BUILD, "cpu"),), allow_cpu_fallback=True)
    plain = resolve(cpu_profile(), policy)
    assert plain.resolved is True and plain.backend == "cpu"
    assert plain.degraded is False
    assert "cpu_fallback_degraded" not in codes(plain)

    # Contrôle positif : sur un hôte GPU, la même sonde trouve bien le constat.
    degraded = resolve(nvidia_profile(), policy)
    assert "cpu_fallback_degraded" in codes(degraded)


def test_gpu_backend_listed_without_any_gpu_is_not_a_degradation():
    """Un candidat GPU sans GPU exposé est du bruit d'inventaire, pas une cible."""
    profile = rr.HardwareProfile(
        platform="linux-x86_64", backend_candidates=("vulkan", "cpu"), gpu_count=0,
    )
    resolution = resolve(profile, make_policy((variant(rr.SOURCE_LOCAL_BUILD, "cpu"),)))
    assert resolution.resolved is True
    assert resolution.degraded is False
    assert resolution.targeted_backend is None


def test_gpu_search_covers_every_source_before_cpu_is_considered():
    """Une archive CPU officielle ne doit jamais battre une image GPU officielle."""
    policy = make_policy((
        variant(rr.SOURCE_OFFICIAL_RELEASE, "cpu"),
        variant(rr.SOURCE_LOCAL_BUILD, "cuda12"),
    ), allow_cpu_fallback=True)
    resolution = resolve(nvidia_profile(), policy)
    assert resolution.backend == "cuda12"
    assert resolution.degraded is False


# ── LLAMA_SERVER_MIN_BUILD appliqué au binaire présent ────────────────────────

def test_existing_binary_above_floor_is_kept_when_its_manifest_matches():
    resolution = resolve(
        nvidia_profile(), make_policy((variant(rr.SOURCE_LOCAL_BUILD),)),
        existing_binary=Path("/usr/local/bin/llama-server"),
        existing_manifest=posed_manifest(),
        existing_binary_sha256=SHA_BINARY,
        probe=probing(6750, "version: 6750 (0123456)", commit="0123456"),
        digest=digesting(SHA_BINARY),
    )
    assert resolution.resolved is True
    assert resolution.reuse_existing is True
    assert resolution.observed_build == 6750
    assert resolution.status == "ok"
    assert rr.to_plan_steps(resolution) == ()


def test_existing_binary_below_floor_is_replaced():
    resolution = resolve(
        nvidia_profile(), make_policy((variant(rr.SOURCE_LOCAL_BUILD),)),
        existing_binary=Path("/usr/local/bin/llama-server"),
        probe=probing(6500, "version: 6500 (abc1234)"),
    )
    assert resolution.reuse_existing is False
    assert resolution.resolved is True
    assert "llama_server_build_too_old" in codes(resolution)
    assert "llama_server_replaced" in codes(resolution)


def test_unreadable_version_with_an_enforced_floor_fails_closed():
    resolution = resolve(
        nvidia_profile(), make_policy((variant(rr.SOURCE_LOCAL_BUILD),)),
        existing_binary=Path("/usr/local/bin/llama-server"),
        probe=probing(None, "<binaire injoignable>"),
    )
    assert resolution.resolved is False
    assert resolution.status == "fail"
    finding = next(f for f in resolution.findings if f.code == "llama_server_version_unreadable")
    assert finding.level == "fail"
    assert resolution.variant is None


def test_unreadable_version_without_a_floor_only_warns():
    resolution = resolve(
        nvidia_profile(),
        make_policy((variant(rr.SOURCE_LOCAL_BUILD),), release=make_release(floor=0)),
        existing_binary=Path("/usr/local/bin/llama-server"),
        probe=probing(None, "<sortie inconnue>"),
    )
    assert resolution.resolved is True
    finding = next(f for f in resolution.findings if f.code == "llama_server_version_unreadable")
    assert finding.level == "warn"


def test_existing_binary_without_manifest_is_not_kept_on_a_gpu_host():
    """Sans manifeste, rien ne prouve que le binaire en place soit un binaire GPU."""
    resolution = resolve(
        nvidia_profile(), make_policy((variant(rr.SOURCE_LOCAL_BUILD),)),
        existing_binary=Path("/usr/local/bin/llama-server"),
        probe=probing(6900, "version: 6900 (abc1234)"),
    )
    assert resolution.reuse_existing is False
    assert "runtime_provenance_unknown" in codes(resolution)


# ── SEC-009 volet b : le manifeste recoupé contre le binaire ──────────────────
#
# Avant SEC-009, `_judge_existing_binary` accordait `reuse_existing` sur la foi
# d'un manifeste qu'il ne confrontait jamais au binaire : ni la version, ni le
# commit, ni aucune empreinte. Un manifeste recopié d'un autre hôte, ou survivant
# à un remplacement manuel du binaire, valait attestation de provenance.

def test_manifest_declaring_another_build_is_not_an_attestation():
    """Manifeste b6750 posé à côté d'un binaire qui rend 6900 : il décrit autre chose."""
    resolution = resolve(
        nvidia_profile(), make_policy((variant(rr.SOURCE_LOCAL_BUILD),)),
        existing_binary=Path("/usr/local/bin/llama-server"),
        existing_manifest=posed_manifest(),
        existing_binary_sha256=SHA_BINARY,
        probe=probing(6900, "version: 6900 (0123456)", commit="0123456"),
        digest=digesting(SHA_BINARY),
    )
    assert resolution.reuse_existing is False
    assert "runtime_manifest_build_mismatch" in codes(resolution)


def test_manifest_declaring_another_commit_is_not_an_attestation():
    """Même numéro de build, révision différente : l'étiquette ment."""
    resolution = resolve(
        nvidia_profile(), make_policy((variant(rr.SOURCE_LOCAL_BUILD),)),
        existing_binary=Path("/usr/local/bin/llama-server"),
        existing_manifest=posed_manifest(),
        existing_binary_sha256=SHA_BINARY,
        probe=probing(6750, "version: 6750 (fedcba9)", commit="fedcba9"),
        digest=digesting(SHA_BINARY),
    )
    assert resolution.reuse_existing is False
    assert "runtime_manifest_commit_mismatch" in codes(resolution)


def test_short_commit_is_accepted_as_a_prefix_of_the_declared_one():
    """
    Contrôle positif du test précédent : `--version` rend un SHA court, le
    manifeste un SHA long. Sans cette tolérance, le recoupement refuserait TOUT
    binaire sain et l'item serait « fermé » par un refus systématique.
    """
    resolution = resolve(
        nvidia_profile(), make_policy((variant(rr.SOURCE_LOCAL_BUILD),)),
        existing_binary=Path("/usr/local/bin/llama-server"),
        existing_manifest=posed_manifest(),
        existing_binary_sha256=SHA_BINARY,
        probe=probing(6750, "version: 6750 (0123456)", commit="0123456"),
        digest=digesting(SHA_BINARY),
    )
    assert resolution.reuse_existing is True
    assert "runtime_manifest_commit_mismatch" not in codes(resolution)


def test_unreadable_commit_does_not_block_a_coherent_binary():
    """`--version` ne rend pas toujours le commit : on ne peut pas recouper, on n'invente pas."""
    resolution = resolve(
        nvidia_profile(), make_policy((variant(rr.SOURCE_LOCAL_BUILD),)),
        existing_binary=Path("/usr/local/bin/llama-server"),
        existing_manifest=posed_manifest(),
        existing_binary_sha256=SHA_BINARY,
        probe=probing(6750, "version: 6750"),
        digest=digesting(SHA_BINARY),
    )
    assert resolution.reuse_existing is True


def test_manifest_without_a_binary_fingerprint_is_not_an_attestation():
    """Version et commit concordent, mais rien ne distingue ce binaire d'un autre."""
    resolution = resolve(
        nvidia_profile(), make_policy((variant(rr.SOURCE_LOCAL_BUILD),)),
        existing_binary=Path("/usr/local/bin/llama-server"),
        existing_manifest=posed_manifest(),
        probe=probing(6750, "version: 6750 (0123456)", commit="0123456"),
        digest=digesting(SHA_BINARY),
    )
    assert resolution.reuse_existing is False
    assert "runtime_binary_unattested" in codes(resolution)


def test_replaced_binary_under_a_surviving_manifest_is_detected():
    """Le scénario nommé par SEC-009 : le manifeste survit au remplacement du binaire."""
    resolution = resolve(
        nvidia_profile(), make_policy((variant(rr.SOURCE_LOCAL_BUILD),)),
        existing_binary=Path("/usr/local/bin/llama-server"),
        existing_manifest=posed_manifest(),
        existing_binary_sha256=SHA_BINARY,
        probe=probing(6750, "version: 6750 (0123456)", commit="0123456"),
        digest=digesting(SHA_OTHER),
    )
    assert resolution.reuse_existing is False
    finding = next(f for f in resolution.findings if f.code == "runtime_binary_tampered")
    assert "remplacé ou altéré" in finding.message


def test_unreadable_binary_refuses_the_attestation():
    resolution = resolve(
        nvidia_profile(), make_policy((variant(rr.SOURCE_LOCAL_BUILD),)),
        existing_binary=Path("/usr/local/bin/llama-server"),
        existing_manifest=posed_manifest(),
        existing_binary_sha256=SHA_BINARY,
        probe=probing(6750, "version: 6750 (0123456)", commit="0123456"),
        digest=digesting(None),
    )
    assert resolution.reuse_existing is False
    assert "runtime_binary_unreadable" in codes(resolution)


def test_binary_is_not_hashed_when_no_manifest_is_offered():
    """
    Sans manifeste, il n'y a rien à recouper : lire des centaines de Mo ne
    prouverait rien. Le test échoue si le résolveur hache quand même.
    """
    appels: list[Path] = []

    async def _digest(binary: Path) -> str | None:
        appels.append(binary)
        return SHA_BINARY

    resolution = resolve(
        cpu_profile(), make_policy((variant(rr.SOURCE_LOCAL_BUILD, "cpu"),)),
        existing_binary=Path("/usr/local/bin/llama-server"),
        probe=probing(6900, "version: 6900 (0123456)", commit="0123456"),
        digest=_digest,
    )
    assert appels == []
    # Contrôle positif : la sonde de digest est bien appelée quand un manifeste existe.
    resolve(
        nvidia_profile(), make_policy((variant(rr.SOURCE_LOCAL_BUILD),)),
        existing_binary=Path("/usr/local/bin/llama-server"),
        existing_manifest=posed_manifest(),
        existing_binary_sha256=SHA_BINARY,
        probe=probing(6750, "version: 6750 (0123456)", commit="0123456"),
        digest=_digest,
    )
    assert appels == [Path("/usr/local/bin/llama-server")]
    # Le binaire sans manifeste reste conservé sur un hôte CPU, mais sa provenance
    # est signalée inconnue : l'absence de recoupement est dite, pas dissimulée.
    assert resolution.reuse_existing is True
    assert "runtime_provenance_unknown" in codes(resolution)


def test_attested_binary_sha256_reads_the_installer_block():
    document = {
        "runtime": posed_manifest().to_dict(),
        "install": {"binary_sha256": SHA_BINARY, "binary": "/opt/llama/llama-server"},
    }
    assert rr.attested_binary_sha256(document) == SHA_BINARY


@pytest.mark.parametrize(
    "document",
    [
        {"runtime": {}},                                  # aucun bloc install
        {"runtime": {}, "install": {}},                    # bloc vide
        {"runtime": {}, "install": {"binary_sha256": ""}},  # valeur vide
        {"runtime": {}, "install": {"binary_sha256": "pas-une-empreinte"}},
        {"runtime": {}, "install": "not-a-mapping"},
        "pas un document",
    ],
)
def test_attested_binary_sha256_refuses_anything_that_is_not_an_attestation(document):
    assert rr.attested_binary_sha256(document) is None


def test_sha256_binary_reads_a_real_file(tmp_path):
    """Le calcul par défaut n'est pas un bouchon : il lit vraiment le fichier."""
    import hashlib

    binaire = tmp_path / "llama-server"
    binaire.write_bytes(b"ELF" + b"\x00" * 4096)
    attendu = hashlib.sha256(binaire.read_bytes()).hexdigest()
    assert asyncio.run(rr.sha256_binary(binaire)) == attendu
    assert asyncio.run(rr.sha256_binary(tmp_path / "absent")) is None


# ── COR-021 : les invariants ne sont plus portés par des `assert` ─────────────

def test_build_number_of_an_incoherent_version_raises_a_named_error():
    """
    Un `assert` disparaîtrait sous `python -O` et `int(None.group(1))` lèverait un
    `AttributeError` opaque. On force l'état incohérent en contournant le gel du
    dataclass, comme le ferait une désérialisation exotique.
    """
    manifest = posed_manifest()
    object.__setattr__(manifest, "version", "pas-un-tag")
    with pytest.raises(rr.ProvenanceError, match="bNNNNN"):
        _ = manifest.build_number
    # Contrôle positif : la propriété sait aussi rendre un numéro.
    assert posed_manifest().build_number == 6750


def test_decision_without_a_variant_raises_a_named_error():
    resolution = rr.RuntimeResolution(
        profile=nvidia_profile(), min_build=6700, resolved=True, reuse_existing=False,
        degraded=False, targeted_backend="cuda12", variant=None, manifest=None,
        observed_build=None, summary="", findings=(), rejected=(),
    )
    with pytest.raises(rr.ProvenanceError, match="sans variante"):
        rr.to_decision(resolution)
    # Contrôle positif : la même fonction produit bien une décision quand elle peut.
    saine = resolve(nvidia_profile(), make_policy((variant(rr.SOURCE_LOCAL_BUILD),)))
    assert rr.to_decision(saine).choice


def test_manifest_announcing_a_foreign_backend_forces_a_replacement():
    manifest = rr.ProvenanceManifest(
        version="b6750", commit=COMMIT, source=rr.SOURCE_LOCAL_BUILD, backend="cpu",
        platform="linux-x86_64", installed_at=NOW, build_options={"LLAMA_BUILD_SERVER": True},
    )
    profile = rr.HardwareProfile(
        platform="linux-x86_64", backend_candidates=("cuda12",), gpu_vendor="nvidia", gpu_count=1,
    )
    resolution = resolve(
        profile, make_policy((variant(rr.SOURCE_LOCAL_BUILD),)),
        existing_binary=Path("/usr/local/bin/llama-server"),
        existing_manifest=manifest,
        probe=probing(6750, "version: 6750"),
    )
    assert resolution.reuse_existing is False
    assert "runtime_backend_mismatch" in codes(resolution)


# ── Clone superficiel (§0.10) ─────────────────────────────────────────────────

def test_shallow_clone_signature_names_its_real_cause():
    resolution = resolve(
        nvidia_profile(), make_policy((variant(rr.SOURCE_LOCAL_BUILD),)),
        existing_binary=Path("/usr/local/bin/llama-server"),
        probe=probing(1, "version: 1 (unknown)"),
    )
    assert resolution.resolved is False
    finding = next(f for f in resolution.findings if f.code == "llama_server_shallow_clone")
    assert finding.level == "fail"
    assert "--depth 1" in finding.message
    assert "unshallow" in finding.message
    # Le message ne doit PAS se contenter de « version trop ancienne ».
    assert "llama_server_build_too_old" not in codes(resolution)


def test_shallow_clone_without_a_floor_still_warns():
    resolution = resolve(
        nvidia_profile(),
        make_policy((variant(rr.SOURCE_LOCAL_BUILD),), release=make_release(floor=0)),
        existing_binary=Path("/usr/local/bin/llama-server"),
        probe=probing(1, "version: 1 (unknown)"),
    )
    finding = next(f for f in resolution.findings if f.code == "llama_server_shallow_clone")
    assert finding.level == "warn"
    assert resolution.resolved is True


def test_install_step_warns_against_a_shallow_clone():
    resolution = resolve(nvidia_profile(), make_policy((variant(rr.SOURCE_LOCAL_BUILD),)))
    install = next(s for s in rr.to_plan_steps(resolution) if s.action == schema.ACTION_INSTALL_RUNTIME)
    assert "profondeur complète" in install.detail


# ── Projection du profil matériel (§5) ────────────────────────────────────────

def test_inventory_mapping_projects_trivially():
    profile = rr.hardware_profile_from_mapping({
        "os": "ubuntu",
        "os_version": "24.04",
        "arch": "x86_64",
        "cpu_flags": ["avx2"],
        "ram_total_bytes": 0,
        "gpus": [{
            "uuid": "GPU-abc", "vendor": "NVIDIA", "model": "L40S",
            "vram_total_bytes": 48318382080, "driver_version": "580.65.06",
            "compute_capability": "8.9",
        }],
        "backend_candidates": ["cuda12", "vulkan", "cpu"],
    })
    assert profile.platform == "linux-x86_64"
    assert profile.backend_candidates == ("cuda12", "vulkan", "cpu")
    assert profile.gpu_vendor == "nvidia"
    assert profile.driver_version == "580.65.06"
    assert profile.cuda_major == 12
    assert profile.gpu_count == 1
    assert profile.targeted_backend == "cuda12"


def test_inventory_mapping_survives_a_partial_profile():
    profile = rr.hardware_profile_from_mapping({"os": "debian", "arch": "aarch64"})
    assert profile.platform == "linux-arm64"
    assert profile.backend_candidates == ()
    assert profile.gpu_vendor is None and profile.gpu_count == 0
    assert profile.targeted_backend is None


@pytest.mark.parametrize("os_name,arch,expected", [
    ("ubuntu", "amd64", "linux-x86_64"),
    ("Debian", "x86_64", "linux-x86_64"),
    ("darwin", "arm64", "macos-arm64"),
    ("windows", "AMD64", "windows-x86_64"),
    ("plan9", "riscv64", "plan9-riscv64"),
])
def test_platform_normalization(os_name, arch, expected):
    assert rr.normalize_platform(os_name, arch) == expected


# ── Matrice par défaut : ce qu'elle affirme et ce qu'elle suppose ─────────────

def test_default_matrix_has_no_native_cuda_archive_for_linux():
    """§6 : les archives natives de release Linux ne couvrent pas nécessairement CUDA."""
    native_cuda = [
        v for v in rr.DEFAULT_VARIANTS
        if v.source == rr.SOURCE_OFFICIAL_RELEASE
        and v.backend in ("cuda12", "cuda13")
        and v.platform.startswith("linux-")
    ]
    assert native_cuda == []
    # Contrôle positif : la matrice contient bien des entrées official-release.
    assert [v for v in rr.DEFAULT_VARIANTS if v.source == rr.SOURCE_OFFICIAL_RELEASE]


def test_default_matrix_publishes_cuda_containers():
    images = {
        v.backend: v for v in rr.DEFAULT_VARIANTS
        if v.source == rr.SOURCE_OFFICIAL_CONTAINER and v.evidence == rr.EVIDENCE_SPEC
    }
    assert {"cuda12", "cuda13"} <= set(images)
    assert "server-cuda" in images["cuda12"].reference


def test_default_matrix_publishes_no_evaruntime_artifact_yet():
    """§6 présente la matrice EVARuntime comme un objectif, pas comme un existant."""
    assert [v for v in rr.DEFAULT_VARIANTS if v.source == rr.SOURCE_EVARUNTIME_BUILD] == []
    # Contrôle positif : d'autres sources sont bien peuplées.
    assert [v for v in rr.DEFAULT_VARIANTS if v.source == rr.SOURCE_LOCAL_BUILD]


def test_every_default_variant_declares_its_evidence_level():
    for entry in rr.DEFAULT_VARIANTS:
        assert entry.evidence in (rr.EVIDENCE_SPEC, rr.EVIDENCE_ASSUMPTION)
        assert entry.evidence_note.strip()
    assert any(v.evidence == rr.EVIDENCE_ASSUMPTION for v in rr.DEFAULT_VARIANTS)
    assert any(v.evidence == rr.EVIDENCE_SPEC for v in rr.DEFAULT_VARIANTS)


def test_a_variant_chosen_on_an_assumption_is_flagged():
    policy = make_policy((
        variant(rr.SOURCE_LOCAL_BUILD, "cpu", evidence=rr.EVIDENCE_ASSUMPTION,
                evidence_note="Supposition de test."),
    ))
    resolution = resolve(cpu_profile(), policy)
    assert "variant_evidence_assumed" in codes(resolution)
    # Contrôle positif : un constat vérifié ne déclenche pas ce drapeau.
    verified = resolve(cpu_profile(), make_policy((variant(rr.SOURCE_LOCAL_BUILD, "cpu"),)))
    assert "variant_evidence_assumed" not in codes(verified)


def test_default_policy_on_a_linux_nvidia_host_falls_through_to_local_build():
    """Rien n'étant épinglé dans ce dépôt, seul le build local reste éligible."""
    resolution = resolve(nvidia_profile(), rr.ResolverPolicy(release=make_release()))
    assert resolution.variant.source == rr.SOURCE_LOCAL_BUILD
    assert resolution.backend == "cuda12"
    assert any("allow_container=False" in r for r in resolution.rejected)


def test_variant_rejects_an_unknown_evidence_level():
    with pytest.raises(rr.ProvenanceError, match="evidence"):
        rr.ArtifactVariant(
            source=rr.SOURCE_LOCAL_BUILD, backend="cpu", platform="linux-x86_64",
            evidence="parce que", evidence_note="…",
        )


# ── Étapes du plan ────────────────────────────────────────────────────────────

def test_une_archive_epinglee_ne_produit_que_l_etape_qui_verifie_vraiment():
    """
    COR-030 — l'étape `verify_artifact` du domaine runtime n'est plus émise.

    Elle précédait `install_runtime` alors que l'archive n'est pas encore
    téléchargée à ce numéro d'étape : elle ne pouvait rien vérifier,
    `applier._verify_dispatcher` la sautait, et `install_report` en tirait — à
    juste titre — que la condition n°1 de M2 n'était pas prouvée.

    L'empreinte attendue ne disparaît pas du plan pour autant : elle est
    désormais portée par l'étape qui la confronte réellement.
    """
    resolution = resolve(nvidia_profile(), make_policy((variant(rr.SOURCE_OFFICIAL_RELEASE),)))
    steps = rr.to_plan_steps(resolution)

    assert [s.action for s in steps] == [schema.ACTION_INSTALL_RUNTIME]
    assert SHA_A in steps[0].detail, "l'empreinte épinglée a disparu du plan"
    assert steps[-1].requires_root is True


def test_l_etape_de_verification_reste_disponible_mais_sur_demande():
    """
    `include_verify` survit comme opt-in, pour un appelant qui poserait l'archive
    en amont. Aucun ne le fait aujourd'hui — et c'est bien pour cela que le défaut
    ne se voyait qu'à l'exécution du parcours complet.
    """
    resolution = resolve(nvidia_profile(), make_policy((variant(rr.SOURCE_OFFICIAL_RELEASE),)))
    steps = rr.to_plan_steps(resolution, include_verify=True)

    # Quand elle est demandée, la vérification précède toujours l'installation.
    assert [s.action for s in steps] == [
        schema.ACTION_VERIFY_ARTIFACT, schema.ACTION_INSTALL_RUNTIME,
    ]


def test_aucun_appelant_du_depot_ne_demande_l_etape_de_verification():
    """
    Contrôle de non-retour : réactiver le défaut demanderait un appel explicite.

    Le paramètre était mort avec un défaut à `True` ; il est mort avec un défaut
    à `False`. La différence est qu'il ne produit plus une étape que l'exécuteur
    saute et que le rapport compte comme une preuve manquante.
    """

    assert inspect.signature(rr.to_plan_steps).parameters["include_verify"].default is False

    paquet = Path(rr.__file__).parent
    for fichier in paquet.glob("*.py"):
        source = fichier.read_text(encoding="utf-8")
        assert "include_verify=True" not in source, fichier.name
    # Contrôle positif : la recherche voit bien les appels à `to_plan_steps`.
    assert any(
        "to_plan_steps(" in f.read_text(encoding="utf-8")
        for f in paquet.glob("*.py")
        if f.name != "runtime_resolver.py"
    )


def test_steps_are_renumbered_from_start_order():
    resolution = resolve(nvidia_profile(), make_policy((variant(rr.SOURCE_OFFICIAL_RELEASE),)))
    assert [s.order for s in rr.to_plan_steps(resolution, start_order=7)] == [7]
    assert [
        s.order for s in rr.to_plan_steps(resolution, start_order=7, include_verify=True)
    ] == [7, 8]


def test_local_build_has_nothing_to_verify_yet():
    resolution = resolve(nvidia_profile(), make_policy((variant(rr.SOURCE_LOCAL_BUILD),)))
    steps = rr.to_plan_steps(resolution)
    assert [s.action for s in steps] == [schema.ACTION_INSTALL_RUNTIME]


def test_container_step_pins_the_digest():
    policy = make_policy((variant(rr.SOURCE_OFFICIAL_CONTAINER),), allow_container=True)
    steps = rr.to_plan_steps(resolve(nvidia_profile(), policy))
    assert DIGEST in steps[0].detail


# ── Non-divulgation et isolation ──────────────────────────────────────────────

def test_plan_section_data_leaks_no_secret():
    for resolution in (
        resolve(nvidia_profile(), make_policy((variant(rr.SOURCE_OFFICIAL_RELEASE),))),
        resolve(nvidia_profile(), make_policy((variant(rr.SOURCE_LOCAL_BUILD, "cpu"),))),
        resolve(cpu_profile(), rr.ResolverPolicy(release=make_release())),
    ):
        assert schema.find_secret_leaks(rr.to_plan_section(resolution).to_dict()) == ()

    # Contrôle positif : le détecteur voit bien quelque chose quand il y a quoi voir.
    poisoned = rr.to_plan_section(
        resolve(cpu_profile(), rr.ResolverPolicy(release=make_release()))
    ).to_dict()
    poisoned["data"]["reference"] = "https://user:motdepasse@example.invalid/llama.tar.gz"
    assert schema.find_secret_leaks(poisoned)


def test_resolver_imports_nothing_that_could_reach_the_network_or_build():
    imports = rr.module_toplevel_imports()
    assert imports & rr.FORBIDDEN_IMPORTS == frozenset()
    assert imports & rr.FORBIDDEN_SIBLING_IMPORTS == frozenset()
    # Contrôle positif : l'analyse voit réellement les imports du module, y
    # compris le seul frère qu'il s'autorise — un contrat, pas un pair.
    assert {"yaml", "llama_version", "re", "ast", f"{rr.PACKAGE}.schema"} <= imports


# ── TST-007 — le garde-fou lui-même est-il capable d'échouer ? ────────────────
#
# L'analyse statique filtrait les imports sur `node.level == 0` : `from . import
# public_https` lui était invisible. Le garde-fou aurait donc laissé passer
# exactement l'ajout qu'il existe pour empêcher. Les tests qui suivent le
# prouvent par mutation : on injecte l'import interdit dans une copie du module
# et on exige que l'analyse le voie.

def _mutant(tmp_path: Path, ligne: str) -> Path:
    """Copie du résolveur avec une ligne d'import ajoutée juste après la sienne."""
    source = Path(rr.__file__).read_text(encoding="utf-8")
    ancre = "from . import schema"
    assert ancre in source, "point d'injection introuvable : le module a changé d'import"
    chemin = tmp_path / "resolveur_mute.py"
    chemin.write_text(source.replace(ancre, f"{ancre}\n{ligne}", 1), encoding="utf-8")
    return chemin


@pytest.mark.parametrize("ligne", [
    "from . import public_https",
    "from .public_https import fetch_json",
    "from bootstrap import public_https",
    "import bootstrap.public_https",
])
def test_le_garde_fou_voit_un_frere_reseau_quelle_que_soit_la_syntaxe(tmp_path, ligne):
    """
    Les quatre écritures d'un même import doivent toutes rendre le garde-fou rouge.

    Trois d'entre elles lui échappaient : les deux relatives par le filtre
    `node.level == 0`, et `from bootstrap import public_https` parce qu'elle ne
    rendait que « bootstrap », interdit nulle part.
    """
    vus = rr.module_toplevel_imports(_mutant(tmp_path, ligne))

    assert f"{rr.PACKAGE}.public_https" in vus, vus
    assert vus & rr.FORBIDDEN_SIBLING_IMPORTS, "le garde-fou reste aveugle à ce frère"


def test_le_garde_fou_voit_un_import_reseau_direct_meme_differe(tmp_path):
    """
    Contrôle symétrique : un `import socket` caché dans un corps de fonction est vu.

    `ast.walk` descend dans tout l'arbre ; s'il ne parcourait que le premier
    niveau, déplacer l'import de trois lignes suffirait à contourner le garde-fou.
    """
    source = Path(rr.__file__).read_text(encoding="utf-8")
    chemin = tmp_path / "resolveur_differe.py"
    chemin.write_text(
        source.replace(
            "async def sha256_binary(path: Path) -> str | None:",
            "async def sha256_binary(path: Path) -> str | None:\n    import socket  # noqa",
            1,
        ),
        encoding="utf-8",
    )

    assert "socket" in rr.module_toplevel_imports(chemin)


def test_l_analyse_ne_confond_pas_un_frere_avec_un_module_du_meme_nom(tmp_path):
    """
    `import schema` (absolu) et `from . import schema` (frère) ne sont pas le même
    module. Les canoniser tous les deux en « schema » rendrait n'importe quelle
    liste d'autorisation ambiguë.
    """
    chemin = tmp_path / "deux_schemas.py"
    chemin.write_text("import schema\nfrom . import schema as s2\n", encoding="utf-8")

    vus = rr.module_toplevel_imports(chemin)
    assert vus == {"schema", f"{rr.PACKAGE}.schema"}


def _atteint_un_interdit() -> frozenset[str]:
    """
    Fermeture transitive « module du paquet qui peut atteindre un FORBIDDEN_IMPORTS ».

    Recalculée depuis les sources plutôt que recopiée : c'est ce qui empêche
    `FORBIDDEN_SIBLING_IMPORTS` de vieillir en silence le jour où un module gagne
    un accès réseau.
    """
    paquet = Path(rr.__file__).parent
    directs: dict[str, frozenset[str]] = {}
    for fichier in paquet.glob("*.py"):
        if fichier.name == "__init__.py":
            continue
        directs[f"{rr.PACKAGE}.{fichier.stem}"] = rr.module_toplevel_imports(fichier)

    atteints = {
        nom for nom, vus in directs.items() if vus & rr.FORBIDDEN_IMPORTS
    }
    change = True
    while change:  # propagation jusqu'au point fixe
        change = False
        for nom, vus in directs.items():
            if nom not in atteints and vus & atteints:
                atteints.add(nom)
                change = True
    return frozenset(atteints)


def test_la_liste_des_freres_interdits_est_complete_et_sans_exces():
    """
    `FORBIDDEN_SIBLING_IMPORTS` doit valoir exactement la fermeture transitive.

    Trop courte, le garde-fou laisse passer ; trop longue, il interdit un module
    inoffensif et l'opérateur finira par contourner la règle plutôt que la lire.
    """
    attendu = _atteint_un_interdit() - {f"{rr.PACKAGE}.{Path(rr.__file__).stem}"}

    # Contrôle positif : le calcul voit bien quelque chose, et voit bien le frère
    # qui a motivé TST-007.
    assert f"{rr.PACKAGE}.public_https" in attendu
    assert len(attendu) >= 5, attendu

    assert attendu == rr.FORBIDDEN_SIBLING_IMPORTS


def test_le_resolveur_lui_meme_n_atteint_aucun_interdit():
    """Le corollaire : le résolveur ne figure pas dans sa propre fermeture."""
    assert f"{rr.PACKAGE}.runtime_resolver" not in _atteint_un_interdit()


def test_version_probe_is_delegated_to_llama_version_not_reimplemented():
    """Réutiliser `llama_version` est une règle du dépôt, pas une préférence."""
    import llama_version

    assert rr.probe_llama_version is llama_version.probe_llama_version
    source = Path(rr.__file__).read_text(encoding="utf-8")
    assert "create_subprocess" not in source


# ══════════════════════════════════════════════════════════════════════════════
# COR-030 — les quatre parcours bout en bout
# ══════════════════════════════════════════════════════════════════════════════
#
# Le correctif retire une étape du plan. Une suppression d'étape est toujours
# suspecte : elle peut supprimer un défaut, ou seulement supprimer le témoin qui
# le signalait. Les quatre parcours ci-dessous tranchent la question en exécutant
# réellement la chaîne — plan → applicateur → journal → rapport d'installation —
# sur les deux axes indépendants :
#
#   axe 1 : l'étape `verify_artifact` est-elle émise ?
#   axe 2 : l'archive servie correspond-elle à l'empreinte épinglée ?
#
# C'est le COUPLE des deux parcours « sans verify » qui fait la démonstration :
# retirer l'étape verdit le parcours réussi SANS verdir le parcours non vérifié.
# Sans lui, le correctif serait indistinguable d'une complaisance.
#
# Aucun réseau, aucun sous-processus : le transport et la sonde `--version` sont
# injectés, comme dans la suite de `runtime_installer`.

_URL_ARCHIVE ="https://releases.example.com/llama-server-b6800-cpu.tar.gz"


def _archive_bytes(contenu: bytes = b"#!/bin/false\nfaux llama-server\n") -> bytes:
    """Archive plausible : le binaire, un objet partagé, une licence."""
    tampon = io.BytesIO()
    with tarfile.open(fileobj=tampon, mode="w:gz") as tar:
        for nom, data, mode in (
            ("llama-server", contenu, 0o755),
            ("libggml.so.0", b"faux objet partage", 0o644),
            ("LICENSE", b"MIT License\n", 0o644),
        ):
            info = tarfile.TarInfo(nom)
            info.size = len(data)
            info.mode = mode
            tar.addfile(info, io.BytesIO(data))
    return tampon.getvalue()


class _TransportInjecte:
    """Sert des octets préparés. Ne joint rien, ne résout rien."""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    async def fetch(self, url: str, destination: Path, *, max_bytes: int) -> int:
        destination.write_bytes(self.payload)
        return len(self.payload)


def _parcours(tmp_path: Path, *, include_verify: bool, archive_conforme: bool):
    """
    Exécute un parcours complet et rend `(rapport_exec, rapport_install)`.

    `archive_conforme=False` fait servir au transport une archive DIFFÉRENTE de
    celle qu'épingle le manifeste : c'est le vrai contrôle d'intégrité de
    `runtime_installer` qui échoue alors, pas une assertion de ce test.
    """
    servi = _archive_bytes() if archive_conforme else _archive_bytes(b"charge utile substituee")
    epingle = hashlib.sha256(_archive_bytes()).hexdigest()

    resolution = resolve(
        cpu_profile(),
        make_policy(
            (variant(rr.SOURCE_OFFICIAL_RELEASE, "cpu", artifact_sha256=epingle),),
            release=rr.ReleasePolicy(
                pinned_version="b6800", pinned_commit=COMMIT, security_floor_build=6700,
            ),
        ),
    )
    assert resolution.resolved and resolution.variant is not None

    steps = rr.to_plan_steps(resolution, include_verify=include_verify)
    plan = schema.BootstrapPlan(
        generated_at=NOW,
        mode="apply",
        sections=(rr.to_plan_section(resolution),),
        steps=steps,
        decisions=(rr.to_decision(resolution),),
    )
    document = plan.to_dict()
    assert schema.validate_plan_dict(document) == ()
    assert document["applicable"] is True

    installateur = ri.RuntimeInstaller(
        ri.RuntimeInstallRequest(
            resolution=resolution,
            archive_url=_URL_ARCHIVE,
            install_root=tmp_path / "runtime",
            limits=ri.ArchiveLimits(),
        ),
        transport=_TransportInjecte(servi),  # type: ignore[arg-type]
        probe=probing(6800, raw="version: 6800 (abc1234)", commit="abc1234"),
    )

    registre = ex.ExecutorRegistry()
    registre.register(schema.ACTION_INSTALL_RUNTIME, installateur)
    registre.register(
        schema.ACTION_VERIFY_ARTIFACT,
        ap._verify_dispatcher(ap.ApplierConfig(runtime=installateur), None),
    )

    ticks = iter(range(0, 100_000))
    contexte = ex.ExecutionContext(
        mode=ex.ExecutionMode.APPLY,
        allowed_roots=(tmp_path,),
        monotonic=lambda: float(next(ticks)) / 1000.0,
        now=lambda: NOW,
    )
    rapport_exec = asyncio.run(ex.execute_plan(
        ex.load_plan_document(json.dumps(document)), registre, contexte,
    ))
    rapport_install = ir.build_install_report(
        plan_document=document, execution_report=rapport_exec, now=lambda: NOW,
    )
    return rapport_exec, rapport_install


def _condition_runtime(rapport: ir.InstallReport) -> ir.ConditionOutcome:
    return next(c for c in rapport.conditions() if c.code == "runtime_installed")


def test_parcours_avec_verify_et_runtime_conforme_reste_insatisfait(tmp_path):
    """
    Le défaut, reproduit tel qu'il se produisait : l'installation RÉUSSIT et la
    condition n°1 du jalon M2 sort quand même insatisfaite.

    L'étape `verify_artifact` ne peut rien vérifier à ce numéro — l'archive n'est
    pas encore téléchargée — donc l'applicateur la saute, et une étape sautée ne
    prouve rien. Ce test reste vert avec le correctif : il passe `include_verify`
    explicitement, et documente ce que le défaut coûtait.
    """
    rapport_exec, rapport = _parcours(tmp_path, include_verify=True, archive_conforme=True)

    statuts = [(r.action, r.status) for r in rapport_exec.results]
    assert statuts == [
        (schema.ACTION_VERIFY_ARTIFACT, ex.STEP_SKIPPED),
        (schema.ACTION_INSTALL_RUNTIME, ex.STEP_DONE),
    ]
    condition = _condition_runtime(rapport)
    assert condition.status == ir.CONDITION_UNSATISFIED
    assert "sautée" in condition.proof
    # Le rayon d'action dépassait le rapport M2 : l'EXÉCUTION elle-même sortait
    # en code 3, et tout script testant le code 0 lisait un demi-échec — alors
    # que la seule étape qui agit avait réussi.
    assert rapport_exec.verdict() == ex.VERDICT_PARTIAL
    assert rapport_exec.exit_code() == ex.EXIT_PARTIAL == 3


def test_parcours_avec_verify_et_runtime_non_verifie_reste_insatisfait(tmp_path):
    """Contrôle : avec l'étape vide, un échec réel reste un échec."""
    rapport_exec, rapport = _parcours(tmp_path, include_verify=True, archive_conforme=False)

    assert rapport_exec.results[-1].status == ex.STEP_FAILED
    assert _condition_runtime(rapport).status == ir.CONDITION_UNSATISFIED


def test_parcours_sans_verify_et_runtime_conforme_est_satisfait(tmp_path):
    """
    COR-030 — le parcours nominal se prononce enfin.

    Une seule étape, celle qui vérifie et installe réellement. La condition n°1
    est satisfaite, l'exécution est `ok`, et `bootstrap-apply` sort en code 0.
    """
    rapport_exec, rapport = _parcours(tmp_path, include_verify=False, archive_conforme=True)

    assert [(r.action, r.status) for r in rapport_exec.results] == [
        (schema.ACTION_INSTALL_RUNTIME, ex.STEP_DONE),
    ]
    assert _condition_runtime(rapport).status == ir.CONDITION_SATISFIED
    assert rapport_exec.verdict() == ex.VERDICT_OK
    assert rapport_exec.exit_code() == ex.EXIT_OK == 0

    # Le rapport d'installation reste `partial`, et c'est correct : ce plan-ci ne
    # pose QUE le runtime, les six autres conditions de M2 n'ont aucune étape pour
    # les prouver. Ce qui est corrigé, c'est la condition n°1 — la seule que ce
    # plan puisse trancher — et le code de sortie de `bootstrap-apply`.
    assert {c.status for c in rapport.conditions() if c.code != "runtime_installed"} <= {
        ir.CONDITION_UNPROVEN, ir.CONDITION_SATISFIED,
    }


def test_parcours_sans_verify_et_runtime_non_verifie_reste_insatisfait(tmp_path):
    """
    Le pendant décisif du précédent, et la preuve que le correctif n'est pas une
    complaisance : retirer l'étape vide NE verdit PAS un runtime non vérifié.

    La détection est portée entièrement par `install_runtime`, qui confronte
    l'empreinte de l'archive avant d'extraire. L'étape `verify_artifact` n'y
    contribuait rien — elle ne faisait que soustraire.
    """
    rapport_exec, rapport = _parcours(tmp_path, include_verify=False, archive_conforme=False)

    assert [(r.action, r.status) for r in rapport_exec.results] == [
        (schema.ACTION_INSTALL_RUNTIME, ex.STEP_FAILED),
    ]
    assert _condition_runtime(rapport).status == ir.CONDITION_UNSATISFIED
    assert rapport_exec.exit_code() != ex.EXIT_OK
    assert rapport.exit_code() != ir.EXIT_OK
    assert rapport.verdict() == ir.VERDICT_FAILED


# ══════════════════════════════════════════════════════════════════════════════
# SEC-017 — relire le manifeste posé à côté du binaire
# ══════════════════════════════════════════════════════════════════════════════
#
# SEC-009 (volet b) a livré le recoupement manifeste ↔ binaire, mais AUCUN
# appelant ne fournissait jamais `existing_manifest` : le chemin corrigé n'était
# exercé que par les tests. Le trou était réel et dormant.
# `read_existing_attestation` est la lecture qui manquait ; ses quatre issues
# sont couvertes ici, et `test_bootstrap_planner.py` prouve qu'elle est empruntée.

def _ecrire_manifeste(binaire: Path, document) -> Path:
    chemin = rr.provenance_path(binaire)
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_text(
        document if isinstance(document, str)
        else yaml.safe_dump(document, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return chemin


def _document_pose(**overrides) -> dict:
    """Document tel que `runtime_installer` le pose : bloc §6 + bloc `install:`."""
    document = posed_manifest().to_document()
    document["install"] = {"installer": "eva-bootstrap", "binary_sha256": SHA_BINARY}
    for cle, valeur in overrides.items():
        document["runtime"][cle] = valeur
    return document


def test_le_nom_du_manifeste_ne_peut_pas_diverger_de_celui_de_l_installateur():
    """
    Le résolveur DÉFINIT le manifeste, l'installateur le POSE. Le nom vit donc
    chez le décideur : le sens de dépendance de `bootstrap/__init__.py` interdit
    l'inverse, et le garde-fou d'isolation (TST-007) le refuserait.

    Deux constantes, une seule vérité : ce test est le lien qui les tient.
    """
    assert rr.MANIFEST_FILENAME == ri.MANIFEST_FILENAME == "provenance.yaml"
    assert rr.provenance_path(Path("/opt/eva/bin/llama-server")) == Path(
        "/opt/eva/bin/provenance.yaml"
    )


def test_un_manifeste_absent_est_un_constat_nomme_et_non_une_attestation(tmp_path):
    """Fail-closed : l'absence de manifeste ne vaut jamais attestation."""
    lu = rr.read_existing_attestation(tmp_path / "llama-server")

    assert lu.manifest is None
    assert lu.binary_sha256 is None
    assert lu.finding is not None
    assert lu.finding.code == "runtime_manifest_absent"
    assert lu.finding.level == "info"
    # Le constat dit OÙ le manifeste était attendu : sans ce chemin, l'opérateur
    # ne sait pas où poser celui qui manque.
    assert str(tmp_path / "provenance.yaml") in lu.finding.message


def test_un_manifeste_illisible_est_un_constat_nomme(tmp_path):
    binaire = tmp_path / "llama-server"
    _ecrire_manifeste(binaire, "runtime: [ceci n'est pas\n  un: yaml valide\n")

    lu = rr.read_existing_attestation(binaire)

    assert lu.manifest is None
    assert lu.finding is not None and lu.finding.code == "runtime_manifest_unreadable"
    assert lu.finding.level == "warn"


def test_un_manifeste_incoherent_est_un_constat_nomme(tmp_path):
    """Un manifeste approximatif vaut moins que pas de manifeste : on lui fait confiance."""
    binaire = tmp_path / "llama-server"
    _ecrire_manifeste(binaire, _document_pose(version="six-mille-huit-cents"))

    lu = rr.read_existing_attestation(binaire)

    assert lu.manifest is None
    assert lu.finding is not None and lu.finding.code == "runtime_manifest_invalid"
    assert "version" in lu.finding.message


def test_un_manifeste_sain_rend_le_manifeste_et_l_empreinte_du_binaire(tmp_path):
    """Contrôle positif de la famille : le cas nominal produit bien une attestation."""
    binaire = tmp_path / "llama-server"
    _ecrire_manifeste(binaire, _document_pose())

    lu = rr.read_existing_attestation(binaire)

    assert lu.finding is None
    assert lu.manifest == posed_manifest()
    assert lu.binary_sha256 == SHA_BINARY


def test_un_manifeste_sans_empreinte_de_binaire_n_atteste_pas(tmp_path):
    """
    Le bloc `install.binary_sha256` rattache le manifeste à CE binaire-ci. Sans
    lui, la lecture réussit mais n'atteste rien, et le recoupement aval refuse la
    réutilisation.
    """
    binaire = tmp_path / "llama-server"
    document = _document_pose()
    del document["install"]["binary_sha256"]
    _ecrire_manifeste(binaire, document)

    lu = rr.read_existing_attestation(binaire)

    assert lu.manifest is not None  # le bloc §6 est cohérent
    assert lu.binary_sha256 is None  # mais rien ne le rattache au binaire
