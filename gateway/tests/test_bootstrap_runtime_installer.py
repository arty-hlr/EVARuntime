"""
AUT-016 — régressions de l'installateur de runtime (`bootstrap/runtime_installer.py`).

Cet exécuteur est le premier du parcours M2 qui **écrit sur l'hôte** : il pose un
binaire téléchargé, le rend exécutable et le publie. Les tests verrouillent donc
cinq familles d'invariants, dans l'ordre où ils protègent quelque chose :

1. **rien n'entre sans preuve** — URL HTTPS sans identifiants, archive confrontée
   au SHA-256 de l'épinglage, version relue sur le binaire posé et confrontée à
   l'épinglage ;
2. **une archive est une entrée hostile** — traversée de chemin, liens sortants,
   entrées exotiques, bombes de décompression et bits `setuid` sont refusés
   explicitement, avec des archives fabriquées ici ;
3. **rien n'est publié à moitié** — tout refus laisse `current` inchangé et ne
   laisse aucune release résiduelle ;
4. **idempotence et réversibilité** — une seconde passe ne télécharge rien, et
   l'installation conserve la release précédente en le disant dans ses preuves ;
5. **aucun secret, aucune écriture en simulation**.

Aucun test ne touche le réseau, ne lance de sous-processus ni n'écrit hors de
`tmp_path` : transport, sonde de version, horloge et racines autorisées sont
tous injectés.

Chaque test d'ABSENCE porte son contrôle positif — un test qui affirme « aucune
release résiduelle » ou « pas d'avertissement de licence » sans prouver qu'il
saurait en voir un passerait au vert le jour où la garde deviendrait inerte.
"""
from __future__ import annotations

import ast
import asyncio
import hashlib
import io
import json
import os
import socket
import stat
import tarfile
import zipfile
from pathlib import Path

import pytest
import yaml

from bootstrap import execution as ex
from bootstrap import runtime_installer as ri
from bootstrap import runtime_resolver as rr
from bootstrap import schema as sc
from llama_version import LlamaVersion

VERSION = "b6500"
BUILD = 6500
COMMIT = "abc1234def5678"
PLATFORM = "linux-x86_64"
NOW_PLAN = "2026-08-01T09:00:00Z"
NOW_EXEC = "2026-08-01T10:30:00Z"
URL = "https://example.invalid/llama/llama-server-b6500-linux-x86_64.tar.gz"

# Construit à l'exécution : un littéral ressemblant à un vrai jeton n'a rien à
# faire dans un dépôt, même en fixture.
FAUX_TOKEN = "hf_" + "B" * 24


# ── Fabriques d'archives ──────────────────────────────────────────────────────

def _fichier(tar: tarfile.TarFile, nom: str, data: bytes, mode: int = 0o644) -> None:
    info = tarfile.TarInfo(nom)
    info.size = len(data)
    info.mode = mode
    tar.addfile(info, io.BytesIO(data))


def _lien(tar: tarfile.TarFile, nom: str, cible: str, *, symbolique: bool = True) -> None:
    info = tarfile.TarInfo(nom)
    info.type = tarfile.SYMTYPE if symbolique else tarfile.LNKTYPE
    info.linkname = cible
    tar.addfile(info)


def _exotique(tar: tarfile.TarFile, nom: str, type_: bytes = tarfile.FIFOTYPE) -> None:
    info = tarfile.TarInfo(nom)
    info.type = type_
    tar.addfile(info)


def _repertoire(tar: tarfile.TarFile, nom: str) -> None:
    info = tarfile.TarInfo(nom)
    info.type = tarfile.DIRTYPE
    info.mode = 0o755
    tar.addfile(info)


def _tar_bytes(remplir) -> bytes:
    """Rend les octets d'un `.tar.gz` construit par le rappel `remplir(tar)`."""
    tampon = io.BytesIO()
    with tarfile.open(fileobj=tampon, mode="w:gz") as tar:
        remplir(tar)
    return tampon.getvalue()


def _archive_nominale(
    *,
    contenu: bytes = b"#!/bin/false\nfaux llama-server\n",
    licence: bool = True,
    nom: str = "llama-server",
    prefixe: str = "",
) -> bytes:
    """Archive plausible : le binaire, une notice MIT, un lien interne, un sous-répertoire."""
    def remplir(tar: tarfile.TarFile) -> None:
        if prefixe:
            _repertoire(tar, prefixe.rstrip("/"))
        _fichier(tar, f"{prefixe}{nom}", contenu, mode=0o755)
        _fichier(tar, f"{prefixe}libggml.so.0", b"faux objet partage")
        _lien(tar, f"{prefixe}libggml.so", "libggml.so.0")
        if licence:
            _fichier(tar, f"{prefixe}LICENSE", b"MIT License\n")
    return _tar_bytes(remplir)


def _ecrire(tmp_path: Path, nom: str, data: bytes) -> Path:
    chemin = tmp_path / nom
    chemin.write_bytes(data)
    return chemin


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ── Fabriques de décision ─────────────────────────────────────────────────────

def _variante(
    *,
    source: str = rr.SOURCE_OFFICIAL_RELEASE,
    backend: str = rr.BACKEND_CPU,
    sha: str | None = None,
    evidence: str = rr.EVIDENCE_SPEC,
) -> rr.ArtifactVariant:
    return rr.ArtifactVariant(
        source=source,
        backend=backend,
        platform=PLATFORM,
        evidence=evidence,
        evidence_note="Entrée de matrice fabriquée pour les tests.",
        reference="https://example.invalid/llama/releases",
        artifact_sha256=sha,
    )


def _manifeste(
    *,
    source: str = rr.SOURCE_OFFICIAL_RELEASE,
    backend: str = rr.BACKEND_CPU,
    sha: str | None = None,
    version: str = VERSION,
    build_options: dict | None = None,
) -> rr.ProvenanceManifest:
    return rr.ProvenanceManifest(
        version=version,
        commit=COMMIT,
        source=source,
        backend=backend,
        platform=PLATFORM,
        artifact_sha256=sha,
        build_options=build_options or {},
        installed_at=NOW_PLAN,
    )


def _resolution(
    *,
    sha: str | None = None,
    variant: rr.ArtifactVariant | None = None,
    manifest: rr.ProvenanceManifest | None = None,
    resolved: bool = True,
    reuse_existing: bool = False,
    degraded: bool = False,
    min_build: int = 0,
    targeted_backend: str | None = None,
) -> rr.RuntimeResolution:
    variante = variant if variant is not None else _variante(sha=sha)
    return rr.RuntimeResolution(
        profile=rr.HardwareProfile(platform=PLATFORM, backend_candidates=("cpu",)),
        min_build=min_build,
        resolved=resolved,
        reuse_existing=reuse_existing,
        degraded=degraded,
        targeted_backend=targeted_backend,
        variant=variante,
        manifest=manifest if manifest is not None else _manifeste(
            source=variante.source, backend=variante.backend, sha=sha,
        ),
        observed_build=None,
        summary="résolution fabriquée pour les tests",
        findings=(),
        rejected=(),
    )


def _etape(*, version: str = VERSION, backend: str = rr.BACKEND_CPU, order: int = 1) -> sc.PlanStep:
    return sc.PlanStep(
        order=order,
        action=sc.ACTION_INSTALL_RUNTIME,
        target=f"llama-server {version} ({backend})",
        detail="Installer l'artefact vérifié et écrire le manifeste de provenance.",
        requires_root=True,
        reversible=True,
    )


class _FauxTransport:
    """Transport injecté : sert des octets préparés, compte ses appels, ne joint rien."""

    def __init__(self, payload: bytes = b"", *, erreur: Exception | None = None) -> None:
        self.payload = payload
        self.erreur = erreur
        self.appels: list[str] = []

    async def fetch(self, url: str, destination: Path, *, max_bytes: int) -> int:
        self.appels.append(url)
        if self.erreur is not None:
            raise self.erreur
        if len(self.payload) > max_bytes:
            raise ri.InstallError(f"archive de {len(self.payload)} octets au-delà de {max_bytes}")
        destination.write_bytes(self.payload)
        return len(self.payload)


