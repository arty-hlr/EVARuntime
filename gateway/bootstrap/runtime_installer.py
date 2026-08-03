"""
AUT-016 — installation effective de `llama-server` (jalon M2, action `install_runtime`).

Ce que ce module fait
---------------------
Il **applique** la décision d'AUT-003. Il ne la reprend pas, ne la révise pas et
n'en invente aucune partie : `runtime_resolver` a dit quelle variante installer,
sous quelle version épinglée, avec quelle empreinte et sur quel niveau de preuve ;
ce module pose le binaire sur l'hôte, le **vérifie**, et refuse de le publier tant
qu'il n'a pas été vérifié.

Il est l'exécuteur de `schema.ACTION_INSTALL_RUNTIME` au sens du contrat
`execution.StepExecutor` : `async (PlanStep, ExecutionContext) -> StepResult`.

Pourquoi il importe `runtime_resolver`
--------------------------------------
Les producteurs de la vague 5 ne s'importent jamais entre eux, et les exécuteurs
de la vague 6 non plus. `runtime_resolver` n'est ni l'un ni l'autre pour ce
module : c'est le **contrat de la décision** qu'il applique, au même titre que
`schema` est le contrat du plan et `execution` celui du journal. Redéfinir ici
`ProvenanceManifest` produirait deux définitions du manifeste §6 dans le même
dépôt, et c'est toujours celle qui n'a pas été mise à jour qui finit écrite sur
l'hôte. Aucun autre chantier de la vague 6 ne possède ce fichier : la règle de
parallélisation reste intacte.

L'ordre des opérations, et pourquoi il est dans cet ordre
---------------------------------------------------------
1. **recoupement** de la décision reçue contre l'étape relue par l'opérateur.
   Une résolution qui ne parle pas de la même version ni du même backend que
   l'étape du plan n'est pas appliquée : l'opérateur a signé un texte, c'est ce
   texte qui s'exécute ;
2. **idempotence** — si le binaire attendu est déjà là, à la bonne version, avec
   son manifeste et une empreinte inchangée, l'étape rend `already_satisfied` et
   n'écrit rien. Rien n'est retéléchargé, rien n'est rebasculé. Ce contrôle a lieu
   **aussi en simulation**, et il y sonde le binaire déjà en place : `--version`
   n'écrit rien et ne télécharge rien, c'est exactement ce que le résolveur
   s'autorisait déjà en M1, et sans lui une simulation ne saurait pas dire si
   l'étape aurait quelque chose à faire ;
3. **téléchargement** dans une aire d'incubation, bornée en taille ;
4. **vérification de l'archive** contre le SHA-256 **de l'épinglage**. Le résumé
   attendu vient de `ProvenanceManifest.artifact_sha256`, calculé en amont par la
   politique de release — jamais recalculé depuis l'octet qu'on vient de recevoir
   puis comparé à lui-même, ce qui ne prouverait que la capacité de `hashlib` à
   être déterministe ;
5. **extraction défensive** (voir plus bas) ;
6. **bascule atomique** vers la destination finale ;
7. **relecture de la version depuis le binaire posé**, confrontée à l'épinglage ;
8. **manifeste de provenance** écrit à côté du binaire, puis relu et validé.

Pourquoi la bascule est un lien symbolique, et pas un `mv` de fin de course
--------------------------------------------------------------------------
COR-016 (§0.10) : `update-agent.sh` construisait un venv dans un `mktemp -d` puis
le **déplaçait** vers son emplacement définitif. Un venv n'est pas relogeable —
le shebang de `bin/uvicorn` continuait de pointer vers un staging supprimé, et
l'unité systemd échouait en `203/EXEC`. `gateway/deploy/update.sh` ne souffre pas
du défaut : il construit à l'emplacement final et **bascule par symlink**. Ce
module reprend littéralement cette stratégie :

- l'incubation (`.incoming-*`) est **sous la racine d'installation**, donc sur le
  même système de fichiers : la promotion en `release-*` est un `os.replace`,
  c'est-à-dire un renommage atomique, jamais une copie entre volumes ;
- le binaire n'est **jamais exécuté depuis un chemin autre que celui d'où il
  servira** : la sonde `--version` tourne après la promotion, dans le répertoire
  de release définitif ;
- la publication est un unique renommage atomique du lien `current`. À aucun
  instant `current` ne désigne un demi-arbre : il pointe sur l'ancienne release
  ou sur la nouvelle, jamais entre les deux ;
- l'ancienne release **reste en place**. C'est ce qui rend l'étape réversible, et
  les preuves de l'étape portent son chemin (`previous_release`).

Le corollaire est connu et assumé : comme pour `update.sh`, plus rien n'est
écrasé, donc les releases s'accumulent. C'est la même classe de dette qu'OPS-010
et OPS-002 ; ce module ne purge rien et ne prétend pas le faire.

Extraction défensive — une archive est une entrée non fiable
------------------------------------------------------------
`tarfile.extractall()` et `ZipFile.extractall()` ne sont pas utilisés, et un test
le vérifie sur l'AST du fichier. Python 3.11 n'active pas `data_filter` par
défaut ; s'appuyer dessus ferait dépendre la sécurité de ce module d'une valeur
par défaut qui n'existe pas sur la version de la CI. La barrière est donc écrite
ici, et refuse explicitement :

- les chemins absolus, les lettres de lecteur Windows et tout composant `..` ;
- les liens symboliques et physiques dont la cible sort de la destination ;
- l'écriture à travers un lien symbolique déjà posé (le parent réel est recoupé
  juste avant chaque écriture) ;
- les entrées qui ne sont ni un fichier ordinaire, ni un répertoire, ni un lien
  (périphériques, FIFO, sockets) ;
- les bombes de décompression : nombre d'entrées, taille décompressée cumulée et
  ratio de compression sont bornés, et la borne est appliquée **pendant** la
  copie — un en-tête qui ment sur sa taille ne sert donc à rien ;
- les bits `setuid`/`setgid` : les modes de l'archive ne sont jamais propagés,
  les fichiers sont posés en 0644, les répertoires en 0755, et seul le binaire
  attendu reçoit 0755.

Fail-closed sur la version, sans exception — et pourquoi
---------------------------------------------------------
SEC-009 (§0.12) a constaté que la politique `LLAMA_SERVER_MIN_BUILD` existe en
trois endroits avec deux sémantiques : `doctor` est fail-closed,
`main._validate_inference_runtime` et `llama_version.enforce_llama_min_build` ne
le sont pas (version illisible → `log.warning` et démarrage autorisé). Cet
exécuteur tranche pour lui-même, et dans le sens que §6 exige :

- version **illisible** → refus, quel que soit le plancher, y compris à 0. Le
  binaire vient d'être téléchargé et posé par nous : s'il ne sait pas dire ce
  qu'il est, nous ne savons pas ce que nous avons installé ;
- version **différente de l'épinglage** → refus. Une archive qui porte la bonne
  empreinte mais rend un autre numéro de build est une anomalie de chaîne
  d'approvisionnement, pas un détail ;
- version ≤ `SHALLOW_CLONE_MAX_BUILD` → refus, avec le message de §0.10 : c'est la
  signature d'un `git clone --depth 1`, pas celle d'un binaire ancien ;
- version < plancher de sécurité → refus, recoupé indépendamment de l'invariant
  de `ReleasePolicy`.

Aucun de ces refus ne laisse quoi que ce soit de publié : la release incriminée
est retirée et `current` n'a pas bougé.

Ce que ce module n'installe pas
-------------------------------
- **les images conteneur** : `server_manager` ne sait lancer que des
  sous-processus natifs, §6 prévoit un backend conteneur qui n'existe pas. Une
  variante `official-container` est refusée explicitement plutôt qu'à moitié
  installée ;
- **le build local** : construire depuis les sources est un autre métier (clone
  complet, toolchain, cache, minutes à heures) et un autre item. Une variante
  `local-build` est refusée avec la conduite à tenir. Conséquence assumée : avec
  la matrice `DEFAULT_VARIANTS`, où rien n'est épinglé, ce module n'installe
  rien. C'est cohérent avec AUT-003, qui écarte déjà toute variante non épinglée,
  et l'inverse — installer sans empreinte — serait le défaut que §6 interdit.

Injection
---------
Réseau, sous-processus, horloge et racines d'écriture sont tous injectés :
`transport` (défaut `UrllibTransport`, bibliothèque standard uniquement, aucune
dépendance ajoutée), `probe` (défaut `llama_version.probe_llama_version`),
`ExecutionContext.now` pour l'horodatage et `ExecutionContext.allowed_roots` pour
ce que l'installation a le droit de toucher. Une liste de racines vide n'autorise
rien, y compris en simulation.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import stat
import tarfile
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit

import yaml

from llama_version import LlamaVersion, probe_llama_version

from . import execution, public_https, runtime_resolver, schema

# ── Constantes d'installation ─────────────────────────────────────────────────

DEFAULT_BINARY_NAME = "llama-server"

# Nom du manifeste §6, posé À CÔTÉ du binaire et non à la racine : c'est le
# couple (binaire, manifeste) qui doit rester solidaire d'une release à l'autre.
MANIFEST_FILENAME = "provenance.yaml"

CURRENT_LINK_NAME = "current"
RELEASE_PREFIX = "release-"
INCOMING_PREFIX = ".incoming-"
ARCHIVE_FILENAME = "artifact.download"

# §6 « Obligations de licence » : llama.cpp est sous MIT, la notice doit être
# conservée auprès du binaire.
DEFAULT_LICENSE = "MIT"
LICENSE_CANDIDATES: tuple[str, ...] = (
    "LICENSE", "LICENSE.txt", "LICENSE.md", "COPYING", "COPYING.txt",
)

# Agent identifiable dans les journaux du serveur amont. Aucune information
# d'hôte : l'inventaire n'a pas à fuiter vers un CDN.
USER_AGENT = "eva-bootstrap-apply/1"

_CHUNK = 1024 * 1024
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
DEFAULT_MAX_REDIRECTS = 5

# Codes de constat, exposés pour que les tests les nomment plutôt que de citer
# des chaînes libres qui divergeraient en silence.
CODE_REFUSED = "runtime_install_refused"
CODE_ARCHIVE_MISMATCH = "runtime_artifact_mismatch"
CODE_ARCHIVE_UNSAFE = "runtime_archive_unsafe"
CODE_VERSION_UNREADABLE = "runtime_version_unreadable"
CODE_VERSION_MISMATCH = "runtime_version_mismatch"
CODE_SHALLOW_CLONE = "runtime_shallow_clone"
CODE_BUILD_TOO_OLD = "runtime_build_too_old"
CODE_BINARY_ABSENT = "runtime_binary_absent"
CODE_LICENSE_MISSING = "runtime_license_notice_missing"
CODE_DEGRADED = "runtime_installed_degraded"
CODE_EVIDENCE_ASSUMED = "runtime_evidence_assumed"
CODE_BINARY_ALTERED = "runtime_binary_altered"
CODE_INSTALL_FAILED = "runtime_install_failed"


class InstallError(schema.PlanError):
    """L'installation ne peut pas avoir lieu, ou ce qui a été reçu n'est pas ce qui était attendu."""


class ArchiveRefused(InstallError):
    """L'archive est hostile ou hors bornes. Rien n'en est extrait."""


# ── URL d'artefact ────────────────────────────────────────────────────────────

def validate_artifact_url(url: Any) -> str:
    """
    Refuse tout ce qui n'est pas un HTTPS sans identifiants vers une destination publique.

    Quatre refus, pour quatre menaces distinctes du modèle de `AGENTS.md` :

    - `http://`, `file://`, `ftp://` : un artefact récupéré en clair ou depuis le
      disque local n'est pas celui que la politique a épinglé, et `file://` est le
      chemin le plus court vers une SSRF locale ;
    - identifiants dans l'autorité (`https://user:jeton@…`) : un secret dans une
      URL finit dans un journal, dans un rapport d'exécution, et dans `argv` si
      quelqu'un délègue un jour à `curl` ;
    - autorité vide : `https:///chemin` ne désigne rien.
    - adresse littérale non publique ou nom local : un téléchargement ne doit
      jamais devenir une sonde du réseau de l'hôte. Les noms ordinaires sont
      contrôlés après résolution par le transport.
    """
    try:
        return public_https.validate_url(url)
    except public_https.PublicHttpsError as exc:
        raise InstallError(str(exc)) from exc


def sanitize_url(url: str) -> str:
    """
    Forme publiable d'une URL : sans identifiants, sans requête, sans fragment.

    Ce qui est publié dans un rapport d'exécution est publié pour de bon. Une URL
    signée porte son jeton dans la requête ; le rapport n'a besoin que de l'origine
    et du chemin pour qu'un opérateur sache d'où vient l'artefact.
    """
    try:
        parts = urlsplit(str(url))
    except ValueError:  # pragma: no cover - urlsplit ne lève que sur des IPv6 malformées
        return "<url illisible>"
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


# ── Transport ─────────────────────────────────────────────────────────────────

AddressInfo = public_https.AddressInfo
Resolver = public_https.Resolver
ConnectionFactory = public_https.ConnectionFactory
_HTTPSConnectionLike = public_https.HTTPSConnectionLike
_PinnedHTTPSConnection = public_https.PinnedHTTPSConnection
_system_resolve = public_https.system_resolve
_pinned_connection_factory = public_https.pinned_connection_factory
# Compatibilité des tests historiques qui injectent la socket via ce module ;
# les deux noms désignent le même module standard employé par le connecteur partagé.
socket = public_https.socket


def _resolve_public_addresses(host: str, port: int, resolver: Resolver) -> tuple[AddressInfo, ...]:
    try:
        return public_https.resolve_public_addresses(host, port, resolver)
    except public_https.PublicHttpsError as exc:
        raise InstallError(str(exc)) from exc


class ArtifactTransport(Protocol):
    """
    Ce qui sait récupérer une archive. Injectable, pour que les tests ne touchent jamais le réseau.

    Le contrat est volontairement pauvre : écrire l'octet reçu dans `destination`,
    rendre le nombre d'octets écrits, et ne jamais dépasser `max_bytes`. Aucune
    notion d'en-tête, d'authentification ni de reprise — tout cela appartiendrait
    à une implémentation, pas au contrat, et ouvrirait la porte à un secret dans
    la signature.
    """

    async def fetch(self, url: str, destination: Path, *, max_bytes: int) -> int:
        ...


class UrllibTransport:
    """
    Transport de production : bibliothèque standard seule, aucune dépendance ajoutée.

    `CLAUDE.md` interdit d'ajouter une dépendance sans gain opérationnel réel, et
    la vague 5 a écarté le paquet `gguf` officiel pour cette raison exacte. Un
    téléchargement borné avec contrôle d'empreinte en aval ne justifie pas
    `httpx` : `http.client` suffit, et l'appel bloquant part dans un thread pour
    que le module reste asynchrone de bout en bout.

    Les redirections sont suivies manuellement et chaque destination est
    validée puis résolue avant sa requête. La connexion TCP emploie exactement
    l'adresse validée, tout en conservant le nom original pour SNI et le
    certificat TLS : un second résultat DNS ne peut pas la faire rebondir vers
    le réseau privé entre le contrôle et `connect()`.
    """

    def __init__(
        self,
        *,
        timeout: float = 300.0,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        resolver: Resolver | None = None,
        connection_factory: ConnectionFactory | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout doit être > 0")
        if (
            not isinstance(max_redirects, int)
            or isinstance(max_redirects, bool)
            or max_redirects < 0
        ):
            raise ValueError("max_redirects doit être un entier >= 0")
        self._timeout = timeout
        self._max_redirects = max_redirects
        self._resolver = resolver if resolver is not None else _system_resolve
        self._connection_factory = (
            connection_factory if connection_factory is not None else _pinned_connection_factory
        )

    async def fetch(self, url: str, destination: Path, *, max_bytes: int) -> int:
        validate_artifact_url(url)
        return await asyncio.to_thread(self._fetch_blocking, url, destination, max_bytes)

    def _fetch_blocking(self, url: str, destination: Path, max_bytes: int) -> int:
        current = validate_artifact_url(url)
        redirects = 0
        while True:
            parts = urlsplit(current)
            host = parts.hostname
            if host is None:
                raise InstallError(
                    "URL d'artefact sans hôte après validation — téléchargement refusé"
                )
            port = parts.port or 443
            addresses = _resolve_public_addresses(host, port, self._resolver)
            connection = self._connection_factory(host, port, addresses, self._timeout)
            response: Any | None = None
            try:
                target = urlunsplit(("", "", parts.path or "/", parts.query, ""))
                connection.request("GET", target, headers={"User-Agent": USER_AGENT})
                response = connection.getresponse()
                status = int(response.status)

                if status in _REDIRECT_STATUSES:
                    location = response.getheader("Location")
                    if not location:
                        raise InstallError(
                            f"redirection HTTP {status} sans en-tête Location depuis "
                            f"{sanitize_url(current)}"
                        )
                    if redirects >= self._max_redirects:
                        raise InstallError(
                            f"trop de redirections pour l'artefact (borne {self._max_redirects})"
                        )
                    # Validation AVANT l'itération suivante, donc avant toute
                    # résolution ou connexion vers la nouvelle destination.
                    current = validate_artifact_url(urljoin(current, location))
                    redirects += 1
                    continue

                if not 200 <= status < 300:
                    raise InstallError(
                        f"téléchargement refusé par la source d'artefact "
                        f"{sanitize_url(current)} (HTTP {status})"
                    )

                written = 0
                with open(destination, "wb") as sink:
                    while True:
                        chunk = response.read(_CHUNK)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > max_bytes:
                            raise InstallError(
                                f"archive plus grande que la borne autorisée ({max_bytes} octets) : "
                                "téléchargement interrompu"
                            )
                        sink.write(chunk)
                return written
            finally:
                if response is not None:
                    response.close()
                connection.close()


VersionProbe = Callable[[Path], Awaitable[LlamaVersion]]


# ── Bornes d'extraction ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class ArchiveLimits:
    """
    Bornes d'une archive d'artefact. Toutes finies : « pas de borne » n'est pas une valeur.

    Les défauts sont dimensionnés pour une archive `llama-server` avec ses
    bibliothèques CUDA, pas pour un dépôt entier. Un test les réduit à quelques
    octets pour prouver que chacune mord réellement.
    """
    max_archive_bytes: int = 1024 ** 3
    max_extracted_bytes: int = 2 * 1024 ** 3
    max_entries: int = 4096
    max_ratio: int = 100

    def __post_init__(self) -> None:
        for name in ("max_archive_bytes", "max_extracted_bytes", "max_entries", "max_ratio"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise InstallError(f"ArchiveLimits.{name} doit être un entier > 0, reçu {value!r}")


@dataclass(frozen=True)
class ExtractionReport:
    """Ce que l'extraction a réellement posé. Sert de preuve, pas de décoration."""
    entries: int
    extracted_bytes: int
    names: tuple[str, ...]


