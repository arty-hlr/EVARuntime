"""
AUT-018 — matrice d'artefacts `llama-server` fournie par l'opérateur.

Ce que ces tests portent
------------------------
Le défaut fermé est double, et les deux moitiés sont testées séparément pour
qu'aucune ne puisse disparaître sans faire rougir :

- l'**échappatoire était inatteignable** : `ResolverPolicy.variants` est
  documentée comme le moyen de fournir des variantes épinglées, et aucune option
  de CLI ne permettait de le faire (même défaut qu'AUT-004) ;
- `variant.reference` de la matrice livrée est **une page de releases**, pas une
  archive : `production.runtime_installer_from_plan` en faisait une URL de
  téléchargement.

Trois invariants sont éprouvés un par un : la validation est **fail-closed** (un
fichier malformé refuse, il ne se replie pas sur la matrice livrée), le fichier
**remplace** la matrice livrée, et une variante fournie porte le niveau de preuve
`constat-opérateur` — jamais celui de §6.

Les tests d'absence (pas de repli, pas de constat parasite, exemple non
chargeable) portent chacun un contrôle positif : sans lui, ils resteraient verts
en devenant aveugles.
"""
from __future__ import annotations

import asyncio
import copy
import json
import re
from pathlib import Path

import pytest
import yaml

from bootstrap import production as prod
from bootstrap import runtime_resolver as rr
from bootstrap import runtime_variants as rv
from bootstrap import schema as sc

SHA = "a" * 64
DIGEST = "sha256:" + "b" * 64
COMMIT = "0123456789abcdef0123456789abcdef01234567"
NOW = "2026-08-03T09:00:00+00:00"

EXAMPLE_PATH = Path(__file__).resolve().parents[1] / "deploy" / "runtime-variants.yaml.example"

# Substitutions appliquées à l'exemple livré pour prouver que sa STRUCTURE est
# chargeable. Elles ne remplacent que ce que le dépôt ne peut pas connaître.
EXAMPLE_SUBSTITUTIONS: tuple[tuple[str, str], ...] = (
    ("REMPLACER-VERSION", "b6800"),
    ("REMPLACER-64-HEX", "a" * 64),
    ("REMPLACER-TAILLE-EN-OCTETS", "148000000"),
    ("REMPLACER-CONSTAT", "Archive telechargee et sha256sum recoupe avec SHA256SUMS amont"),
    ("REMPLACER-DATE", "2026-08-03"),
)


# ── Fabriques ─────────────────────────────────────────────────────────────────

def archive_entry(**overrides) -> dict:
    entry = {
        "source": "official-release",
        "backend": "cuda12",
        "platform": "linux-x86_64",
        "reference": "https://artefacts.example/llama-b6800-cuda12-linux-x64.tar.gz",
        "artifact_sha256": SHA,
        "approx_bytes": 148_000_000,
        "evidence": "Archive téléchargée puis sha256sum recoupé avec la somme publiée.",
        "recorded_on": "2026-08-03",
    }
    entry.update(overrides)
    return {k: v for k, v in entry.items() if v is not ...}


def document(*entries: dict, version: int = rv.VARIANTS_VERSION) -> dict:
    return {"variants_version": version, "variants": list(entries or (archive_entry(),))}


def write(tmp_path: Path, doc) -> Path:
    target = tmp_path / "runtime-variants.yaml"
    target.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return target


def nvidia_profile() -> rr.HardwareProfile:
    return rr.HardwareProfile(
        platform="linux-x86_64",
        backend_candidates=("cuda12", "cpu"),
        gpu_vendor="nvidia",
        driver_version="580.65.06",
        cuda_major=12,
        gpu_count=1,
    )


def resolve(variants: tuple[rr.ArtifactVariant, ...], **kwargs) -> rr.RuntimeResolution:
    policy = rr.ResolverPolicy(
        release=rr.ReleasePolicy(
            pinned_version="b6800", pinned_commit=COMMIT, security_floor_build=6700
        ),
        variants=variants,
        **kwargs,
    )
    return asyncio.run(rr.resolve_runtime(nvidia_profile(), policy, installed_at=NOW))


