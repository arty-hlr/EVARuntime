"""Transport HTTPS public épinglé, commun aux artefacts runtime et modèles."""
from __future__ import annotations

import http.client
import ipaddress
import socket
from collections.abc import Callable, Sequence
from typing import Any, Protocol
from urllib.parse import urlsplit


class PublicHttpsError(ValueError):
    """L'URL, sa résolution ou sa destination n'est pas publiquement routable."""


AddressInfo = tuple[int, int, int, str, tuple[Any, ...]]
Resolver = Callable[[str, int], Sequence[AddressInfo]]


def require_public_ip(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    label: str,
) -> None:
    """Refuse toute adresse qui n'est pas routable sur l'Internet public."""
    if (
        not address.is_global
        or address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
    ):
        raise PublicHttpsError(
            f"destination d'artefact refusée ({label}) : adresse privée, locale, "
            "réservée ou non routable"
        )


def validate_url(url: Any) -> str:
    """Valide une URL HTTPS sans identifiants ni destination littérale locale."""
    if not isinstance(url, str) or not url.strip():
        raise PublicHttpsError("URL d'artefact absente : rien à télécharger")
    try:
        parts = urlsplit(url.strip())
        parts.port
    except ValueError as exc:
        raise PublicHttpsError(
            "URL d'artefact refusée : autorité ou port invalide"
        ) from exc
    if parts.scheme != "https":
        raise PublicHttpsError(
            f"URL d'artefact refusée ({parts.scheme or 'sans schéma'}://…) : "
            "seul HTTPS est admis"
        )
    if not parts.hostname:
        raise PublicHttpsError("URL d'artefact refusée : autorité HTTPS absente")
    if parts.username is not None or parts.password is not None:
        raise PublicHttpsError(
            "URL d'artefact refusée : elle porte des identifiants. Un secret dans "
            "une URL se retrouve dans les journaux et les rapports d'exécution."
        )
    hostname = parts.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise PublicHttpsError(
            "URL d'artefact refusée : une destination locale n'est pas admise"
        )
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        require_public_ip(address, hostname)
    return url.strip()


def system_resolve(host: str, port: int) -> Sequence[AddressInfo]:
    """Résolution système isolée derrière une interface injectable."""
    return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)


def resolve_public_addresses(
    host: str,
    port: int,
    resolver: Resolver = system_resolve,
) -> tuple[AddressInfo, ...]:
    """Résout une fois et refuse toute réponse DNS vide, invalide ou mixte."""
    try:
        candidates = tuple(resolver(host, port))
    except (OSError, UnicodeError) as exc:
        raise PublicHttpsError(
            f"résolution DNS impossible pour la source d'artefact {host!r}"
        ) from exc
    if not candidates:
        raise PublicHttpsError(
            f"résolution DNS vide pour la source d'artefact {host!r}"
        )

    validated: list[AddressInfo] = []
    seen: set[tuple[int, tuple[Any, ...]]] = set()
    for candidate in candidates:
        try:
            family, socktype, proto, canonname, sockaddr = candidate
            address = ipaddress.ip_address(sockaddr[0])
        except (IndexError, TypeError, ValueError) as exc:
            raise PublicHttpsError(
                f"réponse DNS invalide pour la source d'artefact {host!r}"
            ) from exc
        require_public_ip(address, f"{host} → {address}")
        key = (family, sockaddr)
        if key not in seen:
            validated.append((family, socktype, proto, canonname, sockaddr))
            seen.add(key)
    return tuple(validated)


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connexion TCP épinglée, avec certificat et SNI sur le hostname initial."""

    def __init__(
        self,
        host: str,
        port: int,
        addresses: Sequence[AddressInfo],
        timeout: float,
    ) -> None:
        super().__init__(host, port=port, timeout=timeout)
        self._addresses = tuple(addresses)

    def connect(self) -> None:
        sock: socket.socket | None = None
        last_error: OSError | None = None
        for family, socktype, proto, _canonname, sockaddr in self._addresses:
            candidate: socket.socket | None = None
            try:
                candidate = socket.socket(family, socktype, proto)
                candidate.settimeout(self.timeout)
                if self.source_address:
                    candidate.bind(self.source_address)
                candidate.connect(sockaddr)
            except OSError as exc:
                last_error = exc
                if candidate is not None:
                    candidate.close()
                continue
            sock = candidate
            break
        if sock is None:
            raise last_error or OSError("aucune adresse publique joignable")

        try:
            self.sock = sock
            if self._tunnel_host:
                self._tunnel()
            self.sock = self._context.wrap_socket(
                self.sock,
                server_hostname=self.host,
            )
        except BaseException:
            sock.close()
            self.sock = None
            raise


class HTTPSConnectionLike(Protocol):
    def request(self, method: str, url: str, *, headers: dict[str, str]) -> None:
        ...

    def getresponse(self) -> Any:
        ...

    def close(self) -> None:
        ...


ConnectionFactory = Callable[
    [str, int, Sequence[AddressInfo], float],
    HTTPSConnectionLike,
]


def pinned_connection_factory(
    host: str,
    port: int,
    addresses: Sequence[AddressInfo],
    timeout: float,
) -> HTTPSConnectionLike:
    return PinnedHTTPSConnection(host, port, addresses, timeout)


__all__ = [
    "AddressInfo",
    "ConnectionFactory",
    "HTTPSConnectionLike",
    "PinnedHTTPSConnection",
    "PublicHttpsError",
    "Resolver",
    "pinned_connection_factory",
    "require_public_ip",
    "resolve_public_addresses",
    "system_resolve",
    "validate_url",
]
