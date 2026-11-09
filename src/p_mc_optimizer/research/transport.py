"""Bounded requests. A document connection dials one validated numeric IP."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import zlib
from dataclasses import dataclass
from time import monotonic
from urllib.parse import urlsplit

import httpx

from .source_policy import require_public_ip


class ResearchError(Exception):
    def __init__(self, code: str, *, retry_after: str | None = None):
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after
        self.redirect_chain: tuple[str, ...] = ()


@dataclass(frozen=True)
class Response:
    status: int
    headers: httpx.Headers
    body: bytes


def _resolve(hostname: str, timeout: float) -> list[str]:
    # getaddrinfo has no portable timeout. The process can be terminated without
    # leaving a DNS thread running in the API process after the deadline.
    command = [
        sys.executable, "-I", "-c",
        "import json,socket,sys; print(json.dumps(sorted({a[4][0] for a in "
        "socket.getaddrinfo(sys.argv[1],443,type=socket.SOCK_STREAM)})))",
        hostname,
    ]
    try:
        result = subprocess.run(command, timeout=timeout, capture_output=True, check=True, env={})
    except subprocess.TimeoutExpired:
        raise ResearchError("SOURCE_UNREACHABLE") from None
    except (OSError, subprocess.CalledProcessError):
        raise ResearchError("SOURCE_UNREACHABLE") from None
    return json.loads(result.stdout)


async def _request(
    method: str,
    url: str,
    timeout: float,
    size: int,
    headers: dict[str, str],
    *,
    body: dict | None = None,
    server_name: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Response:
    extensions = {"sni_hostname": server_name} if server_name else None
    async with asyncio.timeout(timeout):
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=False, trust_env=False, transport=transport,
        ) as client:
            async with client.stream(
                method, url, headers=headers, json=body, extensions=extensions,
            ) as response:
                if response.status_code != 200:
                    return Response(response.status_code, response.headers, b"")
                encoding = response.headers.get("content-encoding", "identity").lower().strip()
                if encoding not in {"gzip", "identity"}:
                    raise ResearchError("SOURCE_UNSUPPORTED_CONTENT")
                unzip = zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding == "gzip" else None
                data = bytearray()
                transferred = 0
                try:
                    async for chunk in response.aiter_raw(chunk_size=16384):
                        transferred += len(chunk)
                        if transferred > size:
                            raise ResearchError("RESEARCH_DATA_LIMIT")
                        remaining = size - len(data)
                        data.extend(unzip.decompress(chunk, remaining + 1) if unzip else chunk)
                        if len(data) > size:
                            raise ResearchError("RESEARCH_DATA_LIMIT")
                    if unzip is not None and (not unzip.eof or unzip.unused_data):
                        raise ResearchError("SOURCE_EXTRACTION_FAILED")
                except zlib.error:
                    raise ResearchError("SOURCE_EXTRACTION_FAILED") from None
                return Response(response.status_code, response.headers, bytes(data))


def request(*args, **kwargs) -> Response:
    return asyncio.run(_request(*args, **kwargs))


class PinnedTransport:
    def __init__(self, *, resolver=_resolve, transport: httpx.AsyncBaseTransport | None = None):
        self.resolver = resolver
        self.transport = transport

    def get(self, url: str, timeout: float, size: int, conditional: dict[str, str]) -> Response:
        parts = urlsplit(url)
        host = parts.hostname
        if host is None:
            raise ResearchError("SOURCE_NOT_ALLOWED")
        started = monotonic()
        addresses = self.resolver(host, timeout)
        try:
            # Reject mixed public/private answers instead of picking around them.
            public = sorted({require_public_ip(address) for address in addresses})
        except ValueError:
            raise ResearchError("SOURCE_NOT_ALLOWED") from None
        if not public:
            raise ResearchError("SOURCE_UNREACHABLE")
        address = public[0]
        literal = f"[{address}]" if ":" in address else address
        pinned = parts._replace(netloc=literal).geturl()
        remaining = timeout - (monotonic() - started)
        if remaining <= 0:
            raise ResearchError("SOURCE_UNREACHABLE")
        # Never forward arbitrary headers. TLS SNI and hostname validation use
        # the authorized DNS name, while the socket connects to the numeric IP.
        headers = {
            name: value for name, value in conditional.items()
            if name in {"If-None-Match", "If-Modified-Since"}
        }
        headers.update({
            "Host": host,
            "User-Agent": "P_MC_Research/2.0",
            "Accept": "text/html, text/plain, application/pdf",
            "Accept-Encoding": "gzip, identity",
        })
        try:
            return request(
                "GET", pinned, remaining, size, headers,
                server_name=host, transport=self.transport,
            )
        except (TimeoutError, httpx.HTTPError):
            raise ResearchError("SOURCE_UNREACHABLE") from None