# ── Le défaut initial : la matrice livrée n'installe rien ─────────────────────

def test_la_matrice_livree_ne_porte_aucune_url_d_archive_exploitable():
    """
    Constat qui motive l'item, vérifié plutôt que supposé.

    Aucune variante par défaut n'est épinglée, et les `reference` des variantes
    d'archive désignent la PAGE de releases du projet. Même munie d'une
    empreinte, la matrice livrée ne donnerait à `runtime_installer` aucune URL
    d'artefact : elle téléchargerait du HTML pour le rejeter au contrôle de SHA.
    """
    assert [v.label() for v in rr.DEFAULT_VARIANTS if v.artifact_sha256 or v.container_digest] == []
    archives = [v for v in rr.DEFAULT_VARIANTS if v.source == rr.SOURCE_OFFICIAL_RELEASE]
    assert archives, "contrôle positif : la matrice livrée contient bien des variantes d'archive"
    for variant in archives:
        with pytest.raises(rv.RuntimeVariantsError, match="ne désigne pas une archive"):
            rv.validate_archive_url(variant.reference, variant.label())


def test_installateur_refuse_la_page_de_releases_de_la_matrice_livree(tmp_path):
    """
    Le contrôle de `production` ne se limitait qu'au schéma et à l'autorité : une
    page de releases le passait. Elle est désormais refusée AVANT tout téléchargement.
    """
    from tests.test_bootstrap_production import _plan_fragment, _resolution

    page = "https://github.com/ggml-org/llama.cpp/releases"
    document_ = _plan_fragment(_resolution(reference=page))
    with pytest.raises(prod.ProductionWiringError, match="ne désigne pas une archive"):
        prod.runtime_installer_from_plan(document_, tmp_path)

    # Contrôle positif : une vraie archive passe toujours, sinon le test
    # précédent ne prouverait que l'existence d'un refus universel.
    installer = prod.runtime_installer_from_plan(_plan_fragment(), tmp_path)
    assert installer.request.archive_url.endswith(".tar.gz")


# ── Chargement nominal ────────────────────────────────────────────────────────

def test_une_matrice_fournie_est_chargee_avec_le_niveau_de_preuve_operateur(tmp_path):
    variants = rv.load_variants(write(tmp_path, document()))
    assert len(variants) == 1
    variant = variants[0]
    assert variant.evidence == rr.EVIDENCE_OPERATOR
    assert variant.is_pinned
    assert "2026-08-03" in variant.evidence_note
    assert "sha256sum" in variant.evidence_note
    assert variant.approx_bytes == 148_000_000


def test_le_fichier_ne_peut_pas_se_reclamer_de_la_specification(tmp_path):
    """
    Une variante fournie est un constat OPÉRATEUR. Le niveau de preuve est imposé
    par le chargeur : §6 ne connaît aucune empreinte, et un fichier qui pourrait
    s'attribuer `constat-§6` annulerait la distinction constat/hypothèse dont
    vivent le plan et le rapport d'installation.
    """
    with pytest.raises(rv.RuntimeVariantsError, match="champs inconnus"):
        rv.parse_variants(
            document(archive_entry(evidence_note="…")), origin="test"
        )
    variants = rv.parse_variants(document(), origin="test")
    assert {v.evidence for v in variants} == {rr.EVIDENCE_OPERATOR}
    assert rr.EVIDENCE_SPEC not in {v.evidence for v in variants}