class _FausseReponseHTTPS:
    """Réponse HTTP injectée pour exercer le vrai transport sans socket."""

    def __init__(
        self,
        status: int,
        *,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self._chunks = [body] if body else []
        self._headers = {key.lower(): value for key, value in (headers or {}).items()}
        self.closed = False

    def getheader(self, name: str) -> str | None:
        return self._headers.get(name.lower())

    def read(self, _size: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""

    def close(self) -> None:
        self.closed = True


class _FausseConnexionHTTPS:
    def __init__(self, response: _FausseReponseHTTPS) -> None:
        self.response = response
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.closed = False

    def request(self, method: str, url: str, *, headers: dict[str, str]) -> None:
        self.requests.append((method, url, headers))

    def getresponse(self) -> _FausseReponseHTTPS:
        return self.response

    def close(self) -> None:
        self.closed = True


class _FabriqueConnexionsHTTPS:
    def __init__(self, responses: list[_FausseReponseHTTPS]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, int, tuple[ri.AddressInfo, ...], float]] = []
        self.connections: list[_FausseConnexionHTTPS] = []

    def __call__(
        self,
        host: str,
        port: int,
        addresses: tuple[ri.AddressInfo, ...],
        timeout: float,
    ) -> _FausseConnexionHTTPS:
        self.calls.append((host, port, tuple(addresses), timeout))
        connection = _FausseConnexionHTTPS(self._responses.pop(0))
        self.connections.append(connection)
        return connection


def _adresse(ip: str) -> ri.AddressInfo:
    if ":" in ip:
        return (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 443, 0, 0))
    return (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 443))


def _sonde(build: int | None, raw: str = "version: 6500 (abc1234)"):
    """Sonde de version injectée. Aucun sous-processus n'est jamais lancé dans ces tests."""
    async def probe(binary: Path) -> LlamaVersion:
        return LlamaVersion(build=build, raw=raw)
    return probe


def _contexte(mode: ex.ExecutionMode, racine: Path, journal: list[str] | None = None) -> ex.ExecutionContext:
    ticks = iter(range(0, 10_000))
    return ex.ExecutionContext(
        mode=mode,
        allowed_roots=(racine,),
        monotonic=lambda: float(next(ticks)) / 1000.0,
        now=lambda: NOW_EXEC,
        log=(journal.append if journal is not None else (lambda _m: None)),
    )


def _installateur(
    tmp_path: Path,
    *,
    payload: bytes | None = None,
    sha: str | None = None,
    build: int | None = BUILD,
    resolution: rr.RuntimeResolution | None = None,
    url: str = URL,
    limits: ri.ArchiveLimits | None = None,
    transport: object | None = None,
) -> tuple[ri.RuntimeInstaller, _FauxTransport]:
    octets = payload if payload is not None else _archive_nominale()
    faux = transport if transport is not None else _FauxTransport(octets)
    requete = ri.RuntimeInstallRequest(
        resolution=resolution if resolution is not None else _resolution(sha=sha or _sha(octets)),
        archive_url=url,
        install_root=tmp_path / "runtime",
        limits=limits or ri.ArchiveLimits(),
    )
    return ri.RuntimeInstaller(requete, transport=faux, probe=_sonde(build)), faux  # type: ignore[arg-type]


def _appliquer(installateur: ri.RuntimeInstaller, contexte: ex.ExecutionContext, etape=None):
    return asyncio.run(installateur(etape or _etape(), contexte))


# ══ 1. URL d'artefact ═════════════════════════════════════════════════════════

@pytest.mark.parametrize("url", [
    "http://example.invalid/a.tar.gz",
    "file:///etc/passwd",
    "ftp://example.invalid/a.tar.gz",
    "example.invalid/a.tar.gz",
    "https:///a.tar.gz",
])
def test_url_refuse_tout_ce_qui_n_est_pas_https(url):
    """Un artefact en clair ou lu sur le disque n'est pas celui qu'épingle la politique."""
    with pytest.raises(ri.InstallError):
        ri.validate_artifact_url(url)


def test_url_https_nominale_acceptee():
    """Contrôle positif : le validateur n'est pas un refus universel."""
    assert ri.validate_artifact_url(URL) == URL


def test_url_refuse_les_identifiants_dans_l_autorite():
    """Un secret dans une URL finit dans un journal, un rapport, et un jour dans argv."""
    with pytest.raises(ri.InstallError) as exc:
        ri.validate_artifact_url(f"https://utilisateur:{FAUX_TOKEN}@example.invalid/a.tar.gz")
    assert "identifiants" in str(exc.value)


def test_sanitize_url_retire_la_requete_et_garde_l_origine():
    """La forme publiable perd le jeton signé, pas l'information utile."""
    propre = ri.sanitize_url(f"https://example.invalid:8443/llama/a.tar.gz?token={FAUX_TOKEN}#frag")
    assert FAUX_TOKEN not in propre
    # Contrôle positif : si la fonction rendait une constante, l'assertion
    # d'absence ci-dessus passerait tout autant.
    assert propre == "https://example.invalid:8443/llama/a.tar.gz"


@pytest.mark.parametrize("host", [
    "localhost",
    "api.localhost",
    "127.0.0.1",        # loopback
    "10.42.0.1",        # privée
    "100.64.0.1",       # plage partagée, non publique
    "169.254.169.254",  # link-local et métadonnées cloud
    "224.0.0.1",        # multicast
    "0.0.0.0",          # unspecified
    "192.0.2.1",        # réservée à la documentation
    "[::1]",            # loopback IPv6
    "[fd00::1]",        # privée IPv6
    "[fe80::1]",        # link-local IPv6
    "[ff02::1]",        # multicast IPv6
    "[::]",             # unspecified IPv6
    "[2001:db8::1]",    # réservée à la documentation
])
def test_url_refuse_les_adresses_non_publiques(host):
    with pytest.raises(ri.InstallError) as exc:
        ri.validate_artifact_url(f"https://{host}/a.tar.gz")
    assert "refus" in str(exc.value)


def test_url_accepte_une_adresse_publique():
    """Contrôle positif : la garde IP ne refuse pas tout littéral."""
    url = "https://8.8.8.8/a.tar.gz"
    assert ri.validate_artifact_url(url) == url


def test_transport_refuse_une_resolution_dns_privee_avant_connexion(tmp_path):
    factory = _FabriqueConnexionsHTTPS([])
    transport = ri.UrllibTransport(
        resolver=lambda _host, _port: (_adresse("10.0.0.7"),),
        connection_factory=factory,
    )

    with pytest.raises(ri.InstallError) as exc:
        asyncio.run(transport.fetch(URL, tmp_path / "archive", max_bytes=100))

    assert "privée" in str(exc.value)
    assert factory.calls == []
    assert not (tmp_path / "archive").exists()


def test_transport_refuse_toute_resolution_dns_mixte(tmp_path):
    """Une réponse publique + privée ne doit pas dépendre de son ordre."""
    factory = _FabriqueConnexionsHTTPS([])
    transport = ri.UrllibTransport(
        resolver=lambda _host, _port: (
            _adresse("93.184.216.34"),
            _adresse("169.254.169.254"),
        ),
        connection_factory=factory,
    )

    with pytest.raises(ri.InstallError):
        asyncio.run(transport.fetch(URL, tmp_path / "archive", max_bytes=100))

    assert factory.calls == []


def test_transport_suit_une_redirection_https_apres_validation(tmp_path):
    responses = [
        _FausseReponseHTTPS(302, headers={"Location": "https://cdn.example.invalid/final?sig=x"}),
        _FausseReponseHTTPS(200, body=b"archive"),
    ]
    factory = _FabriqueConnexionsHTTPS(responses)
    resolutions: list[tuple[str, int]] = []

    def resolve(host: str, port: int):
        resolutions.append((host, port))
        return (_adresse("93.184.216.34"),)

    transport = ri.UrllibTransport(resolver=resolve, connection_factory=factory)
    destination = tmp_path / "archive"
    written = asyncio.run(transport.fetch(URL, destination, max_bytes=100))

    assert written == len(b"archive")
    assert destination.read_bytes() == b"archive"
    assert resolutions == [("example.invalid", 443), ("cdn.example.invalid", 443)]
    assert [call[:2] for call in factory.calls] == [
        ("example.invalid", 443),
        ("cdn.example.invalid", 443),
    ]
    assert factory.connections[0].requests[0][:2] == (
        "GET", "/llama/llama-server-b6500-linux-x86_64.tar.gz",
    )
    assert factory.connections[1].requests[0][:2] == ("GET", "/final?sig=x")
    assert all(connection.closed for connection in factory.connections)
    assert all(response.closed for response in responses)