# ── Extraction défensive ──────────────────────────────────────────────────────

def _refuse_member_name(name: str) -> PurePosixPath:
    """
    Valide le nom d'une entrée d'archive, ou refuse. Aucun nettoyage silencieux.

    Nettoyer un `../` plutôt que refuser transformerait une archive hostile en
    archive acceptable, et l'opérateur ne saurait jamais qu'on lui a envoyé une
    traversée de chemin.
    """
    if not isinstance(name, str) or not name.strip():
        raise ArchiveRefused("entrée d'archive sans nom")
    normalise = name.replace("\\", "/")
    tete = normalise.split("/", 1)[0]
    if len(tete) >= 2 and tete[1] == ":":
        raise ArchiveRefused(f"entrée d'archive en chemin absolu Windows : {name!r}")
    pure = PurePosixPath(normalise)
    if pure.is_absolute():
        raise ArchiveRefused(f"entrée d'archive en chemin absolu : {name!r}")
    if ".." in pure.parts:
        raise ArchiveRefused(f"traversée de chemin dans l'archive : {name!r}")
    return pure


def _resolve_inside(destination: Path, relative: PurePosixPath) -> Path:
    """Chemin cible, prouvé sous la destination. Le contrôle est refait sur le chemin normalisé."""
    cible = Path(os.path.normpath(str(destination / relative)))
    if cible != destination and not cible.is_relative_to(destination):
        raise ArchiveRefused(f"entrée d'archive hors de la destination : {relative}")
    return cible


