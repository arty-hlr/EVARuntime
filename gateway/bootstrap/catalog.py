"""
AUT-005 — catalogue de modèles approuvés : provenance, licence, intégrité.

Distinct du registre opérationnel, et pas par accident
------------------------------------------------------
`gateway/model_registry.py` décrit ce que CETTE installation sert : des chemins
locaux, un `vram_gb` calibré, un `enabled`. Ce catalogue-ci décrit ce que le
bootstrap a le DROIT de proposer au téléchargement : un dépôt, une révision
figée, des empreintes, une licence. §8 insiste sur la séparation ; elle tient
ici par construction — aucun import croisé, aucun champ commun, deux cycles de
vie différents. Le registre est une conséquence du catalogue, jamais l'inverse.

Ce que ce module fait, et ne fait pas
-------------------------------------
Il **lit et valide**. Il ne télécharge rien, n'écrit rien, ne joint aucun
service. Le téléchargement sûr est AUT-006 et n'appartient pas à cette vague :
ce module se contente de dire quelles entrées seraient légitimes à télécharger,
et de refuser les autres.

Fail-closed sur l'intégrité
---------------------------
§8 pose le principe pour la licence : « Un modèle sans licence identifiable doit
être refusé par défaut. » Le même raisonnement s'applique mot pour mot à
l'intégrité, et ce module l'applique :

- une licence dont l'identifiant n'est pas dans `_LICENSES` fait **échouer le
  chargement** — on ne devine pas une licence ;
- une entrée dont la révision ou l'un des SHA-256 vaut `null` est chargée,
  listée, visible… mais marquée `pending` et **écartée de toute planification**,
  avec un constat bloquant qui explique à l'opérateur comment l'épingler.

L'entrée non épinglée reste dans le catalogue à dessein : la faire disparaître
silencieusement priverait l'opérateur de l'information qui lui permet de la
corriger.

Ensemble de fichiers indivisible
--------------------------------
§8 (« Téléchargement ») : « Les fichiers split GGUF et `mmproj` doivent être
traités comme un ensemble indivisible. » Ce n'est pas une consigne d'usage, donc
ce n'est pas un commentaire : `FileSet` est immuable, refuse une série de shards
incomplète ou non contiguë, refuse un `mmproj` manquant quand le runtime le
déclare nécessaire, et n'expose aucune API rendant un sous-ensemble. Il n'y a
pas de chemin de code permettant de ne retenir qu'une partie.

Contrat de producteur (cf. `schema`)
------------------------------------
    SECTION_NAME = schema.SECTION_CATALOG
    SECTION_VERSION = 1
    to_plan_section(catalog) -> schema.PlanSection
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml

from . import schema

SECTION_NAME = schema.SECTION_CATALOG
SECTION_VERSION = 1

# Version du document attendue. Un catalogue plus récent est refusé plutôt que
# lu de travers — même politique que `schema_version` dans le plan.
CATALOG_VERSION = 1

DEFAULT_CATALOG_PATH = Path(__file__).with_name("catalog.yaml")

# Identifiant d'entrée. Même grammaire que `model_registry._MODEL_ID_RE` — les
# deux mondes sont distincts mais un identifiant de catalogue devient souvent
# l'identifiant de registre, et deux grammaires divergentes créeraient un
# renommage silencieux. Un identifiant sert aussi de nom de répertoire de
# téléchargement : `/` est exclu par la classe, `..` est refusé explicitement.
_ENTRY_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,62}[a-z0-9]$")

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")

# Nom de shard GGUF produit par `llama-gguf-split` : `…-00003-of-00007.gguf`.
_SHARD_RE = re.compile(r"^(?P<stem>.+)-(?P<index>\d{5})-of-(?P<total>\d{5})\.gguf$")

# Licences connues. Une licence ABSENTE DE CETTE TABLE fait échouer le
# chargement : c'est le « refusé par défaut » de §8. Ajouter une entrée ici est
# un acte de revue, pas une formalité.
#
# `permissive` ne veut pas dire « gratuit » ni « ouvert » : il veut dire
# redistribution et usage commercial sans condition d'acceptation individuelle.
# Les licences communautaires Llama figurent ici explicitement marquées NON
# permissives — elles imposent conditions d'usage, clause de nommage et seuils
# d'utilisateurs. Les inscrire évite qu'on les prenne un jour pour de l'Apache.
_LICENSES: dict[str, bool] = {
    "apache-2.0": True,
    "mit": True,
    "bsd-3-clause": True,
    "cc0-1.0": True,
    "cc-by-4.0": True,
    "cc-by-sa-4.0": False,
    "cc-by-nc-4.0": False,
    "gemma": False,
    "llama3": False,
    "llama3.1": False,
    "llama3.2": False,
    "llama3.3": False,
    "qwen": False,
    "qwen-research": False,
    "other": False,
}

_KNOWN_PROVIDERS: frozenset[str] = frozenset({"huggingface"})
_KNOWN_ROLES: frozenset[str] = frozenset({"weights", "weights_shard", "mmproj"})
_KNOWN_CAPABILITIES: frozenset[str] = frozenset({
    "text_generation", "streaming", "tool_calls", "vision", "embeddings",
})
_KNOWN_CACHE_TYPES: frozenset[str] = frozenset({"f16", "bf16", "q8_0", "q5_0", "q4_0"})

# Schéma strict : tout champ hors de ces ensembles est un rejet, pas un
# avertissement. Un champ inconnu signifie soit une faute de frappe qui rendrait
# une contrainte inopérante, soit un catalogue écrit pour une autre version.
_TOP_KEYS = {"catalog_version", "downloader", "models"}
_DOWNLOADER_KEYS = {"name", "license_id", "license_url", "notes"}
_ENTRY_KEYS = {
    "id", "family", "display_name", "description", "use_cases",
    "source", "license", "runtime", "resources",
}
_SOURCE_KEYS = {"provider", "repo_id", "repo_url", "revision", "revision_recorded_on", "files"}
_FILE_KEYS = {"name", "role", "sha256", "size_bytes"}
_LICENSE_KEYS = {
    "base_model", "fine_tune", "usage_terms", "gated",
    "redistribution_allowed", "operator_acceptance_required", "notes",
}
_LICENSE_REF_KEYS = {"id", "url", "repo_id"}
_RUNTIME_KEYS = {"min_llama_build", "capabilities", "requires_mmproj", "defaults"}
_DEFAULTS_KEYS = {"ctx_size", "parallel", "cache_type_k", "cache_type_v"}
_RESOURCES_KEYS = {"disk_gb", "initial_vram_gb", "initial_ram_gb", "estimation_basis"}


class CatalogError(Exception):
    """Catalogue structurellement invalide, ou usage interdit d'une entrée."""