@pytest.mark.parametrize("location", [
    "http://cdn.example.invalid/archive",
    "file:///etc/passwd",
    "https://127.0.0.1/archive",
    "https://[::1]/archive",
])
def test_transport_refuse_une_redirection_dangereuse_avant_emission(tmp_path, location):
    response = _FausseReponseHTTPS(302, headers={"Location": location})
    factory = _FabriqueConnexionsHTTPS([response])
    resolutions: list[str] = []

    def resolve(host: str, _port: int):
        resolutions.append(host)
        return (_adresse("93.184.216.34"),)

    transport = ri.UrllibTransport(resolver=resolve, connection_factory=factory)
    with pytest.raises(ri.InstallError):
        asyncio.run(transport.fetch(URL, tmp_path / "archive", max_bytes=100))

    # Seule la source initiale a été résolue et contactée. La cible du 3xx
    # est refusée pendant le traitement de la réponse, avant l'itération suivante.
    assert resolutions == ["example.invalid"]
    assert len(factory.calls) == 1
    assert not (tmp_path / "archive").exists()


def test_transport_refuse_le_dns_prive_d_une_redirection_avant_connexion(tmp_path):
    response = _FausseReponseHTTPS(
        307, headers={"Location": "https://internal.example.invalid/archive"},
    )
    factory = _FabriqueConnexionsHTTPS([response])

    def resolve(host: str, _port: int):
        ip = "93.184.216.34" if host == "example.invalid" else "192.168.1.9"
        return (_adresse(ip),)

    transport = ri.UrllibTransport(resolver=resolve, connection_factory=factory)
    with pytest.raises(ri.InstallError):
        asyncio.run(transport.fetch(URL, tmp_path / "archive", max_bytes=100))

    assert len(factory.calls) == 1
    assert factory.calls[0][0] == "example.invalid"


def test_transport_borne_les_redirections_avant_la_connexion_suivante(tmp_path):
    responses = [
        _FausseReponseHTTPS(302, headers={"Location": "/encore"}),
        _FausseReponseHTTPS(307, headers={"Location": "/toujours"}),
    ]
    factory = _FabriqueConnexionsHTTPS(responses)
    transport = ri.UrllibTransport(
        max_redirects=1,
        resolver=lambda _host, _port: (_adresse("93.184.216.34"),),
        connection_factory=factory,
    )

    with pytest.raises(ri.InstallError) as exc:
        asyncio.run(transport.fetch(URL, tmp_path / "archive", max_bytes=100))

    assert "trop de redirections" in str(exc.value)
    assert len(factory.calls) == 2
    assert all(connection.closed for connection in factory.connections)


def test_transport_epingle_la_resolution_validee_contre_le_dns_rebinding(tmp_path):
    """Le résolveur n'est appelé qu'une fois et son adresse est passée au connecteur."""
    response = _FausseReponseHTTPS(200, body=b"ok")
    factory = _FabriqueConnexionsHTTPS([response])
    answers = iter([
        (_adresse("93.184.216.34"),),
        (_adresse("127.0.0.1"),),
    ])
    resolver_calls = 0

    def resolve(_host: str, _port: int):
        nonlocal resolver_calls
        resolver_calls += 1
        return next(answers)

    transport = ri.UrllibTransport(resolver=resolve, connection_factory=factory)
    destination = tmp_path / "archive"
    asyncio.run(transport.fetch(URL, destination, max_bytes=100))

    assert resolver_calls == 1
    assert factory.calls[0][2] == (_adresse("93.184.216.34"),)
    assert destination.read_bytes() == b"ok"


def test_connexion_epinglee_contacte_l_ip_validee_et_garde_le_sni(monkeypatch):
    """Le connecteur réel ne relance pas une résolution via HTTPConnection."""
    events: list[tuple[str, object]] = []

    class FauxSocket:
        def settimeout(self, timeout):
            events.append(("timeout", timeout))

        def bind(self, source):
            events.append(("bind", source))

        def connect(self, address):
            events.append(("connect", address))

        def close(self):
            events.append(("close", None))

    class FauxContexteTLS:
        def wrap_socket(self, sock, *, server_hostname):
            events.append(("sni", server_hostname))
            return sock

    monkeypatch.setattr(ri.socket, "socket", lambda *_args: FauxSocket())
    connection = ri._PinnedHTTPSConnection(
        "downloads.example.invalid",
        443,
        (_adresse("93.184.216.34"),),
        12.5,
    )
    connection._context = FauxContexteTLS()
    connection.connect()

    assert ("connect", ("93.184.216.34", 443)) in events
    assert ("sni", "downloads.example.invalid") in events
    assert all(event[0] != "bind" for event in events)


# ══ 2. Extraction défensive ═══════════════════════════════════════════════════

def test_extraction_nominale_rend_un_rapport_fidele(tmp_path):
    """Contrôle positif de toute la section : une archive saine s'extrait entièrement."""
    archive = _ecrire(tmp_path, "a.tar.gz", _archive_nominale())
    rapport = ri.extract_archive(archive, tmp_path / "out")
    assert rapport.entries == 4
    assert (tmp_path / "out" / "llama-server").is_file()
    assert (tmp_path / "out" / "libggml.so").is_symlink()
    assert rapport.extracted_bytes > 0


def test_extraction_refuse_la_traversee_de_chemin(tmp_path):
    archive = _ecrire(tmp_path, "a.tar.gz", _tar_bytes(
        lambda tar: _fichier(tar, "../evade", b"x")
    ))
    with pytest.raises(ri.ArchiveRefused) as exc:
        ri.extract_archive(archive, tmp_path / "out")
    assert "traversée" in str(exc.value)
    assert not (tmp_path / "evade").exists()


def test_extraction_refuse_un_chemin_absolu(tmp_path):
    archive = _ecrire(tmp_path, "a.tar.gz", _tar_bytes(
        lambda tar: _fichier(tar, "/etc/evade", b"x")
    ))
    with pytest.raises(ri.ArchiveRefused) as exc:
        ri.extract_archive(archive, tmp_path / "out")
    assert "absolu" in str(exc.value)


def test_extraction_refuse_un_chemin_absolu_windows(tmp_path):
    archive = _ecrire(tmp_path, "a.tar.gz", _tar_bytes(
        lambda tar: _fichier(tar, "C:\\Windows\\evade", b"x")
    ))
    with pytest.raises(ri.ArchiveRefused) as exc:
        ri.extract_archive(archive, tmp_path / "out")
    assert "absolu" in str(exc.value)


@pytest.mark.parametrize("cible", ["/etc/passwd", "../../evade", "..\\..\\evade"])
def test_extraction_refuse_un_lien_symbolique_sortant(tmp_path, cible):
    archive = _ecrire(tmp_path, "a.tar.gz", _tar_bytes(
        lambda tar: _lien(tar, "piege", cible)
    ))
    with pytest.raises(ri.ArchiveRefused) as exc:
        ri.extract_archive(archive, tmp_path / "out")
    assert "lien" in str(exc.value)


def test_extraction_accepte_un_lien_symbolique_interne(tmp_path):
    """Contrôle positif : la garde vise ce qui sort, pas les liens légitimes."""
    def remplir(tar):
        _fichier(tar, "libggml.so.0", b"objet")
        _lien(tar, "sous/libggml.so", "../libggml.so.0")
    archive = _ecrire(tmp_path, "a.tar.gz", _tar_bytes(remplir))
    ri.extract_archive(archive, tmp_path / "out")
    assert (tmp_path / "out" / "sous" / "libggml.so").is_symlink()