def _refuse_link_target(destination: Path, cible: Path, lien: str) -> None:
    """
    Refuse un lien qui sort de la destination, symbolique comme physique.

    Un lien vers `/etc/shadow` ou vers `../../root` ne « pointe pas vers un
    fichier absent » : il pointe vers un fichier du système, et l'entrée suivante
    de l'archive écrira dedans.
    """
    if not lien:
        raise ArchiveRefused(f"lien sans cible dans l'archive : {cible.name}")
    if lien.startswith("/") or lien.startswith("\\"):
        raise ArchiveRefused(f"lien absolu dans l'archive : {cible.name} → {lien!r}")
    vise = Path(os.path.normpath(str(cible.parent / lien.replace("\\", "/"))))
    if vise != destination and not vise.is_relative_to(destination):
        raise ArchiveRefused(
            f"lien sortant de la destination dans l'archive : {cible.name} → {lien!r}"
        )


def _prepare_parent(destination: Path, cible: Path) -> None:
    """
    Crée l'arborescence parente et recoupe qu'aucun lien symbolique ne la détourne.

    Deuxième barrière, volontairement redondante avec `_refuse_link_target` : une
    archive qui poserait d'abord un lien accepté puis écrirait au travers verrait
    son écriture arriver ailleurs. Le parent RÉEL est donc recoupé juste avant
    chaque écriture, pas seulement au moment où le lien est créé.
    """
    parent = cible.parent
    parent.mkdir(parents=True, exist_ok=True)
    reel = Path(os.path.realpath(parent))
    if reel != destination and not reel.is_relative_to(destination):
        raise ArchiveRefused(
            f"écriture au travers d'un lien symbolique sortant : {cible.name} (parent réel {reel})"
        )


def _copy_bounded(source: BinaryIO, cible: Path, restant: int) -> int:
    """
    Copie en bornant les octets RÉELLEMENT écrits, sans rien devoir à l'en-tête.

    Seconde barrière contre la bombe de décompression, volontairement indépendante
    de la première : la taille déclarée est déjà recoupée avant l'ouverture du
    flux, mais cette valeur vient de l'archive, c'est-à-dire de la source qu'on ne
    veut justement pas croire. Ici, on compte ce qu'on écrit, et l'entrée
    partielle est supprimée avant de lever — un refus ne laisse pas de moignon.
    """
    ecrits = 0
    try:
        with open(cible, "wb") as sink:
            while True:
                bloc = source.read(_CHUNK)
                if not bloc:
                    break
                ecrits += len(bloc)
                if ecrits > restant:
                    raise ArchiveRefused(
                        "taille décompressée cumulée dépassée : l'archive livre plus que la borne "
                        f"autorisée ({restant} octets restants) — bombe de décompression probable"
                    )
                sink.write(bloc)
    except ArchiveRefused:
        cible.unlink(missing_ok=True)
        raise
    os.chmod(cible, 0o644)
    return ecrits