def test_les_trois_sources_ont_chacune_leur_forme(tmp_path):
    conteneur = {
        "source": "official-container", "backend": "cuda12", "platform": "linux-x86_64",
        "reference": "ghcr.io/ggml-org/llama.cpp:server-cuda", "container_digest": DIGEST,
        "evidence": "Digest relevé avec docker buildx imagetools inspect.",
        "recorded_on": "2026-08-03",
    }
    local = {
        "source": "local-build", "backend": "cuda12", "platform": "linux-arm64",
        "evidence": "Toolchain CUDA 12 vérifiée sur l'hôte de recette.",
        "recorded_on": "2026-08-03",
    }
    variants = rv.parse_variants(document(archive_entry(), conteneur, local), origin="test")
    par_source = {v.source: v for v in variants}
    assert par_source["official-release"].artifact_sha256 == SHA
    assert par_source["official-container"].container_digest == DIGEST
    assert par_source["local-build"].reference == ""
    assert par_source["local-build"].artifact_sha256 is None


# ── Validation stricte, champ par champ ───────────────────────────────────────

@pytest.mark.parametrize(("overrides", "motif"), [
    ({"source": "artefact-maison"}, "source inconnue"),
    ({"backend": "tpu"}, "backend inconnu"),
    ({"platform": "Linux-X86_64"}, "os-arch"),
    ({"platform": "linux"}, "os-arch"),
    ({"artifact_sha256": "A" * 64}, "hexadécimaux minuscules"),
    ({"artifact_sha256": "a" * 63}, "hexadécimaux minuscules"),
    ({"artifact_sha256": None}, "chaîne non vide"),
    ({"evidence": ""}, "chaîne non vide"),
    ({"evidence": None}, "chaîne non vide"),
    ({"recorded_on": "03/08/2026"}, "AAAA-MM-JJ"),
    ({"recorded_on": "2026-02-30"}, "date inexistante"),
    ({"approx_bytes": 0}, "strictement positif"),
    ({"approx_bytes": None}, "strictement positif"),
    ({"approx_bytes": True}, "strictement positif"),
    ({"container_digest": DIGEST}, "champ interdit"),
])
def test_un_champ_invalide_fait_refuser_le_fichier(overrides, motif):
    with pytest.raises(rv.RuntimeVariantsError, match=motif):
        rv.parse_variants(document(archive_entry(**overrides)), origin="test")


@pytest.mark.parametrize(("url", "motif"), [
    ("https://github.com/ggml-org/llama.cpp/releases", "ne désigne pas une archive"),
    ("https://artefacts.example/llama/", "ne désigne pas une archive"),
    ("https://artefacts.example/llama.tar.zst", "ne désigne pas une archive"),
    ("http://artefacts.example/llama.tar.gz", "seul HTTPS"),
    ("https://jeton:secret@artefacts.example/llama.tar.gz", "identifiants"),
    ("https://127.0.0.1/llama.tar.gz", "adresse privée"),
    ("https://localhost/llama.tar.gz", "destination locale"),
    ("https://artefacts.example/llama.tar.gz?X-Amz-Signature=abc", "chaîne de requête"),
    ("https://artefacts.example/llama.tar.gz#sha", "fragment"),
])
def test_une_url_d_artefact_inexploitable_fait_refuser_le_fichier(url, motif):
    with pytest.raises(rv.RuntimeVariantsError, match=motif):
        rv.parse_variants(document(archive_entry(reference=url)), origin="test")


@pytest.mark.parametrize("suffixe", [".zip", ".tar", ".tar.gz", ".tgz", ".tar.xz", ".tar.bz2"])
def test_les_formats_reellement_extractibles_sont_acceptes(suffixe):
    """
    Contrôle positif du test précédent : sans lui, un `validate_archive_url`
    devenu un refus universel le laisserait vert.
    """
    url = f"https://artefacts.example/llama-b6800{suffixe}"
    assert rv.validate_archive_url(url, "test") == url


def test_un_champ_inconnu_est_un_rejet_pas_un_avertissement():
    with pytest.raises(rv.RuntimeVariantsError, match="champs inconnus"):
        rv.parse_variants(document(archive_entry(backend_flags="GGML_CUDA")), origin="test")
    with pytest.raises(rv.RuntimeVariantsError, match="champs inconnus"):
        rv.parse_variants({"variants_version": 1, "variants": [], "notes": "x"}, origin="test")