def test_extraction_refuse_d_ecrire_au_travers_d_un_lien(tmp_path):
    """
    Deuxième barrière : un lien accepté ne doit pas servir de tremplin.

    Le lien est ici posé vers un répertoire INTERNE légitime, puis l'archive tente
    d'écrire au travers d'un lien déjà présent dans la destination. Le parent réel
    est recoupé avant chaque écriture.
    """
    destination = tmp_path / "out"
    destination.mkdir()
    (tmp_path / "dehors").mkdir()
    os.symlink(tmp_path / "dehors", destination / "pont")

    archive = _ecrire(tmp_path, "a.tar.gz", _tar_bytes(
        lambda tar: _fichier(tar, "pont/evade", b"x")
    ))
    with pytest.raises(ri.ArchiveRefused) as exc:
        ri.extract_archive(archive, destination)
    assert "lien symbolique" in str(exc.value)
    assert not (tmp_path / "dehors" / "evade").exists()


def test_extraction_refuse_un_lien_physique_sortant(tmp_path):
    archive = _ecrire(tmp_path, "a.tar.gz", _tar_bytes(
        lambda tar: _lien(tar, "piege", "../../etc/passwd", symbolique=False)
    ))
    with pytest.raises(ri.ArchiveRefused):
        ri.extract_archive(archive, tmp_path / "out")


def test_extraction_refuse_une_entree_exotique(tmp_path):
    """Un FIFO, un périphérique ou une socket n'ont rien à faire dans un artefact."""
    archive = _ecrire(tmp_path, "a.tar.gz", _tar_bytes(
        lambda tar: _exotique(tar, "tuyau")
    ))
    with pytest.raises(ri.ArchiveRefused) as exc:
        ri.extract_archive(archive, tmp_path / "out")
    assert "non installable" in str(exc.value)


def test_extraction_borne_le_nombre_d_entrees(tmp_path):
    def remplir(tar):
        for index in range(6):
            _fichier(tar, f"f{index}", b"x")
    archive = _ecrire(tmp_path, "a.tar.gz", _tar_bytes(remplir))
    with pytest.raises(ri.ArchiveRefused) as exc:
        ri.extract_archive(archive, tmp_path / "out", limits=ri.ArchiveLimits(max_entries=3))
    assert "entrées" in str(exc.value)


def test_extraction_borne_la_taille_decompressee(tmp_path):
    archive = _ecrire(tmp_path, "a.tar.gz", _tar_bytes(
        lambda tar: _fichier(tar, "gros", b"\0" * 5000)
    ))
    with pytest.raises(ri.ArchiveRefused) as exc:
        ri.extract_archive(
            archive, tmp_path / "out", limits=ri.ArchiveLimits(max_extracted_bytes=100),
        )
    assert "bombe" in str(exc.value)
    assert not (tmp_path / "out" / "gros").exists()


def test_extraction_borne_le_ratio_de_compression(tmp_path):
    """Un ratio délirant est refusé même sous la borne absolue de taille."""
    archive = _ecrire(tmp_path, "a.tar.gz", _tar_bytes(
        lambda tar: _fichier(tar, "gros", b"\0" * 200_000)
    ))
    with pytest.raises(ri.ArchiveRefused):
        ri.extract_archive(
            archive,
            tmp_path / "out",
            limits=ri.ArchiveLimits(max_extracted_bytes=10 ** 9, max_ratio=2),
        )


def test_copie_bornee_coupe_a_l_ecriture_sans_croire_l_entete(tmp_path):
    """
    La seconde barrière est testée pour elle-même : elle ne consulte aucune taille déclarée.

    Sans ce test, seule la barrière « taille annoncée » serait couverte, et celle
    qui compte les octets réellement écrits pourrait disparaître sans rougir.
    """
    cible = tmp_path / "sortie"
    with pytest.raises(ri.ArchiveRefused):
        ri._copy_bounded(io.BytesIO(b"y" * 500), cible, 10)
    assert not cible.exists()

    # Contrôle positif : sous la borne, la copie a bien lieu.
    assert ri._copy_bounded(io.BytesIO(b"y" * 5), cible, 10) == 5
    assert cible.read_bytes() == b"y" * 5


def test_taille_declaree_refusee_avant_ouverture_du_flux(tmp_path):
    """
    La première barrière est testée pour elle-même, hors de la seconde.

    Le protocole de mutation l'a montré : neutralisée seule, elle ne faisait
    tomber aucun test — la borne à l'écriture la masquait entièrement. Une garde
    que rien ne fait rougir n'est pas testée.
    """
    with pytest.raises(ri.ArchiveRefused) as exc:
        ri._refuser_taille_declaree("gros", 5000, 100)
    assert "bombe" in str(exc.value)
    # Contrôle positif : sous le budget, elle laisse passer.
    assert ri._refuser_taille_declaree("petit", 50, 100) is None


def test_extraction_ne_propage_jamais_setuid(tmp_path):
    """Les modes de l'archive ne sont pas des ordres : setuid/setgid ne franchissent pas la barrière."""
    archive = _ecrire(tmp_path, "a.tar.gz", _tar_bytes(
        lambda tar: _fichier(tar, "outil", b"x", mode=0o4755)
    ))
    ri.extract_archive(archive, tmp_path / "out")
    mode = (tmp_path / "out" / "outil").stat().st_mode
    assert not mode & stat.S_ISUID
    assert not mode & stat.S_ISGID
    # Contrôle positif : le fichier existe bien et porte le mode imposé.
    assert stat.S_IMODE(mode) == 0o644


def test_extraction_zip_refuse_un_lien_symbolique(tmp_path):
    """
    ZipFile poserait le lien comme un fichier contenant le chemin de sa cible.

    Une installation subtilement cassée est pire qu'un refus : elle démarre.
    """
    chemin = tmp_path / "a.zip"
    with zipfile.ZipFile(chemin, "w") as zf:
        info = zipfile.ZipInfo("lien")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(info, "libggml.so.0")
    with pytest.raises(ri.ArchiveRefused) as exc:
        ri.extract_archive(chemin, tmp_path / "out")
    assert "ZIP" in str(exc.value)


def test_extraction_zip_nominale(tmp_path):
    """Contrôle positif du chemin ZIP : sans lui, le test ci-dessus prouverait juste un refus global."""
    chemin = tmp_path / "a.zip"
    with zipfile.ZipFile(chemin, "w") as zf:
        zf.writestr("llama-server", "binaire")
    rapport = ri.extract_archive(chemin, tmp_path / "out")
    assert rapport.entries == 1
    assert (tmp_path / "out" / "llama-server").is_file()


def test_extraction_zip_refuse_la_traversee(tmp_path):
    chemin = tmp_path / "a.zip"
    with zipfile.ZipFile(chemin, "w") as zf:
        zf.writestr("../evade", "x")
    with pytest.raises(ri.ArchiveRefused):
        ri.extract_archive(chemin, tmp_path / "out")


def test_extraction_refuse_un_format_inconnu(tmp_path):
    archive = _ecrire(tmp_path, "a.bin", b"ni tar ni zip")
    with pytest.raises(ri.ArchiveRefused) as exc:
        ri.extract_archive(archive, tmp_path / "out")
    assert "format" in str(exc.value)


def test_extraction_refuse_une_archive_trop_grosse(tmp_path):
    archive = _ecrire(tmp_path, "a.tar.gz", _archive_nominale())
    with pytest.raises(ri.ArchiveRefused) as exc:
        ri.extract_archive(archive, tmp_path / "out", limits=ri.ArchiveLimits(max_archive_bytes=10))
    assert "au-delà de la borne" in str(exc.value)


def test_le_module_n_appelle_jamais_extractall():
    """
    Assertion d'ABSENCE sur l'AST, avec son contrôle positif.

    `extractall()` est l'API dangereuse : c'est elle qui applique — ou non, selon
    la version de Python — un filtre par défaut. Ce module écrit sa propre
    barrière ; ce test empêche qu'on la court-circuite un jour « pour simplifier ».
    """
    source = Path(ri.__file__).read_text(encoding="utf-8")
    attributs = {
        node.attr for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Attribute)
    }
    assert "extractall" not in attributs
    # Contrôle positif : le scan voit bien des attributs, il n'est pas inerte.
    assert {"replace", "symlink"} <= attributs


def test_bornes_d_archive_refusent_une_valeur_absurde():
    """« Pas de borne » n'est pas une valeur admissible."""
    for champ in ("max_archive_bytes", "max_extracted_bytes", "max_entries", "max_ratio"):
        with pytest.raises(ri.InstallError):
            ri.ArchiveLimits(**{champ: 0})