# ── Modèle de données ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class LicenseRef:
    """Une licence identifiée : celle du modèle de base ou celle du fine-tune."""
    id: str
    url: str | None = None
    repo_id: str | None = None

    @property
    def permissive(self) -> bool:
        return _LICENSES[self.id]

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "url": self.url, "repo_id": self.repo_id, "permissive": self.permissive}


@dataclass(frozen=True)
class LicenseBlock:
    """
    Les sept distinctions exigées par §8, moins celle du téléchargeur.

    Le téléchargeur (`huggingface_hub`) est commun à toutes les entrées et vit
    au niveau du catalogue : le dupliquer par entrée inviterait à des valeurs
    divergentes pour un fait unique.
    """
    base_model: LicenseRef
    fine_tune: LicenseRef
    gated: bool
    redistribution_allowed: bool
    operator_acceptance_required: bool
    usage_terms: str | None = None
    notes: str | None = None

    @property
    def permissive(self) -> bool:
        """Permissif seulement si les DEUX étages le sont : la chaîne fait foi."""
        return self.base_model.permissive and self.fine_tune.permissive

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_model": self.base_model.to_dict(),
            "fine_tune": self.fine_tune.to_dict(),
            "usage_terms": self.usage_terms,
            "gated": self.gated,
            "redistribution_allowed": self.redistribution_allowed,
            "operator_acceptance_required": self.operator_acceptance_required,
            "permissive": self.permissive,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class CatalogFile:
    """Un fichier attendu, avec son empreinte. `sha256=None` = non épinglé."""
    name: str
    role: str
    sha256: str | None = None
    size_bytes: int | None = None

    @property
    def pinned(self) -> bool:
        return self.sha256 is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "role": self.role,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "pinned": self.pinned,
        }