@pytest.mark.parametrize(("doc", "motif"), [
    ({"variants_version": 2, "variants": [archive_entry()]}, "non supportée"),
    ({"variants": [archive_entry()]}, "variants_version"),
    ({"variants_version": "1", "variants": [archive_entry()]}, "variants_version"),
    ({"variants_version": 1}, "liste non vide"),
    ({"variants_version": 1, "variants": []}, "liste non vide"),
    ({"variants_version": 1, "variants": [None]}, "objet attendu"),
    (["variants"], "objet YAML"),
])
def test_un_document_malforme_est_refuse(doc, motif):
    with pytest.raises(rv.RuntimeVariantsError, match=motif):
        rv.parse_variants(doc, origin="test")


def test_deux_entrees_pour_le_meme_couple_sont_refusees():
    """
    Deux entrées identiques rendraient l'ordre de préférence de §6 dépendant de
    l'ordre d'écriture du fichier : l'opérateur croirait choisir, il tirerait au sort.
    """
    with pytest.raises(rv.RuntimeVariantsError, match="déjà déclaré"):
        rv.parse_variants(
            document(archive_entry(), archive_entry(artifact_sha256="c" * 64)), origin="test"
        )


def test_un_build_local_ne_peut_pas_pretendre_etre_epingle():
    base = {
        "source": "local-build", "backend": "cpu", "platform": "linux-x86_64",
        "evidence": "Toolchain vérifiée.", "recorded_on": "2026-08-03",
    }
    for champ, valeur in (
        ("artifact_sha256", SHA),
        ("reference", "https://artefacts.example/llama.tar.gz"),
        ("approx_bytes", 10),
        ("container_digest", DIGEST),
    ):
        with pytest.raises(rv.RuntimeVariantsError, match="champ interdit"):
            rv.parse_variants(document({**base, champ: valeur}), origin="test")


def test_une_image_ne_s_epingle_que_par_digest():
    base = {
        "source": "official-container", "backend": "cuda12", "platform": "linux-x86_64",
        "reference": "ghcr.io/ggml-org/llama.cpp:server-cuda",
        "evidence": "Digest relevé.", "recorded_on": "2026-08-03",
    }
    with pytest.raises(rv.RuntimeVariantsError, match="sha256:<64 hex"):
        rv.parse_variants(document({**base, "container_digest": SHA}), origin="test")
    with pytest.raises(rv.RuntimeVariantsError, match="champ interdit"):
        rv.parse_variants(
            document({**base, "container_digest": DIGEST, "artifact_sha256": SHA}), origin="test"
        )
    with pytest.raises(rv.RuntimeVariantsError, match="pas par une URL"):
        rv.parse_variants(
            document({**base, "container_digest": DIGEST,
                      "reference": "https://ghcr.io/ggml-org/llama.cpp"}),
            origin="test",
        )


def test_un_fichier_illisible_ou_non_yaml_refuse(tmp_path):
    with pytest.raises(rv.RuntimeVariantsError, match="illisible"):
        rv.load_variants(tmp_path / "absent.yaml")
    casse = tmp_path / "casse.yaml"
    casse.write_text("variants: [\n", encoding="utf-8")
    with pytest.raises(rv.RuntimeVariantsError, match="YAML invalide"):
        rv.load_variants(casse)


# ── Fail-closed : jamais de repli sur la matrice livrée ───────────────────────

def test_un_fichier_refuse_ne_se_replie_jamais_sur_la_matrice_livree(tmp_path):
    """
    Test d'ABSENCE, muni de son contrôle positif : le chargeur ne doit à aucune
    condition rendre `DEFAULT_VARIANTS`. Un repli silencieux ferait installer
    autre chose que ce que l'opérateur a écrit, en le lui cachant.
    """
    mauvais = write(tmp_path, document(archive_entry(artifact_sha256="pas-un-sha")))
    with pytest.raises(rv.RuntimeVariantsError):
        rv.load_variants(mauvais)

    bon = rv.load_variants(write(tmp_path, document()))
    assert bon != rr.DEFAULT_VARIANTS
    assert len(bon) == 1  # contrôle positif : le chargeur rend bien quelque chose


