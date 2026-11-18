"""SSRF-guarded fetching of public http(s) documents.

Runs in the main process only; the parser worker stays network-free. Every hop (including redirects) is
validated, DNS is resolved once, every resolved address must be globally routable, and the connection is
pinned to a vetted address so a second DNS answer cannot redirect it (anti DNS rebinding).
"""
import asyncio
import ipaddress
import socket
import zlib
from urllib.parse import urljoin, urlsplit

import httpx

USER_AGENT = 'ZendrenApifyActor/0.2 (+https://apify.com/zendren)'
MAX_BYTES = 1_048_576
MAX_REDIRECTS = 3
TOTAL_TIMEOUT = 20
CONNECT_TIMEOUT = 10
ALLOWED_PORTS = {'http': 80, 'https': 443}
MAX_URL_LENGTH = 2048
MAX_PINNED_ADDRESSES = 3
REDIRECT_STATUSES = (301, 302, 303, 307, 308)

# Ranges that may embed or tunnel to an arbitrary IPv4 address; refuse them rather than unwrap them.
BLOCKED_V6 = tuple(ipaddress.ip_network(net) for net in ('64:ff9b::/96', '64:ff9b:1::/48', '2002::/16', '2001::/32'))


class FetchError(Exception):
    def __init__(self, code, message, url=None):
        super().__init__(message)
        self.code, self.message, self.url = code, message, url


def safe_url(url):
    """URL for error reporting: truncated, never echoing credentials."""
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url)
        if parts.username is not None or parts.password is not None:
            netloc = parts.netloc.rsplit('@', 1)[-1]
            url = parts._replace(netloc=netloc).geturl()
    except ValueError:
        pass
    return url[:MAX_URL_LENGTH]


def ip_is_public(address):
    ip = ipaddress.ip_address(address)
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            return ip_is_public(str(ip.ipv4_mapped))
        if any(ip in net for net in BLOCKED_V6):
            return False
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified or getattr(ip, 'is_site_local', False):
        return False
    return ip.is_global


def parse_url(url):
    """Validate scheme/credentials/port syntactically. Returns (scheme, ascii host, port, request target)."""
    blocked = lambda message: FetchError('URL_BLOCKED', message, url)
    if not isinstance(url, str) or not url or len(url) > MAX_URL_LENGTH:
        raise blocked('URL must be a string of at most 2,048 characters')
    if any(ord(ch) <= 32 or ord(ch) == 127 or ch == '\\' for ch in url):
        raise blocked('URL contains whitespace, control or backslash characters')
    try:
        parts = urlsplit(url)
        port = parts.port
        host = parts.hostname
    except ValueError:
        raise blocked('URL is malformed') from None
    if parts.scheme not in ALLOWED_PORTS:
        raise blocked('Only http and https URLs are allowed')
    if '@' in parts.netloc or parts.username is not None or parts.password is not None:
        raise blocked('URLs with embedded credentials are not allowed')
    if not host or '%' in host:
        raise blocked('URL has no valid host')
    port = port if port is not None else ALLOWED_PORTS[parts.scheme]
    if port not in (80, 443):
        raise blocked('Only ports 80 and 443 are allowed')
    try:
        host = host.encode('idna').decode('ascii')
    except UnicodeError:
        raise blocked('URL host is not a valid hostname') from None
    target = (parts.path or '/') + ('?' + parts.query if parts.query else '')
    return parts.scheme, host, port, target


async def system_resolver(host, port):
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(info[4][0] for info in infos))


async def resolve_public(url, host, port, resolver):
    """Resolve once; reject if ANY address is non-public. Returns the vetted addresses."""
    try:
        ipaddress.ip_address(host)
        addresses = [host]
    except ValueError:
        try:
            addresses = await resolver(host, port)
        except (OSError, UnicodeError):
            raise FetchError('FETCH_FAILED', 'DNS lookup failed', url) from None
    if not addresses:
        raise FetchError('FETCH_FAILED', 'DNS lookup returned no addresses', url)
    for address in addresses:
        try:
            public = ip_is_public(address)
        except ValueError:
            public = False
        if not public:
            raise FetchError('URL_BLOCKED', 'URL resolves to a non-public network address', url)
    return addresses[:MAX_PINNED_ADDRESSES]