def _extract_tar(archive: Path, destination: Path, limits: ArchiveLimits, taille: int) -> ExtractionReport:
    """Extraction tar, membre par membre. `extractall()` n'est jamais appelé."""
    entrees = 0
    total = 0
    noms: list[str] = []
    with tarfile.open(archive, "r:*") as tar:
        while True:
            membre = tar.next()
            if membre is None:
                break
            entrees += 1
            if entrees > limits.max_entries:
                raise ArchiveRefused(
                    f"archive à plus de {limits.max_entries} entrées : refusée avant extraction"
                )
            relative = _refuse_member_name(membre.name)
            cible = _resolve_inside(destination, relative)

            if membre.isdir():
                cible.mkdir(parents=True, exist_ok=True)
                os.chmod(cible, 0o755)
                noms.append(membre.name)
                continue

            if membre.issym() or membre.islnk():
                _refuse_link_target(destination, cible, membre.linkname or "")
                _prepare_parent(destination, cible)
                cible.unlink(missing_ok=True)
                if membre.issym():
                    os.symlink(membre.linkname, cible)
                else:
                    source_lien = _resolve_inside(destination, _refuse_member_name(membre.linkname))
                    if not source_lien.exists():
                        raise ArchiveRefused(
                            f"lien physique vers une entrée absente de l'archive : {membre.name}"
                        )
                    os.link(source_lien, cible)
                noms.append(membre.name)
                continue

            if not membre.isfile():
                raise ArchiveRefused(
                    f"entrée d'archive d'un type non installable ({membre.name}) : "
                    "seuls les fichiers, répertoires et liens internes sont admis"
                )

            _refuser_taille_declaree(membre.name, membre.size, _restant(limits, total, taille))
            flux = tar.extractfile(membre)
            if flux is None:
                raise ArchiveRefused(f"entrée d'archive illisible : {membre.name}")
            _prepare_parent(destination, cible)
            with flux:
                total += _copy_bounded(flux, cible, _restant(limits, total, taille))
            noms.append(membre.name)

    return ExtractionReport(entries=entrees, extracted_bytes=total, names=tuple(noms))


def _extract_zip(archive: Path, destination: Path, limits: ArchiveLimits, taille: int) -> ExtractionReport:
    """
    Extraction zip, entrée par entrée. `extractall()` n'est jamais appelé.

    Un lien symbolique zip est encodé dans les bits de mode d'`external_attr`.
    `ZipFile.extract()` l'écrit alors comme un fichier ordinaire contenant le
    chemin de la cible : le lien est perdu en silence, et une archive légitime
    devient une installation subtilement cassée. On refuse plutôt que de produire
    ce résultat-là.
    """
    entrees = 0
    total = 0
    noms: list[str] = []
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            entrees += 1
            if entrees > limits.max_entries:
                raise ArchiveRefused(
                    f"archive à plus de {limits.max_entries} entrées : refusée avant extraction"
                )
            relative = _refuse_member_name(info.filename)
            cible = _resolve_inside(destination, relative)

            mode = (info.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode):
                raise ArchiveRefused(
                    f"lien symbolique dans une archive ZIP ({info.filename}) : il serait posé comme "
                    "un fichier ordinaire contenant le chemin de sa cible"
                )

            if info.is_dir():
                cible.mkdir(parents=True, exist_ok=True)
                os.chmod(cible, 0o755)
                noms.append(info.filename)
                continue

            _refuser_taille_declaree(info.filename, info.file_size, _restant(limits, total, taille))
            _prepare_parent(destination, cible)
            with zf.open(info) as flux:
                total += _copy_bounded(flux, cible, _restant(limits, total, taille))
            noms.append(info.filename)

    return ExtractionReport(entries=entrees, extracted_bytes=total, names=tuple(noms))


def _refuser_taille_declaree(nom: str, declaree: int, restant: int) -> None:
    """
    Refuse AVANT d'ouvrir le flux une entrée qui annonce plus que le budget restant.

    Première barrière, la moins chère : elle évite d'écrire un seul octet d'une
    archive qui annonce déjà dépasser. Elle ne suffit pas — elle croit l'archive —
    d'où la seconde barrière, à l'écriture.
    """
    if isinstance(declaree, int) and declaree > restant:
        raise ArchiveRefused(
            f"entrée {nom!r} annonce {declaree} octets décompressés pour un budget restant de "
            f"{restant} : bombe de décompression probable, rien n'est extrait"
        )


def _restant(limits: ArchiveLimits, deja: int, taille_archive: int) -> int:
    """Octets encore autorisés, borne absolue ET borne de ratio, la plus stricte gagnant."""
    plafond_ratio = max(taille_archive, 1) * limits.max_ratio
    plafond = min(limits.max_extracted_bytes, plafond_ratio)
    return max(plafond - deja, 0)


def extract_archive(
    archive: Path,
    destination: Path,
    *,
    limits: ArchiveLimits | None = None,
) -> ExtractionReport:
    """
    Extrait une archive dans `destination` avec toutes les barrières de §6 appliquées.

    Fonction publique parce qu'elle est le cœur testable du module : les tests la
    nourrissent d'archives fabriquées (traversée, lien sortant, bombe, entrée
    exotique, setuid) sans avoir à monter une installation complète.
    """
    bornes = limits or ArchiveLimits()
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    taille = archive.stat().st_size
    if taille > bornes.max_archive_bytes:
        raise ArchiveRefused(
            f"archive de {taille} octets au-delà de la borne {bornes.max_archive_bytes}"
        )
    if tarfile.is_tarfile(archive):
        return _extract_tar(archive, destination, bornes, taille)
    if zipfile.is_zipfile(archive):
        return _extract_zip(archive, destination, bornes, taille)
    raise ArchiveRefused(
        f"format d'archive non reconnu pour {archive.name} : seuls tar (gz/xz/bz2) et zip sont admis"
    )


# ── Empreintes ────────────────────────────────────────────────────────────────

def sha256_file(path: Path) -> str:
    """SHA-256 hexadécimal d'un fichier, lu par blocs. Aucun fichier n'est chargé en mémoire."""
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        while True:
            bloc = source.read(_CHUNK)
            if not bloc:
                break
            digest.update(bloc)
    return digest.hexdigest()


async def _sha256_async(path: Path) -> str:
    return await asyncio.to_thread(sha256_file, path)


# ── Requête d'installation ────────────────────────────────────────────────────

@dataclass(frozen=True)
class RuntimeInstallRequest:
    """
    Tout ce que l'exécuteur doit savoir, et rien de plus.

    La décision vient de `runtime_resolver` ; l'URL et la racine d'installation
    viennent de l'appelant (CLI ou orchestrateur), parce qu'elles dépendent du
    miroir et du parc, pas de la résolution. Aucun secret n'a sa place ici : un
    artefact qui exigerait une authentification n'est pas un artefact public, et
    §6 n'en prévoit pas.
    """
    resolution: runtime_resolver.RuntimeResolution
    archive_url: str
    install_root: Path
    binary_name: str = DEFAULT_BINARY_NAME
    license_spdx: str = DEFAULT_LICENSE
    limits: ArchiveLimits = field(default_factory=ArchiveLimits)

    def __post_init__(self) -> None:
        object.__setattr__(self, "install_root", Path(self.install_root))