def test_une_entree_valide_ne_sauve_pas_un_fichier_qui_en_contient_une_mauvaise():
    """Le refus porte sur le FICHIER, pas sur l'entrée : pas de lecture partielle."""
    bonne = archive_entry()
    mauvaise = archive_entry(backend="cpu", artifact_sha256="zz" * 32)
    with pytest.raises(rv.RuntimeVariantsError):
        rv.parse_variants(document(bonne, mauvaise), origin="test")


# ── Remplacement, et non ajout ────────────────────────────────────────────────

def test_la_matrice_fournie_remplace_la_matrice_livree(tmp_path):
    """
    Décision de l'item. Une faute de frappe dans `platform` doit rendre le plan
    BRUYANT : en union, le `local-build/cuda12/linux-x86_64` livré l'emporterait
    en silence et l'opérateur lirait un plan réussi qui ignore son épinglage.
    """
    faute = rv.parse_variants(document(archive_entry(platform="linux-amd64")), origin="test")
    resolution = resolve(faute)
    assert resolution.resolved is False
    assert any(f.code == "runtime_unresolved" for f in resolution.findings)

    # Contrôle positif : la même entrée sans la faute de frappe est bien retenue.
    juste = rv.parse_variants(document(), origin="test")
    retenue = resolve(juste)
    assert retenue.resolved is True
    assert retenue.variant is not None
    assert retenue.variant.source == rr.SOURCE_OFFICIAL_RELEASE


def test_avec_la_matrice_livree_la_meme_resolution_retombe_sur_un_build_local():
    """
    Contre-épreuve du test précédent : c'est bien la matrice LIVRÉE qui produit
    l'issue silencieuse qu'on refuse d'hériter par union.
    """
    resolution = resolve(rr.DEFAULT_VARIANTS)
    assert resolution.resolved is True
    assert resolution.variant is not None
    assert resolution.variant.source == rr.SOURCE_LOCAL_BUILD


# ── Ce que le plan doit dire de l'origine des variantes ───────────────────────

def test_le_plan_distingue_matrice_livree_et_matrice_fournie():
    fournie = resolve(rv.parse_variants(document(), origin="test"))
    codes = {f.code for f in fournie.findings}
    assert "runtime_variants_operator_supplied" in codes
    constat = next(f for f in fournie.findings if f.code == "runtime_variants_operator_supplied")
    assert constat.level == "info"
    assert "opérateur" in constat.message

    donnees = fournie.to_data()
    assert donnees["variant"]["evidence"] == rr.EVIDENCE_OPERATOR
    assert "Constat opérateur relevé le 2026-08-03" in donnees["variant"]["evidence_note"]
    assert rr.EVIDENCE_OPERATOR in rr.to_decision(fournie).rationale


def test_la_matrice_livree_n_emet_pas_le_constat_d_origine_operateur():
    """
    Contrôle d'absence : sans lui, un constat émis inconditionnellement rendrait
    le test précédent vide de sens tout en le laissant vert.
    """
    livree = resolve(rr.DEFAULT_VARIANTS)
    assert "runtime_variants_operator_supplied" not in {f.code for f in livree.findings}
    assert livree.findings, "contrôle positif : cette résolution émet bien des constats"


def test_une_variante_operateur_n_est_pas_presentee_comme_une_hypothese():
    """
    `variant_evidence_assumed` est réservé aux entrées `hypothèse-à-confirmer` de
    la matrice livrée. Une empreinte relevée par l'opérateur n'en est pas une :
    la confondre reviendrait à demander de « confirmer » ce qui vient d'être relevé.
    """
    fournie = resolve(rv.parse_variants(document(), origin="test"))
    assert "variant_evidence_assumed" not in {f.code for f in fournie.findings}

    hypothese = rr.ArtifactVariant(
        source=rr.SOURCE_OFFICIAL_RELEASE, backend="cuda12", platform="linux-x86_64",
        evidence=rr.EVIDENCE_ASSUMPTION, evidence_note="supposée publiée.",
        reference="https://artefacts.example/l.tar.gz", artifact_sha256=SHA,
    )
    assert "variant_evidence_assumed" in {f.code for f in resolve((hypothese,)).findings}