@dataclass(frozen=True)
class FileSet:
    """
    Ensemble indivisible de fichiers — §8.

    Un GGUF splitté n'est pas une collection de fichiers utilisables séparément :
    c'est un seul artefact réparti sur plusieurs blobs, et un `mmproj` sans ses
    poids ne charge rien. Le modèle de données doit donc rendre la sélection
    partielle IMPOSSIBLE, pas seulement déconseillée :

    - la classe est `frozen` et `files` est un `tuple` — rien ne s'ajoute ni ne
      se retire après construction ;
    - `__post_init__` refuse une série de shards incomplète ou non contiguë ;
    - aucune méthode ne rend un sous-ensemble ; `__iter__` rend tout.
    """
    files: tuple[CatalogFile, ...]

    def __post_init__(self) -> None:
        if not self.files:
            raise CatalogError("ensemble de fichiers vide — une entrée doit déclarer ses fichiers")

        names = [f.name for f in self.files]
        if len(set(names)) != len(names):
            raise CatalogError(f"fichier déclaré deux fois dans l'ensemble : {sorted(names)}")

        weights = [f for f in self.files if f.role in ("weights", "weights_shard")]
        if not weights:
            raise CatalogError(
                "aucun fichier de poids (`weights` ou `weights_shard`) — un mmproj seul "
                "ne charge rien"
            )

        shards = [f for f in self.files if f.role == "weights_shard"]
        if shards:
            if any(f.role == "weights" for f in self.files):
                raise CatalogError(
                    "un GGUF splitté ne peut pas coexister avec un `weights` monolithique "
                    "dans le même ensemble"
                )
            self._check_shard_series(shards)

    @staticmethod
    def _check_shard_series(shards: Sequence[CatalogFile]) -> None:
        """
        Une série de shards se vérifie sur son nom : `…-00002-of-00005.gguf`.

        Le format porte lui-même le total attendu : ne pas le confronter au
        nombre de fichiers déclarés reviendrait à laisser passer un ensemble
        amputé — exactement ce que §8 interdit.
        """
        parsed: list[tuple[str, int, int]] = []
        for shard in shards:
            match = _SHARD_RE.match(shard.name)
            if not match:
                raise CatalogError(
                    f"fichier de rôle `weights_shard` au nom non conforme : {shard.name!r} "
                    "(attendu « …-00001-of-0000N.gguf »)"
                )
            parsed.append((
                match.group("stem"), int(match.group("index")), int(match.group("total"))
            ))

        stems = {p[0] for p in parsed}
        if len(stems) != 1:
            raise CatalogError(f"shards issus de séries différentes : {sorted(stems)}")

        totals = {p[2] for p in parsed}
        if len(totals) != 1:
            raise CatalogError(f"shards annonçant des totaux différents : {sorted(totals)}")

        total = totals.pop()
        indices = sorted(p[1] for p in parsed)
        if indices != list(range(1, total + 1)):
            missing = sorted(set(range(1, total + 1)) - set(indices))
            raise CatalogError(
                f"ensemble de shards incomplet : {len(indices)}/{total} déclarés, "
                f"manque {missing} — un GGUF splitté est indivisible (§8)"
            )

    def __iter__(self) -> Any:
        return iter(self.files)

    def __len__(self) -> int:
        return len(self.files)

    @property
    def pinned(self) -> bool:
        return all(f.pinned for f in self.files)

    @property
    def total_bytes(self) -> int | None:
        sizes = [f.size_bytes for f in self.files]
        return sum(s for s in sizes if s is not None) if all(s is not None for s in sizes) else None

    def to_dict(self) -> list[dict[str, Any]]:
        return [f.to_dict() for f in self.files]


@dataclass(frozen=True)
class RuntimeDefaults:
    """Paramètres de service proposés. Le registre les reprendra, ou les durcira."""
    ctx_size: int
    parallel: int
    cache_type_k: str
    cache_type_v: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "ctx_size": self.ctx_size,
            "parallel": self.parallel,
            "cache_type_k": self.cache_type_k,
            "cache_type_v": self.cache_type_v,
        }


@dataclass(frozen=True)
class RuntimeBlock:
    min_llama_build: int
    capabilities: tuple[str, ...]
    defaults: RuntimeDefaults
    requires_mmproj: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "min_llama_build": self.min_llama_build,
            "capabilities": list(self.capabilities),
            "requires_mmproj": self.requires_mmproj,
            "defaults": self.defaults.to_dict(),
        }


@dataclass(frozen=True)
class ResourceEstimate:
    """
    Ressources ESTIMÉES. Jamais mesurées — le nom des champs le dit, le rendu
    aussi, et `estimation_basis` dit d'où elles viennent.
    """
    disk_gb: float
    initial_vram_gb: float
    initial_ram_gb: float
    estimation_basis: str = "gguf_header"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "estimation",
            "disk_gb": self.disk_gb,
            "initial_vram_gb": self.initial_vram_gb,
            "initial_ram_gb": self.initial_ram_gb,
            "estimation_basis": self.estimation_basis,
            "avertissement": (
                "Valeurs conservatrices, non mesurées. Une calibration par chargement "
                "réel (§9) est requise avant d'écrire un `vram_gb` de registre."
            ),
        }