def refusal_reasons(request: RuntimeInstallRequest) -> tuple[str, ...]:
    """
    Toutes les raisons de ne pas installer cette résolution. Vide si elle est installable.

    Publique et pure : une CLI peut refuser avant de commencer plutôt que d'échouer
    à mi-parcours, et le test peut les énumérer sans monter d'installation.
    """
    raisons: list[str] = []
    resolution = request.resolution

    if not resolution.resolved:
        raisons.append(
            "la résolution du runtime a échoué : rien n'a été décidé, il n'y a donc rien à installer"
        )
    if resolution.reuse_existing:
        raisons.append(
            "la résolution conclut à la conservation du binaire en place : une installation "
            "remplacerait un runtime que le plan a explicitement décidé de garder"
        )
    variant = resolution.variant
    manifest = resolution.manifest
    if variant is None or manifest is None:
        raisons.append("la résolution ne porte ni variante ni manifeste : décision incomplète")
        return tuple(raisons)

    if variant.source == runtime_resolver.SOURCE_OFFICIAL_CONTAINER:
        raisons.append(
            "variante conteneur : `server_manager` ne lance que des sous-processus natifs et §6 "
            "prévoit un backend conteneur qui n'existe pas. Choisissez une variante native ou "
            "implémentez d'abord ce backend"
        )
    if variant.source == runtime_resolver.SOURCE_LOCAL_BUILD:
        raisons.append(
            "variante « local-build » : cet exécuteur installe un artefact vérifié, il ne compile "
            "pas depuis les sources. Fournissez une variante épinglée (artifact_sha256) via la "
            "politique, ou construisez le binaire hors de ce parcours"
        )
    if not manifest.artifact_sha256:
        raisons.append(
            "manifeste sans artifact_sha256 : l'archive serait installée sans contrôle d'intégrité (§6)"
        )
    try:
        validate_artifact_url(request.archive_url)
    except InstallError as exc:
        raisons.append(str(exc))

    if not request.binary_name or "/" in request.binary_name or request.binary_name in (".", ".."):
        raisons.append(f"nom de binaire invalide : {request.binary_name!r}")

    return tuple(raisons)


def _step_matches(step: schema.PlanStep, request: RuntimeInstallRequest) -> str | None:
    """
    Recoupe l'étape relue par l'opérateur contre la décision liée à l'exécuteur.

    L'opérateur a lu un texte qui nomme une version et un backend. Exécuter autre
    chose sous ce numéro d'étape reviendrait à lui faire signer un blanc-seing —
    et le rapport final porterait la cible du plan, pas celle qui a été posée.
    """
    manifest = request.resolution.manifest
    variant = request.resolution.variant
    if manifest is None or variant is None:
        return None
    if manifest.version not in step.target:
        return (
            f"l'étape vise « {step.target} » alors que la décision porte la version "
            f"{manifest.version}"
        )
    if variant.backend not in step.target:
        return (
            f"l'étape vise « {step.target} » alors que la décision porte le backend "
            f"{variant.backend}"
        )
    return None


def covers_step(request: RuntimeInstallRequest, step: schema.PlanStep) -> bool:
    """
    Cette étape désigne-t-elle l'artefact de CETTE décision de runtime ?

    Existe parce que `verify_artifact` est une action à DEUX domaines : le
    planificateur l'émet une fois pour l'archive de `llama-server` et une fois
    par ensemble de GGUF, avec deux grammaires de cible incompatibles. Un
    registre n'admettant qu'un exécuteur par action, l'applicateur doit trancher
    lui-même, et il doit le faire sur une question posée par le module qui
    possède la décision — pas en devinant la forme d'une chaîne chez lui.

    Fail-closed : une décision sans manifeste ni variante ne couvre rien.
    """
    if request.resolution.manifest is None or request.resolution.variant is None:
        return False
    return _step_matches(step, request) is None


# ── Exécuteur ─────────────────────────────────────────────────────────────────