# ── Le fichier d'exemple livré ────────────────────────────────────────────────

def test_l_exemple_livre_refuse_de_se_charger_tel_quel():
    """
    Il porte des valeurs invérifiables marquées `REMPLACER`. Le refus les nomme :
    « vous avez chargé l'exemple » est un diagnostic différent de « votre
    empreinte a une faute de frappe ».
    """
    assert EXAMPLE_PATH.exists(), f"exemple absent : {EXAMPLE_PATH}"
    with pytest.raises(rv.RuntimeVariantsError, match=rv.PLACEHOLDER_TOKEN):
        rv.load_variants(EXAMPLE_PATH)


def test_l_exemple_livre_ne_contient_aucune_empreinte_qui_pourrait_passer_pour_reelle():
    """
    Aucun SHA-256 ni digest bien formé ne doit figurer dans l'exemple : recopié
    par mégarde, il ferait échouer une installation avec un message d'intégrité,
    ou pire, passerait pour une valeur relevée.
    """
    texte = EXAMPLE_PATH.read_text(encoding="utf-8")
    assert re.search(r"\b[0-9a-f]{64}\b", texte) is None
    assert re.search(r"sha256:[0-9a-f]{64}", texte) is None
    # Contrôle positif : la recherche voit bien une empreinte quand il y en a une.
    assert re.search(r"\b[0-9a-f]{64}\b", texte + "\n" + SHA) is not None


def test_la_structure_de_l_exemple_livre_est_reellement_chargeable():
    """
    Garde-fou COR-014, à l'envers : le dépôt a déjà livré des fichiers d'exemple
    que rien ne pouvait charger. Ici, SEULES les valeurs invérifiables sont
    substituées ; si une clé, une source ou une forme de l'exemple divergeait du
    chargeur, ce test rougirait.
    """
    texte = EXAMPLE_PATH.read_text(encoding="utf-8")
    for marqueur, valeur in EXAMPLE_SUBSTITUTIONS:
        texte = texte.replace(marqueur, valeur)
    assert rv.PLACEHOLDER_TOKEN not in texte.split("variants_version:")[1]

    variants = rv.parse_variants(yaml.safe_load(texte), origin="exemple substitué")
    assert {v.source for v in variants} == {
        rr.SOURCE_OFFICIAL_RELEASE, rr.SOURCE_OFFICIAL_CONTAINER, rr.SOURCE_LOCAL_BUILD,
    }
    assert all(v.evidence == rr.EVIDENCE_OPERATOR for v in variants)
    assert all(v.is_pinned for v in variants)


def test_l_exemple_livre_montre_qu_un_build_local_doit_etre_redeclare():
    """
    Conséquence directe du remplacement : sans entrée `local-build` dans le
    fichier, cette voie disparaît. L'exemple doit le montrer, sinon la décision
    « remplacer » se paierait par une surprise sur l'hôte.
    """
    texte = EXAMPLE_PATH.read_text(encoding="utf-8")
    assert "REMPLACE" in texte
    assert "local-build" in texte


# ── Intégration CLI — l'échappatoire est enfin atteignable ────────────────────

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _sans_ansi(texte: str) -> str:
    """rich colorise en CI : sans ce nettoyage, une recherche devient dépendante de l'environnement."""
    return _ANSI_RE.sub("", texte)


def _profil(tmp_path: Path) -> Path:
    """Profil §5 déclaré, emprunté aux tests du planificateur pour ne pas en écrire un second."""
    from tests.test_bootstrap_planner import _profile_document

    chemin = tmp_path / "profil.json"
    chemin.write_text(json.dumps(_profile_document()), encoding="utf-8")
    return chemin