# ══ 3. Refus de décision ══════════════════════════════════════════════════════

def test_refus_variante_conteneur():
    """§6 prévoit un backend conteneur que `server_manager` n'a pas : refus explicite."""
    variante = _variante(source=rr.SOURCE_OFFICIAL_CONTAINER, backend=rr.BACKEND_CUDA12)
    manifeste = rr.ProvenanceManifest(
        version=VERSION, commit=COMMIT, source=rr.SOURCE_OFFICIAL_CONTAINER,
        backend=rr.BACKEND_CUDA12, platform=PLATFORM,
        container_digest="sha256:" + "a" * 64, installed_at=NOW_PLAN,
    )
    requete = ri.RuntimeInstallRequest(
        resolution=_resolution(variant=variante, manifest=manifeste),
        archive_url=URL,
        install_root=Path("/tmp/inexistant"),
    )
    raisons = ri.refusal_reasons(requete)
    assert any("conteneur" in r for r in raisons)


def test_refus_variante_build_local_et_absence_d_empreinte():
    """Un build local n'est pas un artefact vérifiable : les deux raisons sont dites."""
    variante = _variante(source=rr.SOURCE_LOCAL_BUILD)
    manifeste = _manifeste(
        source=rr.SOURCE_LOCAL_BUILD, build_options={"LLAMA_BUILD_SERVER": True},
    )
    requete = ri.RuntimeInstallRequest(
        resolution=_resolution(variant=variante, manifest=manifeste),
        archive_url=URL,
        install_root=Path("/tmp/inexistant"),
    )
    raisons = ri.refusal_reasons(requete)
    assert any("local-build" in r for r in raisons)
    # Distinguée du message ci-dessus, qui cite lui aussi « artifact_sha256 » :
    # sans cette précision, neutraliser la garde d'empreinte ne faisait rougir
    # aucun test (protocole de mutation).
    assert any(r.startswith("manifeste sans artifact_sha256") for r in raisons)


@pytest.mark.parametrize("kwargs,motif", [
    ({"resolved": False}, "a échoué"),
    ({"reuse_existing": True}, "conservation"),
])
def test_refus_resolution_non_installable(kwargs, motif):
    requete = ri.RuntimeInstallRequest(
        resolution=_resolution(sha="a" * 64, **kwargs),
        archive_url=URL,
        install_root=Path("/tmp/inexistant"),
    )
    assert any(motif in r for r in ri.refusal_reasons(requete))


def test_aucune_raison_de_refus_pour_une_decision_saine():
    """Contrôle positif : `refusal_reasons` n'est pas un refus universel."""
    requete = ri.RuntimeInstallRequest(
        resolution=_resolution(sha="a" * 64),
        archive_url=URL,
        install_root=Path("/tmp/inexistant"),
    )
    assert ri.refusal_reasons(requete) == ()


def test_refus_si_l_etape_relue_ne_designe_pas_la_meme_decision(tmp_path):
    """
    L'opérateur a signé un texte : c'est ce texte qui s'exécute.

    Une résolution qui ne parle pas de la même version ni du même backend que
    l'étape du plan installerait autre chose sous un numéro d'étape approuvé.
    """
    installateur, transport = _installateur(tmp_path)
    resultat = _appliquer(
        installateur,
        _contexte(ex.ExecutionMode.APPLY, tmp_path),
        _etape(version="b9999"),
    )
    assert resultat.status == ex.STEP_FAILED
    assert "b9999" in resultat.error
    assert transport.appels == []


def test_refus_si_le_backend_de_l_etape_differe(tmp_path):
    installateur, _ = _installateur(tmp_path)
    resultat = _appliquer(
        installateur, _contexte(ex.ExecutionMode.APPLY, tmp_path), _etape(backend="cuda12"),
    )
    assert resultat.status == ex.STEP_FAILED


def test_racines_autorisees_vides_interdisent_l_installation(tmp_path):
    """Fail-closed : aucune racine déclarée n'autorise rien, pas même en simulation."""
    installateur, _ = _installateur(tmp_path)
    contexte = ex.ExecutionContext(mode=ex.ExecutionMode.DRY_RUN, allowed_roots=())
    with pytest.raises(ex.ExecutionError):
        _appliquer(installateur, contexte)


def test_racine_hors_perimetre_refusee(tmp_path):
    installateur, _ = _installateur(tmp_path)
    contexte = ex.ExecutionContext(
        mode=ex.ExecutionMode.APPLY, allowed_roots=(tmp_path / "ailleurs",),
    )
    with pytest.raises(ex.ExecutionError):
        _appliquer(installateur, contexte)


# ══ 4. Simulation ═════════════════════════════════════════════════════════════

def test_simulation_n_ecrit_rien_et_ne_telecharge_rien(tmp_path):
    installateur, transport = _installateur(tmp_path)
    resultat = _appliquer(installateur, _contexte(ex.ExecutionMode.DRY_RUN, tmp_path))

    assert resultat.status == ex.STEP_WOULD_APPLY
    assert transport.appels == []
    assert not (tmp_path / "runtime").exists()
    # Contrôle positif : la même racine reçoit bien quelque chose en application.
    installateur2, _ = _installateur(tmp_path)
    assert _appliquer(installateur2, _contexte(ex.ExecutionMode.APPLY, tmp_path)).status == ex.STEP_DONE
    assert (tmp_path / "runtime").exists()


def test_simulation_dit_precisement_ce_qui_serait_fait(tmp_path):
    octets = _archive_nominale()
    installateur, _ = _installateur(tmp_path, payload=octets)
    resultat = _appliquer(installateur, _contexte(ex.ExecutionMode.DRY_RUN, tmp_path))

    assert _sha(octets) in resultat.summary
    assert "example.invalid" in resultat.summary
    assert resultat.evidence["expected_build"] == BUILD
    assert resultat.evidence["would_publish_binary"].endswith("current/llama-server")
    assert resultat.evidence["artifact_sha256_expected"] == _sha(octets)


def test_simulation_reconnait_une_installation_deja_satisfaite(tmp_path):
    """En simulation aussi, « déjà satisfait » est une réponse — et elle n'écrit rien."""
    installateur, _ = _installateur(tmp_path)
    assert _appliquer(installateur, _contexte(ex.ExecutionMode.APPLY, tmp_path)).status == ex.STEP_DONE
    avant = sorted(p.name for p in (tmp_path / "runtime").iterdir())

    installateur2, transport2 = _installateur(tmp_path)
    resultat = _appliquer(installateur2, _contexte(ex.ExecutionMode.DRY_RUN, tmp_path))
    assert resultat.status == ex.STEP_ALREADY_SATISFIED
    assert transport2.appels == []
    assert sorted(p.name for p in (tmp_path / "runtime").iterdir()) == avant


# ══ 5. Installation nominale ══════════════════════════════════════════════════

def test_installation_nominale_publie_le_binaire(tmp_path):
    octets = _archive_nominale()
    installateur, transport = _installateur(tmp_path, payload=octets)
    resultat = _appliquer(installateur, _contexte(ex.ExecutionMode.APPLY, tmp_path))

    assert resultat.status == ex.STEP_DONE
    assert transport.appels == [URL]
    binaire = tmp_path / "runtime" / "current" / "llama-server"
    assert binaire.is_file()
    assert stat.S_IMODE(binaire.stat().st_mode) == 0o755
    assert (tmp_path / "runtime" / "current").is_symlink()
    assert resultat.evidence["artifact_sha256_observed"] == _sha(octets)
    assert resultat.evidence["observed_build"] == BUILD


def test_installation_ecrit_un_manifeste_de_provenance_valide(tmp_path):
    installateur, _ = _installateur(tmp_path)
    _appliquer(installateur, _contexte(ex.ExecutionMode.APPLY, tmp_path))

    chemin = tmp_path / "runtime" / "current" / ri.MANIFEST_FILENAME
    document = yaml.safe_load(chemin.read_text(encoding="utf-8"))
    assert rr.validate_manifest_document(document) == ()
    assert document["runtime"]["version"] == VERSION
    assert document["runtime"]["source"] == rr.SOURCE_OFFICIAL_RELEASE
    # `installed_at` est l'heure de l'INSTALLATION, pas celle de la planification.
    assert document["runtime"]["installed_at"] == NOW_EXEC
    assert document["install"]["planned_at"] == NOW_PLAN
    assert document["install"]["license"] == "MIT"
    assert stat.S_IMODE(chemin.stat().st_mode) == 0o644