@dataclass(frozen=True)
class CatalogEntry:
    """Une proposition du catalogue, avec tout ce qu'il faut pour l'accepter… ou la refuser."""
    id: str
    family: str
    display_name: str
    description: str
    use_cases: tuple[str, ...]
    provider: str
    repo_id: str
    repo_url: str | None
    revision: str | None
    revision_recorded_on: str | None
    files: FileSet
    license: LicenseBlock
    runtime: RuntimeBlock
    resources: ResourceEstimate

    # ── Intégrité ─────────────────────────────────────────────────────────────

    @property
    def pinned(self) -> bool:
        """Épinglée = révision exacte ET empreinte de chaque fichier de l'ensemble."""
        return self.revision is not None and self.files.pinned

    @property
    def verification(self) -> str:
        return "pinned" if self.pinned else "pending"

    @property
    def blocking_findings(self) -> tuple[schema.Finding, ...]:
        """
        Ce qui interdit de planifier le téléchargement de cette entrée.

        Fail-closed : tant que cette liste n'est pas vide, `download_fileset()`
        refuse et `Catalog.plannable_entries()` exclut l'entrée. Le message dit
        quoi faire, pas seulement ce qui ne va pas.
        """
        out: list[schema.Finding] = []
        if not self.pinned:
            missing = "révision" if self.revision is None else ""
            unpinned = [f.name for f in self.files if not f.pinned]
            if unpinned:
                missing = (missing + " et " if missing else "") + f"SHA-256 de {unpinned}"
            out.append(schema.Finding(
                code="catalogue_entree_non_epinglee",
                level="fail",
                message=(
                    f"« {self.id} » n'est pas épinglée ({missing} manquant) : son "
                    "téléchargement ne peut pas être planifié. Relevez les vraies valeurs "
                    f"— `curl -s https://huggingface.co/api/models/{self.repo_id}?blobs=true` "
                    "donne `sha` (la révision) et `siblings[].lfs.sha256` (l'empreinte de "
                    "chaque fichier) — puis reportez-les dans catalog.yaml. N'inventez "
                    "jamais ces valeurs : un SHA faux rend la vérification d'intégrité "
                    "inopérante tout en la faisant paraître active."
                ),
            ))
        if self.license.gated:
            out.append(schema.Finding(
                code="catalogue_modele_gated",
                level="fail",
                message=(
                    f"« {self.id} » est un dépôt gated : il exige une acceptation nominative "
                    "et un jeton. Le bootstrap ne peut ni l'accepter à votre place ni "
                    "planifier ce téléchargement."
                ),
            ))
        return tuple(out)

    @property
    def advisory_findings(self) -> tuple[schema.Finding, ...]:
        """Ce qui mérite d'être lu sans interdire le téléchargement."""
        out: list[schema.Finding] = []
        if not self.license.permissive:
            out.append(schema.Finding(
                code="catalogue_licence_non_permissive",
                level="warn",
                message=(
                    f"« {self.id} » : licence {self.license.base_model.id} / "
                    f"{self.license.fine_tune.id} — non permissive. Usage, redistribution "
                    "et conditions doivent être validés par l'organisation avant activation."
                ),
            ))
        if not self.license.redistribution_allowed:
            out.append(schema.Finding(
                code="catalogue_redistribution_interdite",
                level="warn",
                message=(
                    f"« {self.id} » : la redistribution du GGUF n'est pas autorisée. "
                    "L'artefact ne doit pas être recopié vers un miroir interne partagé."
                ),
            ))
        return tuple(out)

    @property
    def findings(self) -> tuple[schema.Finding, ...]:
        return self.blocking_findings + self.advisory_findings

    @property
    def plannable(self) -> bool:
        return not self.blocking_findings

    def download_fileset(self) -> FileSet:
        """
        Rend l'ensemble COMPLET des fichiers à récupérer, ou refuse.

        Seul point d'entrée vers les fichiers d'une entrée en vue d'un
        téléchargement, et il est tout-ou-rien : il n'accepte ni filtre, ni
        rôle, ni index de shard. C'est ainsi que « ensemble indivisible » cesse
        d'être une consigne pour devenir une propriété du code.
        """
        if not self.plannable:
            raise CatalogError(
                f"« {self.id} » n'est pas planifiable : "
                + " ; ".join(f.message for f in self.blocking_findings)
            )
        return self.files

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "family": self.family,
            "display_name": self.display_name,
            "description": self.description,
            "use_cases": list(self.use_cases),
            "source": {
                "provider": self.provider,
                "repo_id": self.repo_id,
                "repo_url": self.repo_url,
                "revision": self.revision,
                "revision_recorded_on": self.revision_recorded_on,
                "files": self.files.to_dict(),
                "total_bytes": self.files.total_bytes,
            },
            "license": self.license.to_dict(),
            "runtime": self.runtime.to_dict(),
            "resources": self.resources.to_dict(),
            "verification": self.verification,
            "plannable": self.plannable,
            "findings": [f.to_dict() for f in self.findings],
        }


