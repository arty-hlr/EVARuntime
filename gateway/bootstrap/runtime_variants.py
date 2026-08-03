"""
AUT-018 — matrice d'artefacts `llama-server` fournie par l'opérateur (§6).

Le défaut fermé ici
-------------------
`runtime_resolver.DEFAULT_VARIANTS` ne porte **aucune empreinte**, et c'est un
choix juste : inventer un SHA-256 serait pire que de ne pas en avoir. Mais deux
conséquences en découlaient, subies et non assumées :

1. `ResolverPolicy.variants` est l'échappatoire officielle — « l'opérateur, ou la
   CI, fournit les variantes épinglées » — et elle n'était atteignable que depuis
   du code Python. `cli.py` exposait `--pin-version`, `--pin-commit`,
   `--min-build`, et **rien** pour fournir une variante. Même défaut qu'AUT-004,
   dont l'adaptateur était implémenté puis inatteignable depuis le parcours
   opérateur ;
2. `production.runtime_installer_from_plan` fait `archive_url = variant.reference`,
   or les `reference` par défaut valent `https://github.com/…/releases` — une page
   HTML, pas une archive. Même munie d'une empreinte, la matrice livrée ne porte
   aucune URL d'artefact exploitable.

Résultat : en configuration par défaut, l'installateur n'installait rien et
l'opérateur n'avait aucun moyen supporté d'y remédier. Ce module est ce moyen.

Ce que ce module fait, et ne fait pas
-------------------------------------
Il **lit et valide** un fichier YAML de variantes, et rend des
`ArtifactVariant`. Il ne joint aucun service, ne télécharge rien, ne vérifie
aucune empreinte contre un contenu réel : le contrôle d'intégrité appartient à
`runtime_installer` (AUT-016), qui recalcule le SHA-256 de l'archive reçue.

Il ne vit pas dans `runtime_resolver` **à dessein**. Le résolveur porte un
garde-fou d'isolation (`FORBIDDEN_IMPORTS`) qui prouve qu'il ne peut ni parler au
réseau ni lancer un build. Réutiliser `public_https` — comme la règle du dépôt
l'exige, plutôt que d'écrire une seconde politique d'URL — y ferait entrer
`socket` et `http.client` par la bande. La politique d'URL est donc appliquée
ici, dans un module qui n'a jamais promis d'être en bac à sable.

Fail-closed, sans repli
-----------------------
Un fichier malformé **refuse**. Il ne se replie pas sur `DEFAULT_VARIANTS`, ne
retient pas « les entrées valides » et n'avertit pas : une matrice à moitié lue
est une matrice dont l'opérateur croit connaître le contenu. Le refus nomme
l'entrée et le champ.

Remplacement, et non ajout
--------------------------
Les variantes du fichier **remplacent** `DEFAULT_VARIANTS`, elles ne s'y ajoutent
pas. L'union est plus confortable, et c'est précisément ce qui la disqualifie :
`SOURCE_ORDER` place `local-build` en dernier, mais la matrice livrée contient
des entrées `local-build` pour les couples les plus courants. Une faute de frappe
dans le `platform` d'une entrée opérateur (`linux-amd64` au lieu de
`linux-x86_64`) la rendrait invisible, et le `local-build` livré l'emporterait en
silence : l'opérateur lirait un plan réussi qui ignore intégralement son
épinglage. En remplacement, la même faute de frappe donne un refus explicite
« aucune variante pour linux-x86_64 » — bruyant, donc corrigible.

La contrepartie est réelle et assumée : un fichier fourni doit redéclarer les
entrées `local-build` que l'opérateur veut conserver. L'exemple livré le montre.

Le niveau de preuve n'est pas négociable
----------------------------------------
Toute entrée du fichier reçoit `EVIDENCE_OPERATOR`. Le fichier ne peut pas se
réclamer de `EVIDENCE_SPEC` : §6 ne connaît aucune empreinte, et laisser un
fichier s'attribuer l'autorité de la spécification annulerait la distinction
constat/hypothèse que le rapport d'installation (AUT-011) fait vivre. Le champ
`evidence` du fichier dit **comment l'empreinte a été relevée** ; `recorded_on`
dit **quand**. Les deux sont obligatoires et se retrouvent dans le plan.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

import yaml

from . import public_https
from . import runtime_resolver as rr

# Version du document. Un fichier plus récent est refusé plutôt que lu de
# travers — même politique que `catalog_version` et que `schema_version`.
VARIANTS_VERSION = 1

# Suffixes d'archive que `runtime_installer` sait réellement ouvrir : il détecte
# le format par le contenu (`tarfile.is_tarfile` / `zipfile.is_zipfile`), et
# `tarfile.open(…, "r:*")` couvre gzip, bzip2 et xz. Zstandard n'en fait pas
# partie : accepter `.tar.zst` promettrait une extraction qui échouerait après le
# téléchargement.
ARCHIVE_SUFFIXES: tuple[str, ...] = (
    ".zip", ".tar", ".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.bz2", ".tbz2",
)

# Marqueur des valeurs à remplacer dans le fichier d'exemple livré. Sa présence
# vaut refus dédié : « vous avez chargé l'exemple tel quel » est un diagnostic
# différent de « votre empreinte a une faute de frappe », et les deux méritent
# des messages différents.
PLACEHOLDER_TOKEN = "REMPLACER"

_TOP_KEYS = {"variants_version", "variants"}
_VARIANT_KEYS = {
    "source", "backend", "platform", "reference", "artifact_sha256",
    "container_digest", "approx_bytes", "evidence", "recorded_on",
}

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_PLATFORM_RE = re.compile(r"^[a-z0-9]+-[a-z0-9_]+$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Référence d'image : `hôte[:port]/chemin[:tag]`, minuscules. Volontairement
# étroite — l'épinglage réel est le `container_digest`, cette référence ne sert
# qu'à désigner le dépôt.
_IMAGE_RE = re.compile(r"^[a-z0-9.\-]+(:\d+)?(/[a-z0-9._\-]+)+(:[a-zA-Z0-9._\-]+)?$")

# Sources dont l'artefact est une archive téléchargeable. Elles seules exigent
# une URL et une empreinte.
_ARCHIVE_SOURCES: frozenset[str] = frozenset({
    rr.SOURCE_OFFICIAL_RELEASE, rr.SOURCE_EVARUNTIME_BUILD,
})


class RuntimeVariantsError(Exception):
    """Fichier de variantes illisible, malformé ou incohérent — refusé en bloc."""


# ── Politique d'URL d'artefact ────────────────────────────────────────────────

def validate_archive_url(url: Any, label: str) -> str:
    """
    URL d'archive réellement exploitable, et pas seulement « en HTTPS ».

    `public_https.validate_url` est la politique d'URL publique du dépôt :
    HTTPS obligatoire, pas d'identifiants dans l'autorité, pas de destination
    locale ou non routable. Elle est **réutilisée**, pas réécrite.

    Trois contraintes s'y ajoutent, qui viennent du défaut que cet item ferme :

    - le chemin doit désigner un **fichier**, avec un suffixe d'archive connu.
      `https://github.com/ggml-org/llama.cpp/releases` passe la politique HTTPS
      sans être un artefact : c'est une page HTML, et son téléchargement
      échouerait au contrôle d'empreinte après avoir consommé la bande passante ;
    - pas de chaîne de requête : une URL présignée y transporte un secret, et
      cette URL est recopiée telle quelle dans le plan, dans le rapport
      d'installation et dans les journaux ;
    - pas de fragment : il n'a aucun sens côté serveur et trahit une URL copiée
      depuis un navigateur.
    """
    try:
        cleaned = public_https.validate_url(url)
    except public_https.PublicHttpsError as exc:
        raise RuntimeVariantsError(f"{label} : {exc}") from exc

    parts = urlsplit(cleaned)
    if parts.query:
        raise RuntimeVariantsError(
            f"{label} : une URL d'artefact ne doit pas porter de chaîne de requête. "
            "Une URL présignée y transporte un secret, et cette URL est recopiée dans le "
            "plan, le rapport d'installation et les journaux."
        )
    if parts.fragment:
        raise RuntimeVariantsError(
            f"{label} : une URL d'artefact ne doit pas porter de fragment."
        )

    name = parts.path.rsplit("/", 1)[-1]
    lowered = name.lower()
    if not name or not any(lowered.endswith(suffix) for suffix in ARCHIVE_SUFFIXES):
        raise RuntimeVariantsError(
            f"{label} : {cleaned} ne désigne pas une archive. Le chemin doit se terminer par "
            f"un nom de fichier en {', '.join(ARCHIVE_SUFFIXES)}. Une page de releases n'est "
            "pas un artefact : elle serait téléchargée, puis rejetée au contrôle d'empreinte."
        )
    return cleaned


def _validate_image_reference(value: Any, label: str) -> str:
    """Référence d'image conteneur. L'épinglage reste le digest, pas ce texte."""
    text = _require_str(value, label)
    if "://" in text:
        raise RuntimeVariantsError(
            f"{label} : une image ne se désigne pas par une URL. Attendu "
            "« hôte/chemin:tag », par exemple ghcr.io/ggml-org/llama.cpp:server-cuda."
        )
    if not _IMAGE_RE.match(text):
        raise RuntimeVariantsError(
            f"{label} : référence d'image invalide {text!r} (attendu « hôte/chemin:tag »)."
        )
    host = text.split("/", 1)[0]
    if host == "localhost" or host.startswith("localhost:") or "." not in host.split(":", 1)[0]:
        raise RuntimeVariantsError(
            f"{label} : l'hôte de registre {host!r} n'est pas un registre public nommé."
        )
    return text


# ── Contrôles de champ ────────────────────────────────────────────────────────

def _reject_placeholder(value: Any, label: str) -> None:
    """
    Refuse une valeur laissée telle quelle dans le fichier d'exemple.

    L'exemple livré porte des empreintes visiblement fictives. Sans ce contrôle,
    il serait refusé quand même — le marqueur n'est pas 64 hexadécimaux — mais
    avec le message d'une faute de frappe. L'opérateur mérite de lire ce qui s'est
    réellement passé.
    """
    if isinstance(value, str) and PLACEHOLDER_TOKEN in value:
        raise RuntimeVariantsError(
            f"{label} : la valeur porte encore le marqueur « {PLACEHOLDER_TOKEN} » du fichier "
            "d'exemple. Un exemple n'est pas une matrice : relevez les valeurs réelles auprès "
            "de la release amont avant de charger ce fichier."
        )


def _require_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeVariantsError(f"{label} : chaîne non vide attendue, reçu {value!r}.")
    _reject_placeholder(value, label)
    return value.strip()


def _require_absent(value: Any, label: str, reason: str) -> None:
    if value is not None:
        raise RuntimeVariantsError(f"{label} : champ interdit ici — {reason}.")


def _require_sha256(value: Any, label: str) -> str:
    text = _require_str(value, label)
    if not _SHA256_RE.match(text):
        raise RuntimeVariantsError(
            f"{label} : attendu 64 caractères hexadécimaux minuscules, reçu {text!r}. "
            "Relevez-la avec `sha256sum` sur l'archive téléchargée, et recoupez-la avec "
            "les sommes publiées par la release."
        )
    return text


def _require_digest(value: Any, label: str) -> str:
    text = _require_str(value, label)
    if not _DIGEST_RE.match(text):
        raise RuntimeVariantsError(
            f"{label} : attendu « sha256:<64 hex minuscules> », reçu {text!r}. "
            "Relevez-le avec `docker buildx imagetools inspect <image>`."
        )
    return text


def _require_date(value: Any, label: str) -> str:
    # `yaml.safe_load` convertit `2026-08-03` non quoté en `datetime.date` : c'est
    # ce qu'un opérateur écrira naturellement, et le refuser au motif que « ce
    # n'est pas une chaîne » serait incompréhensible. Un `datetime` complet, lui,
    # est refusé : un relevé se date au jour, pas à la seconde.
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    text = _require_str(value, label)
    if not _DATE_RE.match(text):
        raise RuntimeVariantsError(f"{label} : date AAAA-MM-JJ attendue, reçu {text!r}.")
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise RuntimeVariantsError(f"{label} : date inexistante {text!r}.") from exc
    return text


def _require_positive_int(value: Any, label: str) -> int:
    _reject_placeholder(value, label)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise RuntimeVariantsError(f"{label} : entier strictement positif attendu, reçu {value!r}.")
    return value


def _reject_unknown(node: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(node) - allowed)
    if unknown:
        raise RuntimeVariantsError(
            f"{label} : champs inconnus {unknown}. Un champ hors schéma est soit une faute de "
            "frappe qui rendrait une contrainte inopérante, soit un fichier écrit pour une "
            "autre version."
        )


# ── Chargement ────────────────────────────────────────────────────────────────

def parse_variants(document: Any, *, origin: str) -> tuple[rr.ArtifactVariant, ...]:
    """
    Valide un document déjà désérialisé. Lève `RuntimeVariantsError` au moindre écart.

    Séparée de `load_variants` pour que la validation soit testable sans toucher
    au disque — même découpage que `inventory.load_hardware_profile`.
    """
    if not isinstance(document, dict):
        raise RuntimeVariantsError(
            f"{origin} : le fichier de variantes doit être un objet YAML, reçu "
            f"{type(document).__name__}."
        )
    _reject_unknown(document, _TOP_KEYS, origin)

    version = document.get("variants_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise RuntimeVariantsError(
            f"{origin} : variants_version doit être un entier, reçu {version!r}."
        )
    if version != VARIANTS_VERSION:
        raise RuntimeVariantsError(
            f"{origin} : variants_version {version} non supportée (attendu {VARIANTS_VERSION})."
        )

    raw_variants = document.get("variants")
    if not isinstance(raw_variants, list) or not raw_variants:
        raise RuntimeVariantsError(
            f"{origin} : « variants » doit être une liste non vide. Un fichier vide ne "
            "remplace pas la matrice livrée par rien : il ne serait pas fourni."
        )

    variants: list[rr.ArtifactVariant] = []
    seen: set[tuple[str, str, str]] = set()
    for index, item in enumerate(raw_variants):
        variant = _parse_variant(item, f"{origin} variants[{index}]")
        key = (variant.source, variant.backend, variant.platform)
        if key in seen:
            raise RuntimeVariantsError(
                f"{origin} variants[{index}] : couple ({'/'.join(key)}) déjà déclaré. "
                "Deux entrées identiques rendent l'ordre de préférence de §6 dépendant de "
                "l'ordre d'écriture du fichier."
            )
        seen.add(key)
        variants.append(variant)
    return tuple(variants)


def _parse_variant(node: Any, label: str) -> rr.ArtifactVariant:
    """Une entrée de matrice, contrôlée champ par champ selon sa source."""
    if not isinstance(node, dict):
        raise RuntimeVariantsError(f"{label} : objet attendu, reçu {type(node).__name__}.")
    _reject_unknown(node, _VARIANT_KEYS, label)

    source = _require_str(node.get("source"), f"{label}.source")
    if source not in rr.SOURCES:
        raise RuntimeVariantsError(
            f"{label}.source : source inconnue {source!r} (attendu parmi {sorted(rr.SOURCES)})."
        )

    backend = _require_str(node.get("backend"), f"{label}.backend")
    if backend not in rr.BACKENDS:
        raise RuntimeVariantsError(
            f"{label}.backend : backend inconnu {backend!r} (attendu parmi {sorted(rr.BACKENDS)})."
        )

    # La plateforme est validée par sa GRAMMAIRE, pas par une liste close. La
    # liste close serait `normalize_platform`, or celle-ci laisse passer une
    # architecture qu'elle ne connaît pas (`linux-ppc64le`) plutôt que de la
    # deviner. Fermer l'ensemble ici refuserait un hôte légitime ; une plateforme
    # erronée reste bruyante grâce au remplacement de la matrice.
    platform = _require_str(node.get("platform"), f"{label}.platform")
    if not _PLATFORM_RE.match(platform):
        raise RuntimeVariantsError(
            f"{label}.platform : forme « os-arch » en minuscules attendue, reçu {platform!r} "
            "(par exemple linux-x86_64, macos-arm64)."
        )

    evidence_note = _require_str(node.get("evidence"), f"{label}.evidence")
    recorded_on = _require_date(node.get("recorded_on"), f"{label}.recorded_on")

    reference = node.get("reference")
    artifact_sha256 = node.get("artifact_sha256")
    container_digest = node.get("container_digest")
    approx_bytes = node.get("approx_bytes")

    if source in _ARCHIVE_SOURCES:
        reference = validate_archive_url(reference, f"{label}.reference")
        artifact_sha256 = _require_sha256(artifact_sha256, f"{label}.artifact_sha256")
        _require_absent(
            container_digest, f"{label}.container_digest",
            f"la source « {source} » désigne une archive, pas une image",
        )
        container_digest = None
        # Obligatoire, et pas par pédanterie : §0.14 relève que le volume annoncé
        # par le plan ignore entièrement le téléchargement du runtime, faute
        # d'`approx_bytes` dans la matrice livrée. Un opérateur qui tient
        # l'archive en connaît la taille ; l'exiger rend le dimensionnement du
        # disque juste pour toute matrice fournie.
        approx_bytes = _require_positive_int(approx_bytes, f"{label}.approx_bytes")
    elif source == rr.SOURCE_OFFICIAL_CONTAINER:
        reference = _validate_image_reference(reference, f"{label}.reference")
        container_digest = _require_digest(container_digest, f"{label}.container_digest")
        _require_absent(
            artifact_sha256, f"{label}.artifact_sha256",
            "une image s'épingle par digest, jamais par somme d'archive",
        )
        artifact_sha256 = None
        approx_bytes = (
            None if approx_bytes is None
            else _require_positive_int(approx_bytes, f"{label}.approx_bytes")
        )
    else:  # local-build
        # Rien à épingler : l'empreinte naît de la construction. En échange, rien
        # ne doit prétendre l'épingler — un sha256 écrit ici ne serait vérifié
        # par personne et donnerait l'illusion d'un contrôle.
        _require_absent(
            reference, f"{label}.reference",
            "un build local ne se télécharge pas ; sa reproductibilité vient du "
            "couple version + commit de --pin-version/--pin-commit",
        )
        _require_absent(
            artifact_sha256, f"{label}.artifact_sha256",
            "l'empreinte d'un build local n'existe qu'après la construction",
        )
        _require_absent(
            container_digest, f"{label}.container_digest",
            "un build local n'est pas une image",
        )
        _require_absent(
            approx_bytes, f"{label}.approx_bytes",
            "rien n'est téléchargé pour un build local",
        )
        reference, artifact_sha256, container_digest, approx_bytes = "", None, None, None

    try:
        return rr.ArtifactVariant(
            source=source,
            backend=backend,
            platform=platform,
            # Imposé, jamais lu depuis le fichier : voir le docstring du module.
            evidence=rr.EVIDENCE_OPERATOR,
            evidence_note=f"Constat opérateur relevé le {recorded_on} : {evidence_note}",
            reference=reference,
            artifact_sha256=artifact_sha256,
            container_digest=container_digest,
            approx_bytes=approx_bytes,
        )
    except rr.ProvenanceError as exc:  # pragma: no cover - filet, les champs sont déjà contrôlés
        raise RuntimeVariantsError(f"{label} : {exc}") from exc


def load_variants(path: str | Path) -> tuple[rr.ArtifactVariant, ...]:
    """
    Charge un fichier de variantes. Lève `RuntimeVariantsError` au moindre écart.

    `yaml.safe_load` obligatoire (règle du dépôt) : une matrice d'artefacts est un
    fichier de configuration, pas un programme.
    """
    target = Path(path)
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeVariantsError(f"fichier de variantes illisible ({target}) : {exc}") from exc
    try:
        document = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise RuntimeVariantsError(f"fichier de variantes YAML invalide ({target}) : {exc}") from exc
    return parse_variants(document, origin=str(target))


__all__ = [
    "ARCHIVE_SUFFIXES",
    "PLACEHOLDER_TOKEN",
    "VARIANTS_VERSION",
    "RuntimeVariantsError",
    "load_variants",
    "parse_variants",
    "validate_archive_url",
]