def test_le_manifeste_propage_le_niveau_de_preuve_de_la_variante(tmp_path):
    """
    `evidence` ne doit pas se perdre entre la résolution et l'hôte.

    Des mois plus tard, plus personne ne se souviendra que la matrice mélangeait
    constats et suppositions ; seul le manifeste posé le dira.
    """
    octets = _archive_nominale()
    resolution = _resolution(
        variant=_variante(sha=_sha(octets), evidence=rr.EVIDENCE_ASSUMPTION),
        manifest=_manifeste(sha=_sha(octets)),
    )
    installateur, _ = _installateur(tmp_path, payload=octets, resolution=resolution)
    resultat = _appliquer(installateur, _contexte(ex.ExecutionMode.APPLY, tmp_path))

    document = yaml.safe_load(
        (tmp_path / "runtime" / "current" / ri.MANIFEST_FILENAME).read_text(encoding="utf-8")
    )
    assert document["install"]["evidence"] == rr.EVIDENCE_ASSUMPTION
    assert document["install"]["evidence_is_assumption"] is True
    assert any(f.code == ri.CODE_EVIDENCE_ASSUMED for f in resultat.findings)


def test_une_variante_sur_constat_ne_declenche_aucun_avertissement_de_preuve(tmp_path):
    """Contrôle positif du test précédent : l'avertissement n'est pas systématique."""
    installateur, _ = _installateur(tmp_path)
    resultat = _appliquer(installateur, _contexte(ex.ExecutionMode.APPLY, tmp_path))
    assert not any(f.code == ri.CODE_EVIDENCE_ASSUMED for f in resultat.findings)
    # Contrôle positif : le résultat porte bien des constats observables par ailleurs.
    assert resultat.evidence["evidence"] == rr.EVIDENCE_SPEC


def test_une_installation_degradee_le_redit_dans_le_journal(tmp_path):
    """Un rapport d'exécution est lu SEUL : perdre « CPU sur hôte GPU » le rendrait rassurant à tort."""
    octets = _archive_nominale()
    resolution = _resolution(sha=_sha(octets), degraded=True, targeted_backend="cuda12")
    installateur, _ = _installateur(tmp_path, payload=octets, resolution=resolution)
    resultat = _appliquer(installateur, _contexte(ex.ExecutionMode.APPLY, tmp_path))

    degrade = [f for f in resultat.findings if f.code == ri.CODE_DEGRADED]
    assert degrade and degrade[0].level == "warn"
    assert "cuda12" in degrade[0].message
    assert resultat.evidence["degraded"] is True


def test_une_installation_non_degradee_ne_porte_pas_le_constat(tmp_path):
    """Contrôle positif : le constat de dégradation n'est pas inconditionnel."""
    installateur, _ = _installateur(tmp_path)
    resultat = _appliquer(installateur, _contexte(ex.ExecutionMode.APPLY, tmp_path))
    assert not any(f.code == ri.CODE_DEGRADED for f in resultat.findings)


def test_licence_absente_signalee_sans_bloquer(tmp_path):
    """§6 « Obligations de licence » : l'installation marche, la conformité non."""
    octets = _archive_nominale(licence=False)
    installateur, _ = _installateur(tmp_path, payload=octets)
    resultat = _appliquer(installateur, _contexte(ex.ExecutionMode.APPLY, tmp_path))

    assert resultat.status == ex.STEP_DONE
    assert any(f.code == ri.CODE_LICENSE_MISSING for f in resultat.findings)
    assert resultat.evidence["license_notice"] is None


def test_licence_presente_ne_declenche_aucun_avertissement(tmp_path):
    """Contrôle positif du test précédent."""
    installateur, _ = _installateur(tmp_path)
    resultat = _appliquer(installateur, _contexte(ex.ExecutionMode.APPLY, tmp_path))
    assert not any(f.code == ri.CODE_LICENSE_MISSING for f in resultat.findings)
    assert resultat.evidence["license_notice"] == "LICENSE"


def test_installation_depuis_une_archive_a_prefixe(tmp_path):
    """Une archive qui range son binaire sous `build/bin/` s'installe aussi."""
    octets = _archive_nominale(prefixe="llama-b6500/")
    installateur, _ = _installateur(tmp_path, payload=octets)
    resultat = _appliquer(installateur, _contexte(ex.ExecutionMode.APPLY, tmp_path))
    assert resultat.status == ex.STEP_DONE
    assert (tmp_path / "runtime" / "current" / "llama-server").is_file()


def test_archive_sans_binaire_attendu_refusee(tmp_path):
    octets = _tar_bytes(lambda tar: _fichier(tar, "autre-chose", b"x"))
    installateur, _ = _installateur(tmp_path, payload=octets)
    resultat = _appliquer(installateur, _contexte(ex.ExecutionMode.APPLY, tmp_path))
    assert resultat.status == ex.STEP_FAILED
    assert "llama-server" in resultat.error
    assert not (tmp_path / "runtime" / "current").exists()


def test_archive_a_plusieurs_binaires_refusee(tmp_path):
    """Deux candidats, c'est un choix à faire : le faire au hasard serait pire que refuser."""
    def remplir(tar):
        _fichier(tar, "bin/llama-server", b"a", mode=0o755)
        _fichier(tar, "autre/llama-server", b"b", mode=0o755)
    installateur, _ = _installateur(tmp_path, payload=_tar_bytes(remplir))
    resultat = _appliquer(installateur, _contexte(ex.ExecutionMode.APPLY, tmp_path))
    assert resultat.status == ex.STEP_FAILED
    assert "2 exécutables" in resultat.error


# ══ 6. Vérification de l'artefact ═════════════════════════════════════════════

def test_empreinte_non_conforme_refuse_avant_toute_extraction(tmp_path):
    """
    L'empreinte attendue vient de l'ÉPINGLAGE, pas de ce qu'on vient de recevoir.

    Ici l'archive est saine mais n'est pas celle que la politique a épinglée : rien
    ne doit être extrait ni publié.
    """
    octets = _archive_nominale()
    resolution = _resolution(sha="f" * 64)
    installateur, _ = _installateur(tmp_path, payload=octets, resolution=resolution)
    resultat = _appliquer(installateur, _contexte(ex.ExecutionMode.APPLY, tmp_path))

    assert resultat.status == ex.STEP_FAILED
    assert "empreinte" in resultat.error
    assert not (tmp_path / "runtime" / "current").exists()
    assert not list((tmp_path / "runtime").glob(f"{ri.RELEASE_PREFIX}*"))
    # Contrôle positif : la même archive avec la bonne empreinte s'installe.
    bon, _ = _installateur(tmp_path, payload=octets)
    assert _appliquer(bon, _contexte(ex.ExecutionMode.APPLY, tmp_path)).status == ex.STEP_DONE


def test_archive_vide_refusee(tmp_path):
    installateur, _ = _installateur(tmp_path, payload=b"", sha=_sha(b""))
    resultat = _appliquer(installateur, _contexte(ex.ExecutionMode.APPLY, tmp_path))
    assert resultat.status == ex.STEP_FAILED
    assert "vide" in resultat.error


def test_archive_hostile_refusee_et_rien_publie(tmp_path):
    octets = _tar_bytes(lambda tar: _fichier(tar, "../evade", b"x"))
    installateur, _ = _installateur(tmp_path, payload=octets)
    resultat = _appliquer(installateur, _contexte(ex.ExecutionMode.APPLY, tmp_path))

    assert resultat.status == ex.STEP_FAILED
    assert any(f.code == ri.CODE_ARCHIVE_UNSAFE for f in resultat.findings)
    assert not (tmp_path / "evade").exists()
    assert not (tmp_path / "runtime" / "current").exists()
    # L'aire d'incubation est nettoyée : un refus ne laisse pas de moignon sous la
    # racine d'installation. Contrôle positif juste en dessous.
    assert list((tmp_path / "runtime").glob(f"{ri.INCOMING_PREFIX}*")) == []