def _invoke(tmp_path: Path, *extra: str):
    from typer.testing import CliRunner

    import cli

    models = tmp_path / "models"
    models.mkdir(exist_ok=True)
    return CliRunner().invoke(cli.app, [
        "bootstrap-plan", "--json",
        "--hardware-profile", str(_profil(tmp_path)),
        "--models-dir", str(models),
        "--pin-version", "b6800", "--pin-commit", "a" * 40, "--min-build", "6700",
        *extra,
    ])


def test_la_cli_expose_enfin_le_moyen_de_fournir_des_variantes_epinglees():
    """
    Régression du défaut d'AUT-004, reproduit ici : `ResolverPolicy.variants` est
    l'échappatoire documentée pour épingler un runtime, et AUCUNE option ne
    permettait de la remplir. Elle n'était atteignable que depuis du code Python.
    """
    import typer.main

    import cli

    groupe = typer.main.get_command(cli.app)
    commande = groupe.commands["bootstrap-plan"]  # type: ignore[attr-defined]
    options = {opt for param in commande.params for opt in getattr(param, "opts", ())}
    assert "--runtime-variants" in options
    # Contrôle positif : l'introspection voit bien les autres options.
    assert {"--pin-version", "--pin-commit", "--min-build"} <= options


def test_la_cli_planifie_l_installation_de_la_variante_fournie(tmp_path):
    result = _invoke(tmp_path, "--runtime-variants", str(write(tmp_path, document())))
    assert result.exit_code in (sc.EXIT_OK, sc.EXIT_WARNINGS), result.output
    plan = json.loads(result.output)
    assert sc.validate_plan_dict(plan) == ()

    runtime = next(s for s in plan["sections"] if s["name"] == sc.SECTION_RUNTIME)
    assert runtime["data"]["variant"]["source"] == "official-release"
    assert runtime["data"]["variant"]["artifact_sha256"] == SHA
    assert runtime["data"]["variant"]["evidence"] == rr.EVIDENCE_OPERATOR
    assert runtime["data"]["variant"]["reference"].endswith(".tar.gz")

    # Le plan est désormais réellement applicable : l'URL relue est une archive.
    installer = prod.runtime_installer_from_plan(plan, tmp_path / "runtime")
    assert installer.request.archive_url.endswith(".tar.gz")


def test_sans_variantes_fournies_le_plan_par_defaut_n_est_pas_installable(tmp_path):
    """
    Contre-épreuve, et cœur de la condition M2 : la matrice livrée produit un
    plan lisible dont l'installateur ne peut RIEN faire.
    """
    result = _invoke(tmp_path)
    assert result.exit_code in (sc.EXIT_OK, sc.EXIT_WARNINGS), result.output
    plan = json.loads(result.output)
    assert next(
        s for s in plan["sections"] if s["name"] == sc.SECTION_RUNTIME
    )["data"]["variant"]["source"] == rr.SOURCE_LOCAL_BUILD
    with pytest.raises(prod.ProductionWiringError):
        prod.runtime_installer_from_plan(plan, tmp_path / "runtime")


@pytest.mark.parametrize("doc", [
    document(archive_entry(artifact_sha256="pas-un-sha")),
    document(archive_entry(reference="https://github.com/ggml-org/llama.cpp/releases")),
    {"variants_version": 7, "variants": [archive_entry()]},
])
def test_une_matrice_refusee_sort_en_erreur_d_usage_sans_appliquer_la_matrice_livree(tmp_path, doc):
    """
    Sortie 2, jamais 1 : un opérateur — ou un script — ne doit pas confondre
    « ton fichier est mauvais » et « cet hôte est bloqué ». Et le plan n'est pas
    produit : refuser, ce n'est pas retomber sur la matrice livrée.
    """
    result = _invoke(tmp_path, "--runtime-variants", str(write(tmp_path, doc)))
    assert result.exit_code == sc.EXIT_USAGE
    assert "Matrice d'artefacts refusée" in _sans_ansi(result.output)
    assert "local-build" not in _sans_ansi(result.output)


