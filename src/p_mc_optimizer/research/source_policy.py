"""Fail-closed URL and address policy for research documents.

A successful match is permission to resolve a host. The transport must validate
every DNS answer with require_public_ip and connect to the validated address
with TLS verification against the original hostname.
"""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import idna

from p_mc_optimizer.domain.models import DomainError

if TYPE_CHECKING:
    from p_mc_optimizer.research.contracts import SourceSnapshot

_BAD_ESCAPE = re.compile(r"%(?![0-9a-fA-F]{2})")
_ESCAPES = re.compile(r"%([0-9a-fA-F]{2})")
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_LOCAL_SUFFIXES = ("localhost", "localdomain", "local", "internal", "home.arpa")
_SPECIAL_V6 = tuple(
    ipaddress.IPv6Network(block) for block in ("2001::/23", "2002::/16", "3fff::/20")
)
_GLOBAL_V6 = ipaddress.IPv6Network("2000::/3")


class SourcePolicyError(DomainError):
    def __init__(self, message: str):
        super().__init__("SOURCE_NOT_ALLOWED", message)


def _forbidden_url_characters(value: str) -> bool:
    return any(char.isspace() or unicodedata.category(char).startswith("C") for char in value)


def normalize_hostname(host: str) -> str:
    """Canonical public DNS name; numeric aliases and local names are excluded."""
    if not host or _forbidden_url_characters(host):
        raise SourcePolicyError("A DNS hostname is required.")
    try:
        hostname = idna.encode(host, uts46=True, std3_rules=True).decode("ascii").lower()
    except idna.IDNAError:
        raise SourcePolicyError("Invalid source hostname.") from None
    hostname = hostname.removesuffix(".")
    labels = hostname.split(".")
    if (
        len(labels) < 2
        or not any(char.isalpha() for char in labels[-1])
        or re.fullmatch(r"0x[0-9a-f]+", labels[-1])
        or any(hostname == suffix or hostname.endswith("." + suffix) for suffix in _LOCAL_SUFFIXES)
    ):
        raise SourcePolicyError("The source must be a public DNS hostname.")
    return hostname


def canonical_path(path: str) -> str:
    if (
        not path.startswith("/")
        or path.startswith("//")
        or any(char in path for char in "\\?#;")
        or _forbidden_url_characters(path)
        or _BAD_ESCAPE.search(path)
    ):
        raise SourcePolicyError("Invalid or ambiguous source path.")

    def decode_escape(match: re.Match[str]) -> str:
        value = int(match.group(1), 16)
        character = chr(value)
        if value < 32 or value == 127 or character in "/\\%?#;":
            raise SourcePolicyError("Encoded path delimiters and controls are not permitted.")
        if character in _UNRESERVED:
            return character
        return "%" + match.group(1).upper()

    normalized = _ESCAPES.sub(decode_escape, path)
    try:
        decoded = unquote(normalized, errors="strict")
    except UnicodeDecodeError:
        raise SourcePolicyError("The source path has invalid UTF-8 escapes.") from None
    normalized_unicode = unicodedata.normalize("NFKC", decoded)
    if (
        any(unicodedata.category(char).startswith("C") for char in decoded)
        or any(decoded.count(char) != normalized_unicode.count(char) for char in "/\\?#;%")
        or any(part in {".", ".."} for part in normalized_unicode.split("/"))
    ):
        raise SourcePolicyError("Ambiguous encoded or relative path segments are not permitted.")
    return quote(normalized, safe="/!$&'()*+,:=@%~")


def canonical_url(url: str) -> tuple[str, str, str]:
    if len(url) > 4096 or _forbidden_url_characters(url) or "\\" in url:
        raise SourcePolicyError("Invalid source URL.")
    try:
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname or "@" in parts.netloc:
            raise SourcePolicyError("Source URL must use HTTPS without user information.")
        if ":" in parts.netloc and not re.fullmatch(r"[^:]+:443", parts.netloc):
            raise SourcePolicyError("Source URLs must use HTTPS port 443.")
    except ValueError as error:
        if isinstance(error, SourcePolicyError):
            raise
        raise SourcePolicyError("Invalid source URL.") from None
    hostname = normalize_hostname(parts.hostname)
    path = canonical_path(parts.path or "/")
    if _BAD_ESCAPE.search(parts.query) or any(
        int(match.group(1), 16) < 32 or int(match.group(1), 16) == 127
        for match in _ESCAPES.finditer(parts.query)
    ):
        raise SourcePolicyError("Invalid URL query string.")
    query = quote(parts.query, safe="/?!$&'()*+,:;=@%~")
    return urlunsplit(("https", hostname, path, query, "")), hostname, path


def require_public_ip(address: str) -> str:
    """Return a dialable public address, rejecting scoped and transition addresses."""
    try:
        if "%" in address:
            raise ValueError("Scoped address")
        ip = ipaddress.ip_address(address)
    except ValueError:
        raise SourcePolicyError("DNS returned an invalid address.") from None
    if (
        not ip.is_global
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_unspecified
        or address == "168.63.129.16"  # Azure platform metadata/service virtual IP.
        or isinstance(ip, ipaddress.IPv6Address)
        and (ip not in _GLOBAL_V6 or any(ip in network for network in _SPECIAL_V6))
    ):
        raise SourcePolicyError("DNS returned a non-public or special-use address.")
    return str(ip)


@dataclass(frozen=True, slots=True)
class AllowedURL:
    url: str
    hostname: str
    source_id: str


@dataclass(frozen=True, slots=True)
class SourcePolicy:
    snapshot: SourceSnapshot

    @property
    def rules(self):
        return self.snapshot.sources

    @property
    def hash(self) -> str:
        return self.snapshot.hash

    def match(self, url: str, source_ids: tuple[str, ...] | None = None) -> AllowedURL:
        canonical, hostname, path = canonical_url(url)
        for rule in self.rules:
            if not rule.enabled or (source_ids is not None and rule.source_id not in source_ids):
                continue
            if rule.matches_hostname(hostname) and any(
                prefix == "/" or path == prefix or path.startswith(prefix + "/")
                for prefix in rule.path_prefixes
            ):
                return AllowedURL(canonical, hostname, rule.source_id)
        raise SourcePolicyError("The host or path is outside the authorized sources.")

    def match_robots(
        self, url: str, hostname: str, source_ids: tuple[str, ...] | None = None
    ) -> AllowedURL:
        canonical, actual_host, path = canonical_url(url)
        if actual_host != hostname or path != "/robots.txt" or urlsplit(canonical).query:
            raise SourcePolicyError("The robots exception applies only to /robots.txt on the same host.")
        for rule in self.rules:
            if (
                rule.enabled
                and (source_ids is None or rule.source_id in source_ids)
                and rule.matches_hostname(hostname)
            ):
                return AllowedURL(canonical, hostname, rule.source_id)
        raise SourcePolicyError("The robots host is outside the authorized sources.")

    def robots_url(self, allowed: AllowedURL) -> AllowedURL:
        return self.match_robots(
            f"https://{allowed.hostname}/robots.txt", allowed.hostname, (allowed.source_id,)
        )

    def authorize(
        self,
        url: str,
        source_ids: tuple[str, ...] | None = None,
        *,
        robots: bool = False,
        robots_host: str | None = None,
    ) -> tuple[str, str]:
        if robots:
            if robots_host is None:
                raise SourcePolicyError("A previously authorized document host is required.")
            matched = self.match_robots(url, robots_host, source_ids)
        else:
            matched = self.match(url, source_ids)
        return matched.url, matched.source_id

    require_public_ip = staticmethod(require_public_ip)