def test_une_installation_reussie_laisse_bien_quelque_chose(tmp_path):
    """Contrôle positif des assertions d'absence de résidu : la racine sait se remplir."""
    installateur, _ = _installateur(tmp_path)
    _appliquer(installateur, _contexte(ex.ExecutionMode.APPLY, tmp_path))
    assert len(list((tmp_path / "runtime").glob(f"{ri.RELEASE_PREFIX}*"))) == 1


def test_echec_de_transport_consigne_sans_publier(tmp_path):
    installateur, _ = _installateur(
        tmp_path, transport=_FauxTransport(erreur=OSError("réseau injoignable")),
    )
    resultat = _appliquer(installateur, _contexte(ex.ExecutionMode.APPLY, tmp_path))
    assert resultat.status == ex.STEP_FAILED
    assert "OSError" in resultat.error
    assert not (tmp_path / "runtime" / "current").exists()
    assert list((tmp_path / "runtime").glob(f"{ri.INCOMING_PREFIX}*")) == []


def test_un_nom_d_entree_hostile_est_expurge_avant_publication(tmp_path):
    """
    Le nom d'une entrée d'archive est du texte non fiable : il finit dans un rapport.

    C'est le seul chemin où un message brut atteint le journal d'étape sans être
    déjà passé par une expurgation en amont — sans ce test, la garde de `_echec`
    pouvait disparaître sans qu'aucun test rougisse.
    """
    octets = _tar_bytes(lambda tar: _fichier(tar, f"../{FAUX_TOKEN}", b"x"))
    installateur, _ = _installateur(tmp_path, payload=octets)
    resultat = _appliquer(installateur, _contexte(ex.ExecutionMode.APPLY, tmp_path))

    assert resultat.status == ex.STEP_FAILED
    assert FAUX_TOKEN not in resultat.error
    assert FAUX_TOKEN not in resultat.summary
    assert FAUX_TOKEN not in json.dumps([f.to_dict() for f in resultat.findings])
    # Contrôle positif : l'expurgation nomme le type détecté, elle ne vide pas tout.
    assert "expurgé" in resultat.error


# ══ 7. Politique de version — fail-closed sans exception ══════════════════════

def test_version_illisible_refusee_meme_sans_plancher(tmp_path):
    """
    SEC-009 : `llama_version` laisserait démarrer, `doctor` non. Ici on refuse, toujours.

    Le plancher vaut 0 dans ce test : c'est précisément le cas où l'autre
    implémentation se contente d'un `log.warning`.
    """
    installateur, _ = _installateur(tmp_path, build=None)
    resultat = _appliquer(installateur, _contexte(ex.ExecutionMode.APPLY, tmp_path))

    assert resultat.status == ex.STEP_FAILED
    assert any(f.code == ri.CODE_VERSION_UNREADABLE for f in resultat.findings)
    assert not (tmp_path / "runtime" / "current").exists()
    assert not list((tmp_path / "runtime").glob(f"{ri.RELEASE_PREFIX}*"))


def test_version_differente_de_l_epinglage_refusee(tmp_path):
    """Empreinte conforme mais build inattendu : anomalie de chaîne d'approvisionnement."""
    installateur, _ = _installateur(tmp_path, build=6499)
    resultat = _appliquer(installateur, _contexte(ex.ExecutionMode.APPLY, tmp_path))
    assert resultat.status == ex.STEP_FAILED
    assert any(f.code == ri.CODE_VERSION_MISMATCH for f in resultat.findings)


def test_signature_du_clone_superficiel_nommee_pour_ce_qu_elle_est(tmp_path):
    """§0.10 : « version: 1 » n'est pas un build ancien, c'est un `git clone --depth 1`."""
    octets = _archive_nominale()
    resolution = _resolution(sha=_sha(octets), manifest=_manifeste(sha=_sha(octets), version="b1"))
    installateur, _ = _installateur(
        tmp_path, payload=octets, resolution=resolution, build=1,
    )
    resultat = _appliquer(
        installateur, _contexte(ex.ExecutionMode.APPLY, tmp_path), _etape(version="b1"),
    )
    assert resultat.status == ex.STEP_FAILED
    constat = [f for f in resultat.findings if f.code == ri.CODE_SHALLOW_CLONE]
    assert constat and "depth 1" in constat[0].message


def test_build_sous_le_plancher_de_securite_refuse(tmp_path):
    """
    Recoupement indépendant de l'invariant de `ReleasePolicy`.

    La politique interdit déjà d'épingler sous le plancher ; ce contrôle-ci vaut si
    quelqu'un contourne cette construction — c'est le seul cas où il sert, et c'est
    justement pour cela qu'il existe.
    """
    octets = _archive_nominale()
    resolution = _resolution(sha=_sha(octets), min_build=9999)
    installateur, _ = _installateur(tmp_path, payload=octets, resolution=resolution)
    resultat = _appliquer(installateur, _contexte(ex.ExecutionMode.APPLY, tmp_path))
    assert resultat.status == ex.STEP_FAILED
    assert any(f.code == ri.CODE_BUILD_TOO_OLD for f in resultat.findings)


def test_un_refus_de_version_ne_laisse_aucune_release_residuelle(tmp_path):
    """
    L'échec survient APRÈS la promotion : c'est le cas où un résidu serait possible.

    Contrôle positif inclus : la même séquence avec une version conforme laisse
    bien une release, donc l'assertion d'absence sait voir quelque chose.
    """
    installateur, _ = _installateur(tmp_path, build=6499)
    _appliquer(installateur, _contexte(ex.ExecutionMode.APPLY, tmp_path))
    residus = list((tmp_path / "runtime").glob(f"{ri.RELEASE_PREFIX}*"))
    incubations = list((tmp_path / "runtime").glob(f"{ri.INCOMING_PREFIX}*"))
    assert residus == []
    assert incubations == []

    bon, _ = _installateur(tmp_path)
    _appliquer(bon, _contexte(ex.ExecutionMode.APPLY, tmp_path))
    assert len(list((tmp_path / "runtime").glob(f"{ri.RELEASE_PREFIX}*"))) == 1


# ══ 8. Idempotence et réversibilité ═══════════════════════════════════════════

def test_seconde_passe_deja_satisfaite_sans_rien_retelecharger(tmp_path):
    installateur, _ = _installateur(tmp_path)
    assert _appliquer(installateur, _contexte(ex.ExecutionMode.APPLY, tmp_path)).status == ex.STEP_DONE

    installateur2, transport2 = _installateur(tmp_path)
    resultat = _appliquer(installateur2, _contexte(ex.ExecutionMode.APPLY, tmp_path))

    assert resultat.status == ex.STEP_ALREADY_SATISFIED
    assert transport2.appels == []
    assert resultat.evidence["reinstalled"] is False
    assert len(list((tmp_path / "runtime").glob(f"{ri.RELEASE_PREFIX}*"))) == 1


def test_un_binaire_altere_depuis_l_installation_est_remplace(tmp_path):
    """Le manifeste ne suffit pas : c'est un fichier texte à côté du binaire."""
    installateur, _ = _installateur(tmp_path)
    _appliquer(installateur, _contexte(ex.ExecutionMode.APPLY, tmp_path))
    binaire = tmp_path / "runtime" / "current" / "llama-server"
    binaire.write_bytes(b"binaire remplace en douce")

    installateur2, transport2 = _installateur(tmp_path)
    resultat = _appliquer(installateur2, _contexte(ex.ExecutionMode.APPLY, tmp_path))

    assert resultat.status == ex.STEP_DONE
    assert transport2.appels == [URL]
    assert any(f.code == ri.CODE_BINARY_ALTERED for f in resultat.findings)


def test_un_manifeste_absent_provoque_une_reinstallation(tmp_path):
    """Un binaire sans provenance n'est pas « déjà satisfait » : rien ne l'atteste."""
    installateur, _ = _installateur(tmp_path)
    _appliquer(installateur, _contexte(ex.ExecutionMode.APPLY, tmp_path))
    (tmp_path / "runtime" / "current" / ri.MANIFEST_FILENAME).unlink()

    installateur2, transport2 = _installateur(tmp_path)
    assert _appliquer(installateur2, _contexte(ex.ExecutionMode.APPLY, tmp_path)).status == ex.STEP_DONE
    assert transport2.appels == [URL]