def test_des_variantes_sans_epinglage_de_version_sont_une_erreur_d_usage(tmp_path):
    """
    La matrice dit QUOI installer, `--pin-version` dit QUELLE version. Sans les
    deux, le manifeste de provenance serait incomplet — et la cause affichée
    serait ailleurs si on laissait le plan sortir simplement bloqué.
    """
    from typer.testing import CliRunner

    import cli

    models = tmp_path / "models"
    models.mkdir(exist_ok=True)
    result = CliRunner().invoke(cli.app, [
        "bootstrap-plan", "--json",
        "--hardware-profile", str(_profil(tmp_path)),
        "--models-dir", str(models),
        "--runtime-variants", str(write(tmp_path, document())),
    ])
    assert result.exit_code == sc.EXIT_USAGE
    assert "--pin-version" in _sans_ansi(result.output)


def test_un_fichier_de_variantes_introuvable_est_une_erreur_d_usage(tmp_path):
    result = _invoke(tmp_path, "--runtime-variants", str(tmp_path / "nulle-part.yaml"))
    assert result.exit_code == sc.EXIT_USAGE


# ── Le chargeur reste hors du bac à sable du résolveur ────────────────────────

def test_le_resolveur_n_herite_pas_du_reseau_par_le_chargeur():
    """
    `runtime_variants` réutilise `public_https` — donc `socket` et `http.client`.
    Le placer dans `runtime_resolver` y ferait entrer ces modules par la bande et
    viderait le garde-fou d'isolation de sa substance.

    AUT-018 avait dû contourner : `module_toplevel_imports()` filtrait sur
    `node.level == 0` et ne voyait donc pas `from . import public_https`. Le
    contournement — chercher la chaîne « import public_https » dans le texte du
    module — s'est révélé faux dans les deux sens : aveugle à
    `from bootstrap import public_https`, et rouge dès qu'un simple COMMENTAIRE
    nomme l'import. TST-007 a rendu le garde-fou capable de voir ces imports ;
    l'assertion revient donc sur lui, qui juge l'AST et non la prose.
    """
    importes = rr.module_toplevel_imports()

    assert not (importes & rr.FORBIDDEN_IMPORTS)
    assert not (importes & rr.FORBIDDEN_SIBLING_IMPORTS)
    assert f"{rr.PACKAGE}.public_https" not in importes
    assert f"{rr.PACKAGE}.runtime_variants" not in importes

    # Contrôles positifs : l'analyse statique voit bien un import absolu ET un
    # import de module frère. Sans le second, ce test serait redevenu inerte.
    assert "yaml" in importes
    assert f"{rr.PACKAGE}.schema" in importes


def test_le_chargeur_ne_reecrit_pas_une_seconde_politique_d_url():
    """
    La règle du dépôt : `public_https` porte la politique d'URL publique, on la
    réutilise. Le chargeur ajoute des contraintes, il ne refait pas les siennes.
    """
    source = Path(rv.__file__).read_text(encoding="utf-8")
    assert "public_https.validate_url" in source
    assert "def validate_url" not in source


def test_la_matrice_fournie_ne_contourne_pas_le_refus_de_repli_cpu(tmp_path):
    """
    Fournir des variantes ne change RIEN à l'interdit de §6 : une variante CPU
    épinglée ne doit pas devenir un repli tacite sur un hôte GPU.
    """
    cpu = archive_entry(backend="cpu", reference="https://artefacts.example/cpu.tar.gz")
    variants = rv.parse_variants(document(cpu), origin="test")
    refus = resolve(variants)
    assert refus.resolved is False
    assert any(f.code == "cpu_fallback_refused" for f in refus.findings)

    assume = resolve(variants, allow_cpu_fallback=True)
    assert assume.resolved is True and assume.degraded is True


def test_le_document_charge_ne_partage_aucun_etat_mutable():
    """Deux chargements du même document rendent des variantes indépendantes."""
    doc = document()
    a = rv.parse_variants(copy.deepcopy(doc), origin="a")
    b = rv.parse_variants(copy.deepcopy(doc), origin="b")
    assert a == b
    assert a[0] is not b[0]