async def read_body(response, max_bytes, url):
    """Stream the body, capping both wire bytes and decompressed bytes; gzip only."""
    length = response.headers.get('content-length', '')
    if length.isdigit() and int(length) > max_bytes:
        raise FetchError('TOO_LARGE', 'Response exceeds the 1 MiB limit', url)
    encoding = response.headers.get('content-encoding', 'identity').strip().lower()
    if encoding not in ('', 'identity', 'gzip', 'x-gzip'):
        raise FetchError('FETCH_FAILED', 'Unsupported content encoding', url)
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding in ('gzip', 'x-gzip') else None
    body, wire = bytearray(), 0
    try:
        async for chunk in response.aiter_raw():
            wire += len(chunk)
            if wire > max_bytes:
                raise FetchError('TOO_LARGE', 'Response exceeds the 1 MiB limit', url)
            if inflater is None:
                body += chunk
            else:
                data = chunk
                while data and not inflater.eof:
                    body += inflater.decompress(data, max_bytes + 1 - len(body))
                    if len(body) > max_bytes:
                        raise FetchError('TOO_LARGE', 'Decompressed response exceeds the 1 MiB limit', url)
                    data = inflater.unconsumed_tail
            if len(body) > max_bytes:
                raise FetchError('TOO_LARGE', 'Response exceeds the 1 MiB limit', url)
    except zlib.error:
        raise FetchError('FETCH_FAILED', 'Invalid gzip response', url) from None
    if inflater is not None and not inflater.eof:
        raise FetchError('FETCH_FAILED', 'Truncated gzip response', url)
    return bytes(body)


async def request_once(url, max_bytes, resolver, transport):
    """One hop. Returns (body, None) or (None, redirect location)."""
    scheme, host, port, target = parse_url(url)
    addresses = await resolve_public(url, host, port, resolver)
    default_port = ALLOWED_PORTS[scheme]
    host_header = (f'[{host}]' if ':' in host else host) + ('' if port == default_port else f':{port}')
    headers = {'Host': host_header, 'User-Agent': USER_AGENT, 'Accept': '*/*', 'Accept-Encoding': 'gzip', 'Connection': 'close'}
    last_error = None
    for address in addresses:
        pinned = f'{scheme}://{"[" + address + "]" if ":" in address else address}:{port}{target}'
        # A fresh client per hop/address: no cookie jar, no shared connection pool keyed on a pinned IP.
        async with httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False,
                                     timeout=httpx.Timeout(TOTAL_TIMEOUT, connect=CONNECT_TIMEOUT)) as client:
            request = client.build_request('GET', pinned, headers=headers, extensions={'sni_hostname': host})
            try:
                response = await client.send(request, stream=True)
            except (httpx.ConnectError, httpx.ConnectTimeout) as error:
                last_error = error
                continue
            except httpx.TimeoutException:
                raise FetchError('FETCH_TIMEOUT', 'Request timed out', url) from None
            except httpx.HTTPError:
                raise FetchError('FETCH_FAILED', 'Request failed', url) from None
            try:
                if response.status_code in REDIRECT_STATUSES:
                    location = response.headers.get('location')
                    if not location:
                        raise FetchError('FETCH_FAILED', 'Redirect without a Location header', url)
                    return None, location
                if response.status_code != 200:
                    raise FetchError('FETCH_FAILED', f'HTTP status {response.status_code}', url)
                try:
                    return await read_body(response, max_bytes, url), None
                except httpx.TimeoutException:
                    raise FetchError('FETCH_TIMEOUT', 'Request timed out', url) from None
                except httpx.HTTPError:
                    raise FetchError('FETCH_FAILED', 'Request failed', url) from None
            finally:
                await response.aclose()
    if isinstance(last_error, httpx.ConnectTimeout):
        raise FetchError('FETCH_TIMEOUT', 'Connection timed out', url)
    raise FetchError('FETCH_FAILED', 'Could not connect', url)


async def follow(url, max_bytes, resolver, transport):
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        body, location = await request_once(current, max_bytes, resolver, transport)
        if location is None:
            return body
        if len(location) > MAX_URL_LENGTH:
            raise FetchError('URL_BLOCKED', 'Redirect target is too long', url)
        current = urljoin(current, location)
    raise FetchError('FETCH_FAILED', 'Too many redirects (limit 3)', url)


async def fetch_bytes(url, max_bytes=MAX_BYTES, resolver=None, transport=None):
    try:
        return await asyncio.wait_for(follow(url, max_bytes, resolver or system_resolver, transport), TOTAL_TIMEOUT)
    except asyncio.TimeoutError:
        raise FetchError('FETCH_TIMEOUT', 'Fetch exceeded the 20-second limit', url) from None
    except FetchError as error:
        error.url = error.url or url
        raise


async def fetch_document(url, expect, resolver=None, transport=None):
    """Fetch a public document. expect='pdf' returns bytes, expect='xml' returns text."""
    body = await fetch_bytes(url, MAX_BYTES, resolver, transport)
    if expect == 'pdf':
        if not body.startswith(b'%PDF-'):
            raise FetchError('WRONG_CONTENT', 'Response is not a PDF', url)
        return body
    try:
        text = body.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise FetchError('WRONG_CONTENT', 'Response is not UTF-8 XML', url) from None
    if not text.lstrip().startswith('<'):
        raise FetchError('WRONG_CONTENT', 'Response is not XML', url)
    return text