@dataclass(frozen=True)
class Downloader:
    """Licence du LOGICIEL de téléchargement — distinction exigée par §8."""
    name: str
    license_id: str
    license_url: str | None = None
    notes: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "license_id": self.license_id,
            "license_url": self.license_url,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class Catalog:
    version: int
    downloader: Downloader
    entries: tuple[CatalogEntry, ...]
    source_path: str | None = None

    def get(self, entry_id: str) -> CatalogEntry | None:
        for entry in self.entries:
            if entry.id == entry_id:
                return entry
        return None

    def plannable_entries(self) -> tuple[CatalogEntry, ...]:
        """Les seules entrées dont un téléchargement peut être planifié."""
        return tuple(e for e in self.entries if e.plannable)

    def findings(self) -> tuple[schema.Finding, ...]:
        out: list[schema.Finding] = []
        for entry in self.entries:
            out.extend(entry.findings)
        return tuple(out)


# ── Chargement et validation stricte ──────────────────────────────────────────

def load_catalog(path: str | Path | None = None) -> Catalog:
    """
    Charge et valide le catalogue. Lève `CatalogError` au moindre écart.

    `yaml.safe_load` obligatoire (règle du dépôt) : un catalogue est un fichier
    de configuration, pas un programme.
    """
    target = Path(path) if path is not None else DEFAULT_CATALOG_PATH
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise CatalogError(f"catalogue illisible ({target}) : {exc}") from exc

    try:
        document = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise CatalogError(f"catalogue YAML invalide ({target}) : {exc}") from exc

    if not isinstance(document, dict):
        raise CatalogError(
            f"le catalogue doit être un objet YAML, reçu {type(document).__name__} ({target})"
        )

    _reject_unknown(document, _TOP_KEYS, "catalogue")

    version = document.get("catalog_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise CatalogError(f"catalog_version doit être un entier, reçu {version!r}")
    if version != CATALOG_VERSION:
        raise CatalogError(
            f"catalog_version {version} non supportée (attendu {CATALOG_VERSION}) — "
            "mettez à jour eva-bootstrap avant de lire ce catalogue"
        )

    downloader = _parse_downloader(document.get("downloader"))

    models = document.get("models")
    if not isinstance(models, list) or not models:
        raise CatalogError("models doit être une liste non vide — un catalogue vide n'a pas d'usage")

    entries: list[CatalogEntry] = []
    seen: set[str] = set()
    for index, item in enumerate(models):
        entry = _parse_entry(item, f"models[{index}]")
        if entry.id in seen:
            raise CatalogError(f"models[{index}].id en double : {entry.id!r}")
        seen.add(entry.id)
        entries.append(entry)

    return Catalog(
        version=version,
        downloader=downloader,
        entries=tuple(entries),
        source_path=str(target),
    )


def _reject_unknown(node: Mapping[str, Any], allowed: set[str], path: str) -> None:
    """
    Rejette tout champ hors du schéma. Strict à dessein.

    Un champ inconnu est soit une faute de frappe — et alors la contrainte
    qu'on croyait poser (`gated`, `sha256`) n'existe pas —, soit un catalogue
    écrit pour une autre version. Les deux cas doivent s'arrêter ici.
    """
    unknown = sorted(set(node) - allowed)
    if unknown:
        raise CatalogError(
            f"{path} : champ(s) inconnu(s) {unknown} (attendus : {sorted(allowed)})"
        )


def _require_mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CatalogError(f"{path} doit être un objet, reçu {type(value).__name__}")
    return value


def _require_str(node: Mapping[str, Any], key: str, path: str) -> str:
    value = node.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CatalogError(f"{path}.{key} doit être une chaîne non vide, reçu {value!r}")
    return value.strip()


def _optional_str(node: Mapping[str, Any], key: str, path: str) -> str | None:
    value = node.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise CatalogError(f"{path}.{key} doit être une chaîne non vide ou null, reçu {value!r}")
    return value.strip()


def _require_bool(node: Mapping[str, Any], key: str, path: str) -> bool:
    value = node.get(key)
    if not isinstance(value, bool):
        raise CatalogError(
            f"{path}.{key} doit être un booléen explicite, reçu {value!r} — "
            "une valeur absente serait interprétée comme « non », ce qui est trop permissif"
        )
    return value


def _require_int(node: Mapping[str, Any], key: str, path: str, *, minimum: int) -> int:
    value = node.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise CatalogError(f"{path}.{key} doit être un entier >= {minimum}, reçu {value!r}")
    return value


def _require_float(node: Mapping[str, Any], key: str, path: str) -> float:
    value = node.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise CatalogError(f"{path}.{key} doit être un nombre >= 0, reçu {value!r}")
    return float(value)


def _require_str_list(node: Mapping[str, Any], key: str, path: str, allowed: frozenset[str] | None) -> tuple[str, ...]:
    value = node.get(key)
    if not isinstance(value, list) or not value:
        raise CatalogError(f"{path}.{key} doit être une liste non vide, reçu {value!r}")
    out: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise CatalogError(f"{path}.{key}[{index}] doit être une chaîne non vide")
        item = item.strip()
        if allowed is not None and item not in allowed:
            raise CatalogError(
                f"{path}.{key}[{index}] inconnu : {item!r} (attendu parmi {sorted(allowed)})"
            )
        out.append(item)
    return tuple(out)


def _parse_downloader(node: Any) -> Downloader:
    data = _require_mapping(node, "downloader")
    _reject_unknown(data, _DOWNLOADER_KEYS, "downloader")
    license_id = _require_str(data, "license_id", "downloader")
    if license_id not in _LICENSES:
        raise CatalogError(
            f"downloader.license_id inconnu : {license_id!r} — une licence non identifiée "
            f"est refusée par défaut (§8). Connues : {sorted(_LICENSES)}"
        )
    return Downloader(
        name=_require_str(data, "name", "downloader"),
        license_id=license_id,
        license_url=_optional_str(data, "license_url", "downloader"),
        notes=_optional_str(data, "notes", "downloader"),
    )


def _parse_entry(node: Any, path: str) -> CatalogEntry:
    data = _require_mapping(node, path)
    _reject_unknown(data, _ENTRY_KEYS, path)

    entry_id = _require_str(data, "id", path)
    if not _ENTRY_ID_RE.match(entry_id) or ".." in entry_id:
        raise CatalogError(
            f"{path}.id non conforme : {entry_id!r} — attendu minuscules, chiffres, "
            "« . », « _ » et « - », entre 3 et 64 caractères, sans « .. », ne commençant "
            "ni ne finissant par un séparateur"
        )

    source = _require_mapping(data.get("source"), f"{path}.source")
    _reject_unknown(source, _SOURCE_KEYS, f"{path}.source")

    provider = _require_str(source, "provider", f"{path}.source")
    if provider not in _KNOWN_PROVIDERS:
        raise CatalogError(
            f"{path}.source.provider inconnu : {provider!r} "
            f"(attendu parmi {sorted(_KNOWN_PROVIDERS)})"
        )

    revision = _optional_str(source, "revision", f"{path}.source")
    if revision is not None and not _REVISION_RE.match(revision):
        raise CatalogError(
            f"{path}.source.revision non conforme : {revision!r} — attendu un SHA de commit "
            "de 40 caractères hexadécimaux minuscules, ou null si vous ne l'avez pas relevé. "
            "Une branche (« main ») n'est pas une révision : elle bouge."
        )

    runtime = _parse_runtime(data.get("runtime"), f"{path}.runtime")
    files = _parse_files(source.get("files"), f"{path}.source.files", runtime.requires_mmproj)

    return CatalogEntry(
        id=entry_id,
        family=_require_str(data, "family", path),
        display_name=_require_str(data, "display_name", path),
        description=_require_str(data, "description", path),
        use_cases=_require_str_list(data, "use_cases", path, None),
        provider=provider,
        repo_id=_require_str(source, "repo_id", f"{path}.source"),
        repo_url=_optional_str(source, "repo_url", f"{path}.source"),
        revision=revision,
        revision_recorded_on=_optional_str(source, "revision_recorded_on", f"{path}.source"),
        files=files,
        license=_parse_license(data.get("license"), f"{path}.license"),
        runtime=runtime,
        resources=_parse_resources(data.get("resources"), f"{path}.resources"),
    )


def _parse_files(node: Any, path: str, requires_mmproj: bool) -> FileSet:
    if not isinstance(node, list) or not node:
        raise CatalogError(f"{path} doit être une liste non vide")

    files: list[CatalogFile] = []
    for index, item in enumerate(node):
        sub = f"{path}[{index}]"
        data = _require_mapping(item, sub)
        _reject_unknown(data, _FILE_KEYS, sub)

        role = _require_str(data, "role", sub)
        if role not in _KNOWN_ROLES:
            raise CatalogError(
                f"{sub}.role inconnu : {role!r} (attendu parmi {sorted(_KNOWN_ROLES)})"
            )

        name = _require_str(data, "name", sub)
        if "/" in name or name.startswith("."):
            raise CatalogError(
                f"{sub}.name doit être un simple nom de fichier du dépôt, reçu {name!r}"
            )

        sha256 = _optional_str(data, "sha256", sub)
        if sha256 is not None and not _SHA256_RE.match(sha256):
            raise CatalogError(
                f"{sub}.sha256 non conforme : {sha256!r} — attendu 64 caractères "
                "hexadécimaux minuscules, ou null si vous ne l'avez pas relevé. "
                "Ne remplissez JAMAIS ce champ avec une valeur approchée."
            )

        size = data.get("size_bytes")
        if size is not None and (not isinstance(size, int) or isinstance(size, bool) or size <= 0):
            raise CatalogError(f"{sub}.size_bytes doit être un entier > 0 ou null, reçu {size!r}")

        files.append(CatalogFile(name=name, role=role, sha256=sha256, size_bytes=size))

    if requires_mmproj and not any(f.role == "mmproj" for f in files):
        raise CatalogError(
            f"{path} : le runtime déclare `requires_mmproj: true` mais aucun fichier de rôle "
            "`mmproj` n'est listé — poids et projecteur forment un ensemble indivisible (§8)"
        )

    return FileSet(files=tuple(files))


def _parse_license(node: Any, path: str) -> LicenseBlock:
    data = _require_mapping(node, path)
    _reject_unknown(data, _LICENSE_KEYS, path)
    return LicenseBlock(
        base_model=_parse_license_ref(data.get("base_model"), f"{path}.base_model"),
        fine_tune=_parse_license_ref(data.get("fine_tune"), f"{path}.fine_tune"),
        usage_terms=_optional_str(data, "usage_terms", path),
        gated=_require_bool(data, "gated", path),
        redistribution_allowed=_require_bool(data, "redistribution_allowed", path),
        operator_acceptance_required=_require_bool(data, "operator_acceptance_required", path),
        notes=_optional_str(data, "notes", path),
    )


def _parse_license_ref(node: Any, path: str) -> LicenseRef:
    """
    Une licence absente ou non identifiée est un rejet — §8, sans exception.

    Aucun repli sur « inconnue » ni sur un avertissement : accepter une entrée
    dont on ne sait pas dire les conditions reviendrait à laisser l'organisation
    les découvrir après le déploiement.
    """
    data = _require_mapping(node, path)
    _reject_unknown(data, _LICENSE_REF_KEYS, path)
    license_id = _require_str(data, "id", path)
    if license_id not in _LICENSES:
        raise CatalogError(
            f"{path}.id : licence non identifiée ({license_id!r}) — refusée par défaut (§8). "
            f"Connues : {sorted(_LICENSES)}. N'ajoutez un identifiant à `_LICENSES` qu'après "
            "avoir lu le texte de la licence et statué sur son caractère permissif."
        )
    return LicenseRef(
        id=license_id,
        url=_optional_str(data, "url", path),
        repo_id=_optional_str(data, "repo_id", path),
    )


def _parse_runtime(node: Any, path: str) -> RuntimeBlock:
    data = _require_mapping(node, path)
    _reject_unknown(data, _RUNTIME_KEYS, path)

    defaults = _require_mapping(data.get("defaults"), f"{path}.defaults")
    _reject_unknown(defaults, _DEFAULTS_KEYS, f"{path}.defaults")

    for key in ("cache_type_k", "cache_type_v"):
        value = _require_str(defaults, key, f"{path}.defaults")
        if value not in _KNOWN_CACHE_TYPES:
            raise CatalogError(
                f"{path}.defaults.{key} inconnu : {value!r} "
                f"(attendu parmi {sorted(_KNOWN_CACHE_TYPES)})"
            )

    return RuntimeBlock(
        min_llama_build=_require_int(data, "min_llama_build", path, minimum=0),
        capabilities=_require_str_list(data, "capabilities", path, _KNOWN_CAPABILITIES),
        requires_mmproj=_require_bool(data, "requires_mmproj", path),
        defaults=RuntimeDefaults(
            ctx_size=_require_int(defaults, "ctx_size", f"{path}.defaults", minimum=512),
            parallel=_require_int(defaults, "parallel", f"{path}.defaults", minimum=1),
            cache_type_k=defaults["cache_type_k"].strip(),
            cache_type_v=defaults["cache_type_v"].strip(),
        ),
    )


def _parse_resources(node: Any, path: str) -> ResourceEstimate:
    data = _require_mapping(node, path)
    _reject_unknown(data, _RESOURCES_KEYS, path)
    return ResourceEstimate(
        disk_gb=_require_float(data, "disk_gb", path),
        initial_vram_gb=_require_float(data, "initial_vram_gb", path),
        initial_ram_gb=_require_float(data, "initial_ram_gb", path),
        estimation_basis=_require_str(data, "estimation_basis", path),
    )


# ── Projection vers le plan ───────────────────────────────────────────────────

def to_plan_section(
    catalog: Catalog,
    *,
    selected_ids: Iterable[str] | None = None,
) -> schema.PlanSection:
    """
    Projette le catalogue vers le contrat de `schema`. JSON, sans secret.

    Statut, et pourquoi il est sévère
    ---------------------------------
    `fail` dès qu'une entrée est bloquée, `ok` sinon. C'est volontairement plus
    strict qu'un « au moins une entrée utilisable ». Deux raisons :

    1. le catalogue est un artefact versionné, sous notre contrôle : une entrée
       non épinglée ou gated y est un DÉFAUT de l'artefact, pas une condition
       d'environnement qu'on subirait ;
    2. `schema.BootstrapPlan.blockers` ne collecte les constats `fail` que des
       sections elles-mêmes en `fail`. Émettre un constat bloquant dans une
       section `warn` le rendrait invisible du verdict — le blocage doit être
       porté par le statut de section, sinon il n'existe pas.

    `selected_ids` restreint la projection sans changer cette règle : ce qui est
    projeté est ce qui est jugé.
    """
    entries = catalog.entries
    if selected_ids is not None:
        wanted = list(selected_ids)
        unknown = [i for i in wanted if catalog.get(i) is None]
        if unknown:
            raise CatalogError(f"identifiant(s) absent(s) du catalogue : {sorted(unknown)}")
        entries = tuple(e for e in entries if e.id in set(wanted))

    plannable = [e for e in entries if e.plannable]
    blocked = [e for e in entries if not e.plannable]

    findings: list[schema.Finding] = []
    for entry in entries:
        findings.extend(entry.findings)

    if blocked:
        status: schema.SectionStatus = "fail"
        summary = (
            f"{len(plannable)}/{len(entries)} entrée(s) approuvée(s) — "
            f"{len(blocked)} bloquée(s) : {', '.join(e.id for e in blocked)}."
        )
    elif not entries:
        status = "fail"
        summary = "Aucune entrée de catalogue retenue — rien à proposer au téléchargement."
        findings.append(schema.Finding(
            code="catalogue_vide",
            level="fail",
            message="La sélection ne retient aucune entrée : aucun modèle ne peut être proposé.",
        ))
    else:
        status = "ok"
        summary = (
            f"{len(entries)} modèle(s) approuvé(s), épinglé(s) et sous licence identifiée "
            f"({', '.join(sorted({e.license.base_model.id for e in entries}))})."
        )

    data: dict[str, Any] = {
        "catalog_version": catalog.version,
        "source_path": catalog.source_path,
        "downloader": catalog.downloader.to_dict(),
        "counts": {
            "total": len(entries),
            "plannable": len(plannable),
            "blocked": len(blocked),
        },
        "estimated_download_bytes": sum(
            e.files.total_bytes or 0 for e in plannable
        ),
        "avertissement_ressources": (
            "Les champs `resources` sont des ESTIMATIONS conservatrices, pas des mesures. "
            "Une calibration par chargement réel (§9) reste nécessaire avant d'écrire un "
            "`vram_gb` dans le registre opérationnel."
        ),
        "models": [e.to_dict() for e in entries],
    }

    return schema.PlanSection(
        name=SECTION_NAME,
        version=SECTION_VERSION,
        status=status,
        summary=summary,
        data=data,
        findings=schema.merge_findings(findings),
    )