class RuntimeInstaller:
    """
    Exécuteur de `schema.ACTION_INSTALL_RUNTIME`. Satisfait `execution.StepExecutor`.

    Une instance = une installation décidée. Le transport, la sonde de version et
    l'horloge sont injectés ; en production, ce sont un vrai téléchargement HTTPS,
    un vrai `llama-server --version` et l'horloge de `ExecutionContext`.
    """

    def __init__(
        self,
        request: RuntimeInstallRequest,
        *,
        transport: ArtifactTransport | None = None,
        probe: VersionProbe | None = None,
    ) -> None:
        self.request = request
        self._transport = transport if transport is not None else UrllibTransport()
        self._probe: VersionProbe = probe if probe is not None else probe_llama_version

    # ── Chemins ───────────────────────────────────────────────────────────────

    @property
    def current_link(self) -> Path:
        return self.request.install_root / CURRENT_LINK_NAME

    @property
    def published_binary(self) -> Path:
        return self.current_link / self.request.binary_name

    @property
    def published_manifest(self) -> Path:
        return self.current_link / MANIFEST_FILENAME

    # ── Contrat d'exécuteur ───────────────────────────────────────────────────

    async def __call__(
        self, step: schema.PlanStep, context: execution.ExecutionContext
    ) -> execution.StepResult:
        debut = context.monotonic()
        try:
            return await self._executer(step, context, debut)
        except execution.ExecutionError:
            # Racine non autorisée : c'est le contrat d'exécution qui parle, on le
            # laisse remonter tel quel — le lanceur en fait un échec consigné.
            raise
        except InstallError as exc:
            return self._echec(step, context, debut, CODE_INSTALL_FAILED, str(exc))

    async def _executer(
        self,
        step: schema.PlanStep,
        context: execution.ExecutionContext,
        debut: float,
    ) -> execution.StepResult:
        raisons = refusal_reasons(self.request)
        if raisons:
            return self._echec(
                step, context, debut, CODE_REFUSED,
                "installation refusée : " + " ; ".join(raisons),
            )

        ecart = _step_matches(step, self.request)
        if ecart is not None:
            return self._echec(step, context, debut, CODE_REFUSED, ecart)

        # `resolve_path` refuse une racine hors périmètre ET une liste de racines
        # vide. Le contrôle vaut aussi en simulation : une simulation qui promet
        # une écriture impossible ne simule rien d'utile.
        racine = context.resolve_path(self.request.install_root)

        manifest = self.request.resolution.manifest
        variant = self.request.resolution.variant
        if manifest is None or variant is None:
            # Garanti par `refusal_reasons`, mais un `assert` disparaîtrait sous
            # `python -O` — que rien n'interdit dans une unité systemd — et le
            # refus deviendrait un `AttributeError` opaque (COR-021).
            return self._echec(
                step, context, debut, CODE_REFUSED,
                "installation refusée : la résolution ne porte ni manifeste ni variante "
                "exploitables — rien à installer, et rien à attester.",
            )

        satisfait = await self._deja_satisfait(manifest)
        if satisfait.ok:
            return execution.StepResult.for_step(
                step,
                status=execution.STEP_ALREADY_SATISFIED,
                summary=(
                    f"llama-server {manifest.version} ({variant.backend}) déjà installé et attesté "
                    f"dans {self.published_binary} : rien n'a été téléchargé ni écrit."
                ),
                duration_ms=self._ms(context, debut),
                evidence=satisfait.evidence,
                findings=self._constats_contextuels(variant),
            )

        if context.dry_run:
            return execution.StepResult.for_step(
                step,
                status=execution.STEP_WOULD_APPLY,
                summary=self._resume_simulation(manifest, variant, racine, satisfait.raison),
                duration_ms=self._ms(context, debut),
                evidence=self._preuves_simulation(manifest, variant, racine, satisfait),
                findings=self._constats_contextuels(variant),
            )

        return await self._appliquer(step, context, debut, racine, manifest, variant, satisfait)

    # ── Idempotence ───────────────────────────────────────────────────────────

    async def _deja_satisfait(self, manifest: runtime_resolver.ProvenanceManifest) -> _Idempotence:
        """
        Le binaire attendu est-il déjà là, à la bonne version et intact ?

        Trois conditions, toutes nécessaires. Le manifeste seul ne suffit pas : il
        est un fichier texte à côté du binaire, et personne ne l'empêche de
        survivre à un remplacement du binaire. L'empreinte du binaire posé est donc
        recalculée et confrontée à celle que l'installation avait consignée, et la
        version est relue depuis le binaire lui-même.
        """
        binaire = self.published_binary
        manifeste = self.published_manifest
        if not binaire.exists():
            return _Idempotence(False, "aucun binaire n'est publié à cet emplacement", {})
        if not manifeste.exists():
            return _Idempotence(
                False, "le binaire en place n'a pas de manifeste de provenance §6", {}
            )

        try:
            document = yaml.safe_load(manifeste.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            return _Idempotence(False, f"manifeste en place illisible ({exc})", {})

        erreurs = runtime_resolver.validate_manifest_document(document)
        if erreurs:
            return _Idempotence(
                False, "manifeste en place incohérent : " + " ; ".join(erreurs), {}
            )

        pose = runtime_resolver.manifest_from_document(document)
        attendu = (
            manifest.version, manifest.commit, manifest.source,
            manifest.backend, manifest.platform, manifest.artifact_sha256,
        )
        constate = (
            pose.version, pose.commit, pose.source,
            pose.backend, pose.platform, pose.artifact_sha256,
        )
        if attendu != constate:
            return _Idempotence(
                False,
                f"le manifeste en place décrit {pose.version}/{pose.source}/{pose.backend} "
                f"et non {manifest.version}/{manifest.source}/{manifest.backend}",
                {},
            )

        bloc = document.get("install") if isinstance(document, dict) else None
        consigne = bloc.get("binary_sha256") if isinstance(bloc, dict) else None
        empreinte = await _sha256_async(binaire)
        if not isinstance(consigne, str) or consigne != empreinte:
            return _Idempotence(
                False,
                "l'empreinte du binaire en place ne correspond plus à celle consignée à "
                "l'installation : il a été remplacé ou altéré depuis",
                {},
                altere=isinstance(consigne, str),
            )

        version = await self._probe(binaire)
        if version.build != manifest.build_number:
            return _Idempotence(
                False,
                f"le binaire en place rend le build {version.build}, l'épinglage attend "
                f"{manifest.build_number}",
                {},
            )

        return _Idempotence(
            True,
            "",
            {
                "binary": str(binaire),
                "binary_sha256": empreinte,
                "manifest": str(manifeste),
                "observed_build": version.build,
                "expected_build": manifest.build_number,
                "reinstalled": False,
            },
        )

    # ── Application ───────────────────────────────────────────────────────────

    async def _appliquer(
        self,
        step: schema.PlanStep,
        context: execution.ExecutionContext,
        debut: float,
        racine: Path,
        manifest: runtime_resolver.ProvenanceManifest,
        variant: runtime_resolver.ArtifactVariant,
        satisfait: _Idempotence,
    ) -> execution.StepResult:
        horodatage = _stamp(context.now())
        racine.mkdir(parents=True, exist_ok=True)
        incubation = _chemin_libre(racine, f"{INCOMING_PREFIX}{manifest.version}-{horodatage}")
        release = _chemin_libre(racine, f"{RELEASE_PREFIX}{manifest.version}-{horodatage}")
        precedente = _cible_courante(self.current_link)

        incubation.mkdir(parents=True)
        promue = False
        try:
            archive = incubation / ARCHIVE_FILENAME
            octets = await self._transport.fetch(
                self.request.archive_url, archive, max_bytes=self.request.limits.max_archive_bytes
            )
            context.journaliser(
                f"artefact reçu depuis {sanitize_url(self.request.archive_url)} ({octets} octets)"
            )

            empreinte = await self._sha_archive(archive, manifest, octets)
            arbre = incubation / "tree"
            rapport = await asyncio.to_thread(
                extract_archive, archive, arbre, limits=self.request.limits
            )
            archive.unlink(missing_ok=True)

            binaire_incube = _localiser_binaire(arbre, self.request.binary_name)
            _aplatir(arbre, incubation)
            binaire_incube = incubation / binaire_incube.relative_to(arbre)

            # Promotion : renommage atomique sur le même système de fichiers. Le
            # binaire ne sera exécuté qu'après, depuis le chemin d'où il servira.
            os.replace(incubation, release)
            promue = True
            binaire = release / binaire_incube.relative_to(incubation)
            os.chmod(binaire, 0o755)

            version = await self._probe(binaire)
            refus = _verdict_version(binaire, version, manifest, self.request.resolution.min_build)
            if refus is not None:
                shutil.rmtree(release, ignore_errors=True)
                return self._echec(step, context, debut, refus.code, refus.message)

            empreinte_binaire = await _sha256_async(binaire)
            licence = _licence_installee(release)
            installe_le = context.now()
            document = self._document_manifeste(
                manifest=manifest,
                variant=variant,
                binaire=binaire,
                empreinte_binaire=empreinte_binaire,
                licence_posee=licence is not None,
                precedente=precedente,
                release=release,
                observed_build=version.build,
                installe_le=installe_le,
            )
            chemin_manifeste = binaire.parent / MANIFEST_FILENAME
            chemin_manifeste.write_text(
                yaml.safe_dump(document, sort_keys=False, allow_unicode=True), encoding="utf-8"
            )
            os.chmod(chemin_manifeste, 0o644)
            _relire_manifeste(chemin_manifeste)

            _basculer(self.current_link, binaire.parent)
        except ArchiveRefused as exc:
            _nettoyer(incubation, release if promue else None)
            return self._echec(step, context, debut, CODE_ARCHIVE_UNSAFE, str(exc))
        except InstallError as exc:
            _nettoyer(incubation, release if promue else None)
            return self._echec(step, context, debut, CODE_ARCHIVE_MISMATCH, str(exc))
        except Exception as exc:
            _nettoyer(incubation, release if promue else None)
            return self._echec(
                step, context, debut, CODE_INSTALL_FAILED,
                execution.redact_for_log(f"{type(exc).__name__}: {exc}"),
            )

        constats = list(self._constats_contextuels(variant))
        if licence is None:
            constats.append(schema.Finding(
                code=CODE_LICENSE_MISSING,
                level="warn",
                message=(
                    f"Aucune notice de licence n'accompagne l'artefact installé (cherché : "
                    f"{', '.join(LICENSE_CANDIDATES)}). llama.cpp est sous {self.request.license_spdx} "
                    "et §6 exige que la licence et les notices soient conservées auprès du binaire. "
                    "L'installation est fonctionnelle, la conformité de redistribution ne l'est pas."
                ),
            ))
        if satisfait.altere:
            constats.append(schema.Finding(
                code=CODE_BINARY_ALTERED,
                level="warn",
                message=(
                    "Le binaire précédemment publié ne correspondait plus à l'empreinte consignée "
                    "lors de son installation : il avait été remplacé ou altéré hors de ce parcours. "
                    "Il vient d'être remplacé par l'artefact épinglé."
                ),
            ))

        return execution.StepResult.for_step(
            step,
            status=execution.STEP_DONE,
            summary=(
                f"llama-server {manifest.version} ({variant.backend}) installé depuis "
                f"{variant.source} et publié via {self.current_link} ; build {version.build} relu "
                f"sur le binaire posé, manifeste §6 écrit."
            ),
            duration_ms=self._ms(context, debut),
            evidence={
                "artifact_url": sanitize_url(self.request.archive_url),
                "artifact_bytes": octets,
                "artifact_sha256_expected": manifest.artifact_sha256,
                "artifact_sha256_observed": empreinte,
                "archive_entries": rapport.entries,
                "extracted_bytes": rapport.extracted_bytes,
                "release": str(release),
                "previous_release": str(precedente) if precedente else None,
                "reversible": True,
                "current_link": str(self.current_link),
                "binary": str(binaire),
                "binary_sha256": empreinte_binaire,
                "manifest": str(chemin_manifeste),
                "license": self.request.license_spdx,
                "license_notice": licence,
                "expected_build": manifest.build_number,
                "observed_build": version.build,
                "min_build": self.request.resolution.min_build,
                "evidence": variant.evidence,
                "degraded": self.request.resolution.degraded,
                "installed_at": installe_le,
            },
            findings=tuple(constats),
        )

    async def _sha_archive(
        self,
        archive: Path,
        manifest: runtime_resolver.ProvenanceManifest,
        octets: int,
    ) -> str:
        """
        Confronte l'archive reçue à l'empreinte ÉPINGLÉE, jamais à elle-même.

        `manifest.artifact_sha256` vient de la politique de release, en amont du
        téléchargement. Recalculer une empreinte sur ce qu'on vient de recevoir
        puis la comparer à cette même valeur recalculée ne prouverait rien du tout
        — c'est le contrôle d'intégrité qui se félicite lui-même.
        """
        attendu = manifest.artifact_sha256
        if not attendu:
            raise InstallError(
                "aucune empreinte épinglée pour cet artefact : l'installation refuse plutôt que "
                "de poser un binaire invérifiable (§6)"
            )
        reel = archive.stat().st_size
        if reel != octets:
            raise InstallError(
                f"le transport annonce {octets} octets et le fichier en fait {reel} : "
                "réception incohérente"
            )
        if reel == 0:
            raise InstallError("archive vide reçue : rien à vérifier ni à installer")
        empreinte = await _sha256_async(archive)
        if empreinte != attendu:
            raise InstallError(
                f"empreinte de l'archive non conforme à l'épinglage : attendu {attendu}, "
                f"reçu {empreinte} ({reel} octets). Rien n'a été extrait."
            )
        return empreinte

    # ── Rendus ────────────────────────────────────────────────────────────────

    def _document_manifeste(
        self,
        *,
        manifest: runtime_resolver.ProvenanceManifest,
        variant: runtime_resolver.ArtifactVariant,
        binaire: Path,
        empreinte_binaire: str,
        licence_posee: bool,
        precedente: Path | None,
        release: Path,
        observed_build: int | None,
        installe_le: str,
    ) -> dict[str, Any]:
        """
        Le manifeste §6 posé sur l'hôte, plus ce que §6 ne prévoit pas de champ pour.

        Le bloc `runtime:` reste **littéralement** celui de §6 — mêmes clés, même
        ordre — parce qu'il est relu par `runtime_resolver.validate_manifest_document`
        et, demain, par `doctor`. Ce que §6 ne nomme pas — licence, niveau de preuve,
        release précédente, empreinte du binaire posé — vit dans un bloc `install:`
        frère, où il n'a aucune chance de faire diverger le contrat.

        `evidence` est propagé depuis la variante : une installation retenue sur
        hypothèse doit rester identifiable des mois plus tard, quand plus personne
        ne se souviendra que la matrice d'artefacts mélangeait constats et
        suppositions.
        """
        pose = runtime_resolver.ProvenanceManifest(
            project=manifest.project,
            version=manifest.version,
            commit=manifest.commit,
            source=manifest.source,
            backend=manifest.backend,
            platform=manifest.platform,
            artifact_sha256=manifest.artifact_sha256,
            container_digest=manifest.container_digest,
            build_options=manifest.build_options,
            installed_at=installe_le,
        )
        document = pose.to_document()
        document["install"] = {
            "installer": USER_AGENT,
            "binary": str(binaire),
            "binary_sha256": empreinte_binaire,
            "artifact_url": sanitize_url(self.request.archive_url),
            "license": self.request.license_spdx,
            "license_notice_installed": licence_posee,
            "evidence": variant.evidence,
            "evidence_note": variant.evidence_note,
            "evidence_is_assumption": variant.evidence == runtime_resolver.EVIDENCE_ASSUMPTION,
            "degraded": self.request.resolution.degraded,
            "targeted_backend": self.request.resolution.targeted_backend,
            "min_build": self.request.resolution.min_build,
            "observed_build": observed_build,
            "planned_at": manifest.installed_at,
            "release": str(release),
            "previous_release": str(precedente) if precedente else None,
        }
        return document

    def _resume_simulation(
        self,
        manifest: runtime_resolver.ProvenanceManifest,
        variant: runtime_resolver.ArtifactVariant,
        racine: Path,
        raison: str,
    ) -> str:
        return (
            f"Téléchargerait {sanitize_url(self.request.archive_url)}, contrôlerait son sha256 contre "
            f"{manifest.artifact_sha256}, extrairait sous {racine}/{RELEASE_PREFIX}{manifest.version}-…, "
            f"relirait la version depuis le binaire posé (attendu build {manifest.build_number}) puis "
            f"basculerait {self.current_link}. Motif : {raison}. "
            f"Variante {variant.label()}. Aucune écriture, aucun téléchargement en simulation."
        )

    def _preuves_simulation(
        self,
        manifest: runtime_resolver.ProvenanceManifest,
        variant: runtime_resolver.ArtifactVariant,
        racine: Path,
        satisfait: _Idempotence,
    ) -> dict[str, Any]:
        return {
            "would_download": sanitize_url(self.request.archive_url),
            "artifact_sha256_expected": manifest.artifact_sha256,
            "install_root": str(racine),
            "current_link": str(self.current_link),
            "would_publish_binary": str(self.published_binary),
            "would_write_manifest": str(self.published_manifest),
            "previous_release": (
                str(_cible_courante(self.current_link)) if _cible_courante(self.current_link) else None
            ),
            "expected_build": manifest.build_number,
            "min_build": self.request.resolution.min_build,
            "variant": variant.label(),
            "evidence": variant.evidence,
            "degraded": self.request.resolution.degraded,
            "reason": satisfait.raison,
            "max_archive_bytes": self.request.limits.max_archive_bytes,
            "max_extracted_bytes": self.request.limits.max_extracted_bytes,
            "max_entries": self.request.limits.max_entries,
        }

    def _constats_contextuels(
        self, variant: runtime_resolver.ArtifactVariant
    ) -> tuple[schema.Finding, ...]:
        """
        Ce que l'exécution doit redire, même si le plan l'avait déjà dit.

        Un rapport d'exécution est lu seul, sans le plan à côté. Perdre en route
        « ce runtime est un CPU sur un hôte GPU » ou « cette variante repose sur une
        hypothèse » rendrait le journal rassurant à tort.
        """
        constats: list[schema.Finding] = []
        if self.request.resolution.degraded:
            constats.append(schema.Finding(
                code=CODE_DEGRADED,
                level="warn",
                message=(
                    "Le runtime installé est une variante CPU alors qu'un backend GPU était visé "
                    f"({self.request.resolution.targeted_backend}). Repli assumé par la politique : "
                    "l'installation paraîtra saine, le TTFT et le débit seront d'un ordre de grandeur "
                    "inférieurs et la VRAM déclarée dans models.yaml n'aura plus de sens."
                ),
            ))
        if variant.evidence == runtime_resolver.EVIDENCE_ASSUMPTION:
            constats.append(schema.Finding(
                code=CODE_EVIDENCE_ASSUMED,
                level="warn",
                message=(
                    f"La variante installée ({variant.label()}) repose sur une hypothèse, pas sur un "
                    f"constat vérifié : {variant.evidence_note} Le manifeste posé le consigne "
                    "(install.evidence_is_assumption)."
                ),
            ))
        return tuple(constats)

    def _echec(
        self,
        step: schema.PlanStep,
        context: execution.ExecutionContext,
        debut: float,
        code: str,
        message: str,
    ) -> execution.StepResult:
        """Échec consigné, jamais silencieux, et jamais publié à moitié."""
        propre = execution.redact_for_log(message)
        return execution.StepResult.for_step(
            step,
            status=execution.STEP_FAILED,
            summary=f"installation de llama-server refusée ou interrompue : {propre}",
            duration_ms=self._ms(context, debut),
            evidence={
                "current_link": str(self.current_link),
                "published_binary_unchanged": True,
                "code": code,
            },
            findings=(schema.Finding(code=code, level="fail", message=propre),),
            error=propre,
        )

    @staticmethod
    def _ms(context: execution.ExecutionContext, debut: float) -> int:
        return max(int((context.monotonic() - debut) * 1000), 0)


@dataclass(frozen=True)
class _Idempotence:
    """Verdict de la question « faut-il encore installer ? », avec sa raison."""
    ok: bool
    raison: str
    evidence: dict[str, Any]
    altere: bool = False


@dataclass(frozen=True)
class _RefusVersion:
    code: str
    message: str


def _verdict_version(
    binaire: Path,
    version: LlamaVersion,
    manifest: runtime_resolver.ProvenanceManifest,
    min_build: int,
) -> _RefusVersion | None:
    """
    Confronte la version relue sur le binaire POSÉ à l'épinglage. Fail-closed, sans exception.

    Voir le docstring du module (SEC-009) : `llama_version.enforce_llama_min_build`
    autorise le démarrage sur une version illisible, `doctor` non. Ici, l'artefact
    vient d'être téléchargé et posé par nous — l'indulgence n'a aucun sens, et un
    binaire muet est un binaire dont on ne sait rien.
    """
    attendu = manifest.build_number

    if version.build is None:
        return _RefusVersion(
            CODE_VERSION_UNREADABLE,
            f"la version de {binaire} est illisible ({version.raw[:120]}) alors que "
            f"{manifest.version} était attendue. Cet exécuteur est fail-closed sans exception : "
            "un binaire qu'on vient de poser et qui ne sait pas dire ce qu'il est n'est pas "
            "installé (§6, SEC-009). La release a été retirée, le lien courant n'a pas bougé.",
        )

    if version.build <= runtime_resolver.SHALLOW_CLONE_MAX_BUILD:
        return _RefusVersion(
            CODE_SHALLOW_CLONE,
            f"{binaire} se déclare « version: {version.build} ». Ce n'est pas un build ancien : "
            "c'est la signature d'un `git clone --depth 1` de llama.cpp, qui prive la construction "
            "du compte de révisions (§0.10). Reconstruisez depuis un clone complet — mettre à jour "
            "le dépôt ne changera rien.",
        )

    if version.build != attendu:
        return _RefusVersion(
            CODE_VERSION_MISMATCH,
            f"le binaire posé rend le build {version.build} alors que l'épinglage attend "
            f"{attendu} ({manifest.version}), pour une archive dont l'empreinte est pourtant "
            "conforme. Anomalie de chaîne d'approvisionnement : l'installation est annulée.",
        )

    if min_build > 0 and version.build < min_build:
        return _RefusVersion(
            CODE_BUILD_TOO_OLD,
            f"le binaire posé rend le build {version.build}, sous le plancher de sécurité "
            f"{min_build} (LLAMA_SERVER_MIN_BUILD) : binaire potentiellement vulnérable "
            "(GHSA-8947-pfff-2f3c). L'installation est annulée.",
        )

    return None


# ── Système de fichiers ───────────────────────────────────────────────────────

def _stamp(iso: str) -> str:
    """Horodatage ISO réduit à ce qu'un nom de répertoire accepte partout."""
    return "".join(c for c in str(iso) if c.isalnum()) or "sansdate"


def _chemin_libre(racine: Path, base: str) -> Path:
    """Premier chemin libre sous `racine`. Deux installations dans la même seconde ne se marchent pas dessus."""
    candidat = racine / base
    index = 1
    while candidat.exists() or candidat.is_symlink():
        candidat = racine / f"{base}-{index}"
        index += 1
    return candidat


def _cible_courante(lien: Path) -> Path | None:
    """Release actuellement publiée, ou `None`. C'est elle qui rend l'étape réversible."""
    if not lien.is_symlink():
        return None
    try:
        return Path(os.readlink(lien))
    except OSError:  # pragma: no cover - lien disparu entre les deux appels
        return None


def _localiser_binaire(arbre: Path, nom: str) -> Path:
    """
    Trouve le binaire attendu dans l'arbre extrait, ou refuse.

    Une archive légitime le range parfois sous `build/bin/`, parfois à la racine.
    Chercher est donc nécessaire ; deviner ne l'est pas : plusieurs candidats ou
    aucun sont deux refus, pas deux occasions de choisir au hasard.
    """
    trouves = sorted(p for p in arbre.rglob(nom) if p.is_file() and not p.is_symlink())
    if not trouves:
        raise InstallError(
            f"l'archive ne contient aucun exécutable « {nom} » : ce n'est pas l'artefact attendu"
        )
    if len(trouves) > 1:
        raise InstallError(
            f"l'archive contient {len(trouves)} exécutables « {nom} » "
            f"({', '.join(str(p.relative_to(arbre)) for p in trouves)}) : impossible de décider "
            "lequel installer sans deviner"
        )
    return trouves[0]


def _aplatir(arbre: Path, incubation: Path) -> None:
    """
    Remonte le contenu extrait d'un cran, pour que la release soit l'arbre lui-même.

    L'extraction se fait dans un sous-répertoire afin que l'archive téléchargée ne
    puisse jamais être écrasée par une entrée du même nom — l'archive est effacée
    avant ce déplacement, et le sous-répertoire disparaît avec.
    """
    for enfant in list(arbre.iterdir()):
        os.replace(enfant, incubation / enfant.name)
    arbre.rmdir()


def _licence_installee(release: Path) -> str | None:
    """Nom de la notice de licence trouvée auprès du binaire, ou `None` (§6)."""
    for candidat in LICENSE_CANDIDATES:
        for trouve in release.rglob(candidat):
            if trouve.is_file():
                return trouve.name
    return None


def _basculer(lien: Path, cible: Path) -> None:
    """
    Publie la nouvelle release par un unique renommage atomique du lien.

    `os.symlink` sur un chemin existant échoue, et `unlink` puis `symlink`
    laisserait une fenêtre — courte, mais réelle — où plus rien n'est publié. On
    crée donc un lien temporaire puis on le renomme PAR-DESSUS l'ancien :
    `os.replace` sur un lien symbolique est atomique, et `current` désigne
    l'ancienne release ou la nouvelle, jamais rien.
    """
    provisoire = _chemin_libre(lien.parent, f".{lien.name}.nouveau")
    os.symlink(cible, provisoire)
    os.replace(provisoire, lien)


def _relire_manifeste(chemin: Path) -> None:
    """Relit et valide le manifeste qu'on vient d'écrire. Ce qui est publié est recoupé."""
    document = yaml.safe_load(chemin.read_text(encoding="utf-8"))
    erreurs = runtime_resolver.validate_manifest_document(document)
    if erreurs:
        raise InstallError(
            "le manifeste de provenance écrit est incohérent : " + " ; ".join(erreurs)
        )
    schema.assert_no_secrets(document)


def _nettoyer(incubation: Path, release: Path | None) -> None:
    """Retire tout ce qui n'a pas été publié. `current` n'est jamais touché ici."""
    shutil.rmtree(incubation, ignore_errors=True)
    if release is not None:
        shutil.rmtree(release, ignore_errors=True)


# ── Enregistrement ────────────────────────────────────────────────────────────

def register_runtime_installer(
    registry: execution.ExecutorRegistry,
    installer: RuntimeInstaller,
) -> None:
    """
    Branche l'exécuteur sur `ACTION_INSTALL_RUNTIME`. Un seul par registre, par contrat.

    `ExecutorRegistry.register` refuse le second enregistrement plutôt que d'écraser
    le premier : deux installateurs concurrents pour la même action doivent le faire
    savoir au démarrage, pas en production.
    """
    registry.register(schema.ACTION_INSTALL_RUNTIME, installer)