def test_la_simulation_dit_pourquoi_l_etape_n_est_pas_deja_satisfaite(tmp_path):
    """
    Le motif de non-satisfaction est une information d'exploitation, pas un détail.

    C'est ce que la simulation publie sous `reason`, et le seul endroit où les
    branches de `_deja_satisfait` deviennent observables — sans ce test, plusieurs
    d'entre elles pouvaient être supprimées sans rougeur.
    """
    installateur, _ = _installateur(tmp_path)
    _appliquer(installateur, _contexte(ex.ExecutionMode.APPLY, tmp_path))
    (tmp_path / "runtime" / "current" / ri.MANIFEST_FILENAME).unlink()

    installateur2, _ = _installateur(tmp_path)
    resultat = _appliquer(installateur2, _contexte(ex.ExecutionMode.DRY_RUN, tmp_path))
    assert resultat.status == ex.STEP_WOULD_APPLY
    # Le motif nomme l'ABSENCE de manifeste, pas une lecture qui a échoué : les
    # deux branches existent et ne disent pas la même chose à l'opérateur.
    assert resultat.evidence["reason"] == (
        "le binaire en place n'a pas de manifeste de provenance §6"
    )

    # Contrôle positif : sur une racine vierge, le motif est un AUTRE motif — la
    # clé ne porte donc pas une constante.
    vierge, _ = _installateur(tmp_path / "autre")
    autre = _appliquer(vierge, _contexte(ex.ExecutionMode.DRY_RUN, tmp_path))
    assert "aucun binaire" in autre.evidence["reason"]


def test_installation_conserve_la_release_precedente_et_le_dit(tmp_path):
    """Réversibilité : l'ancienne release reste, et les preuves portent son chemin."""
    octets = _archive_nominale()
    installateur, _ = _installateur(tmp_path, payload=octets)
    premier = _appliquer(installateur, _contexte(ex.ExecutionMode.APPLY, tmp_path))
    assert premier.evidence["previous_release"] is None

    autres = _archive_nominale(contenu=b"#!/bin/false\nversion suivante\n")
    resolution = _resolution(
        sha=_sha(autres),
        manifest=_manifeste(sha=_sha(autres), version="b6600"),
        variant=_variante(sha=_sha(autres)),
    )
    installateur2, _ = _installateur(
        tmp_path, payload=autres, resolution=resolution, build=6600,
    )
    second = _appliquer(
        installateur2, _contexte(ex.ExecutionMode.APPLY, tmp_path), _etape(version="b6600"),
    )

    assert second.status == ex.STEP_DONE
    assert second.evidence["previous_release"] == premier.evidence["release"]
    assert second.evidence["reversible"] is True
    assert Path(premier.evidence["release"]).exists()
    assert (tmp_path / "runtime" / "current" / "llama-server").read_bytes() == b"#!/bin/false\nversion suivante\n"


# ══ 9. Intégration au contrat d'exécution ═════════════════════════════════════

def _plan_charge(tmp_path: Path, etape: sc.PlanStep) -> ex.LoadedPlan:
    plan = sc.BootstrapPlan(
        generated_at=NOW_PLAN,
        mode="local",
        sections=(sc.PlanSection(
            name=sc.SECTION_RUNTIME, version=1, status="ok",
            summary="runtime résolu", data={"resolved": True},
        ),),
        steps=(etape,),
        decisions=(sc.Decision(
            topic="runtime llama-server", choice="official-release/cpu/linux-x86_64",
            rationale="variante épinglée",
        ),),
    )
    return ex.load_plan_document(sc.render_json(plan), origin="<test>")


def test_execution_de_bout_en_bout_par_le_lanceur(tmp_path):
    """Le registre, le lanceur et le rendu acceptent le résultat produit ici."""
    installateur, _ = _installateur(tmp_path)
    registre = ex.ExecutorRegistry()
    ri.register_runtime_installer(registre, installateur)
    assert sc.ACTION_INSTALL_RUNTIME in registre

    rapport = asyncio.run(ex.execute_plan(
        _plan_charge(tmp_path, _etape()),
        registre,
        _contexte(ex.ExecutionMode.APPLY, tmp_path),
    ))
    assert rapport.verdict() == ex.VERDICT_OK
    assert rapport.exit_code() == ex.EXIT_OK
    document = json.loads(ex.render_execution_json(rapport))
    assert ex.validate_execution_document(document) == ()
    assert document["results"][0]["status"] == ex.STEP_DONE


def test_simulation_de_bout_en_bout_sort_en_partiel(tmp_path):
    installateur, _ = _installateur(tmp_path)
    registre = ex.ExecutorRegistry()
    ri.register_runtime_installer(registre, installateur)

    rapport = asyncio.run(ex.execute_plan(
        _plan_charge(tmp_path, _etape()),
        registre,
        _contexte(ex.ExecutionMode.DRY_RUN, tmp_path),
    ))
    assert rapport.verdict() == ex.VERDICT_PARTIAL
    assert rapport.exit_code() == ex.EXIT_PARTIAL
    assert not rapport.changed()
    ex.render_execution_human(rapport)


def test_un_second_enregistrement_est_refuse(tmp_path):
    """Deux installateurs pour la même action doivent se découvrir au démarrage."""
    installateur, _ = _installateur(tmp_path)
    registre = ex.ExecutorRegistry()
    ri.register_runtime_installer(registre, installateur)
    with pytest.raises(ex.ExecutionError):
        ri.register_runtime_installer(registre, installateur)


# ══ 10. Non-divulgation ═══════════════════════════════════════════════════════

def test_aucun_secret_d_url_dans_les_preuves_le_manifeste_ni_le_journal(tmp_path):
    """
    Une URL signée porte son jeton en requête : il ne doit survivre nulle part.

    Contrôle positif : l'origine et le chemin, eux, doivent bien être présents —
    sans quoi ce test passerait aussi si les preuves étaient vides.
    """
    octets = _archive_nominale()
    journal: list[str] = []
    installateur, _ = _installateur(
        tmp_path, payload=octets, url=f"{URL}?token={FAUX_TOKEN}",
    )
    resultat = _appliquer(installateur, _contexte(ex.ExecutionMode.APPLY, tmp_path, journal))

    preuves = json.dumps(resultat.evidence, ensure_ascii=False)
    manifeste = (tmp_path / "runtime" / "current" / ri.MANIFEST_FILENAME).read_text(encoding="utf-8")
    trace = "\n".join(journal)

    assert FAUX_TOKEN not in preuves
    assert FAUX_TOKEN not in manifeste
    assert FAUX_TOKEN not in trace
    assert "example.invalid" in preuves
    assert "example.invalid" in manifeste
    assert "example.invalid" in trace


def test_le_rapport_d_erreur_est_expurge(tmp_path):
    """Un message d'échec est publié tel quel : il passe par la même expurgation."""
    installateur, _ = _installateur(
        tmp_path, transport=_FauxTransport(erreur=RuntimeError(f"échec avec {FAUX_TOKEN}")),
    )
    resultat = _appliquer(installateur, _contexte(ex.ExecutionMode.APPLY, tmp_path))
    assert resultat.status == ex.STEP_FAILED
    assert FAUX_TOKEN not in resultat.error
    assert FAUX_TOKEN not in resultat.summary
    # Contrôle positif : l'expurgation nomme ce qu'elle a vu, elle ne vide pas tout.
    assert "expurgé" in resultat.error


def test_le_rapport_complet_passe_le_controle_de_non_divulgation(tmp_path):
    """`render_execution_json` refuse un rapport qui fuit : il doit accepter celui-ci."""
    installateur, _ = _installateur(tmp_path, url=f"{URL}?token={FAUX_TOKEN}")
    registre = ex.ExecutorRegistry()
    ri.register_runtime_installer(registre, installateur)
    rapport = asyncio.run(ex.execute_plan(
        _plan_charge(tmp_path, _etape()), registre, _contexte(ex.ExecutionMode.APPLY, tmp_path),
    ))
    rendu = ex.render_execution_json(rapport)
    assert FAUX_TOKEN not in rendu
    assert sc.find_secret_leaks(json.loads(rendu)) == ()
