"""Offline tests for the SSRF-guarded fetcher: no real network (fake resolver + httpx.MockTransport)."""
import asyncio, gzip, sys
from pathlib import Path
import httpx
import pytest
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import fetcher
from fetcher import FetchError

PUBLIC = '93.184.216.34'
DNS = {
    'example.test': [PUBLIC],
    'other.test': ['93.184.216.35'],
    'localhost': ['127.0.0.1', '::1'],
    'internal.test': ['10.1.2.3'],
    'mixed.test': [PUBLIC, '192.168.0.5'],
    'metadata.test': ['169.254.169.254'],
    'cgnat.test': ['100.64.0.9'],
    'mapped.test': ['::ffff:127.0.0.1'],
    'ula.test': ['fc00::1'],
    'v6public.test': ['2606:2800:220:1:248:1893:25c8:1946'],
}


async def resolver(host, port):
    if host not in DNS:
        raise OSError('no such host')
    return DNS[host]


def run(coro):
    return asyncio.run(coro)


class Stream(httpx.AsyncByteStream):
    """Unconsumed streaming body (Response(content=...) is pre-read and cannot be streamed)."""
    def __init__(self, chunks):
        self.chunks = chunks
    async def __aiter__(self):
        if hasattr(self.chunks, '__aiter__'):
            async for chunk in self.chunks:
                yield chunk
        else:
            for chunk in self.chunks:
                yield chunk


def Response(status, content=None, headers=None):
    if content is None:
        return httpx.Response(status, headers=headers)
    chunks = content if hasattr(content, '__aiter__') else [content]
    return httpx.Response(status, headers=headers, stream=Stream(chunks))


def fetch(url, handler=None, expect='xml', **kw):
    transport = httpx.MockTransport(handler or (lambda request: Response(200, content=b'<a/>')))
    return run(fetcher.fetch_document(url, expect, resolver=resolver, transport=transport, **kw))


def blocked(url, handler=None):
    calls = []
    def tracking(request):
        calls.append(request)
        return (handler or (lambda r: Response(200, content=b'<a/>')))(request)
    with pytest.raises(FetchError) as info:
        fetch(url, tracking)
    assert info.value.code == 'URL_BLOCKED', info.value.message
    assert not calls, 'no request may be sent for a blocked URL'


@pytest.mark.parametrize('url', [
    'http://localhost/x', 'http://127.0.0.1/x', 'http://10.0.0.1/x', 'http://169.254.169.254/latest/meta-data/',
    'http://[::1]/x', 'http://[fc00::1]/x', 'http://100.64.0.1/x', 'http://0.0.0.0/x', 'http://[::ffff:10.0.0.1]/x',
    'http://224.0.0.1/x', 'http://[fe80::1]/x', 'http://[fec0::1]/x', 'http://[64:ff9b::7f00:1]/x',
    'http://internal.test/x', 'http://mixed.test/x', 'http://metadata.test/x', 'http://cgnat.test/x',
    'http://mapped.test/x', 'http://ula.test/x',
    'file:///etc/passwd', 'ftp://example.test/x', 'gopher://example.test/', 'javascript:alert(1)', '//example.test/x',
    'http://user:pass@example.test/x', 'http://user@example.test/x', 'https://@example.test/x',
    'http://example.test:22/x', 'http://example.test:8080/x', 'https://example.test:8443/x',
    'http://example.test\\@127.0.0.1/', 'http://exa mple.test/', 'http://example.test/\nHost: x', '', 'https://',
])
def test_blocked_urls(url):
    blocked(url)


def test_non_string_blocked():
    with pytest.raises(FetchError) as info:
        run(fetcher.fetch_document(None, 'xml', resolver=resolver))
    assert info.value.code == 'URL_BLOCKED'


def test_url_length_limit():
    blocked('http://example.test/' + 'a' * 2100)


@pytest.mark.parametrize('address,public', [
    ('8.8.8.8', True), ('93.184.216.34', True), ('2606:2800:220:1::1', True),
    ('127.0.0.1', False), ('10.0.0.1', False), ('172.16.0.1', False), ('192.168.1.1', False), ('169.254.1.1', False),
    ('100.64.0.1', False), ('100.127.255.255', False), ('0.0.0.0', False), ('255.255.255.255', False), ('240.0.0.1', False),
    ('224.0.0.1', False), ('192.0.2.1', False), ('198.18.0.1', False),
    ('::1', False), ('::', False), ('fc00::1', False), ('fd12::1', False), ('fe80::1', False), ('fec0::1', False),
    ('ff02::1', False), ('::ffff:127.0.0.1', False), ('::ffff:8.8.8.8', True), ('2002:7f00:1::', False), ('64:ff9b::a00:1', False),
])
def test_ip_is_public(address, public):
    assert fetcher.ip_is_public(address) is public


def test_success_pins_connection_and_sets_headers():
    seen = []
    def handler(request):
        seen.append(request)
        return Response(200, content=b'<?xml version="1.0"?><a/>')
    assert fetch('https://example.test/feed.xml?x=1', handler).endswith('<a/>')
    request = seen[0]
    assert request.url.host == PUBLIC and request.url.scheme == 'https' and request.url.port in (None, 443)
    assert request.url.raw_path == b'/feed.xml?x=1'
    assert request.headers['host'] == 'example.test'
    assert request.extensions['sni_hostname'] == 'example.test'
    assert request.headers['user-agent'] == 'ZendrenApifyActor/0.2 (+https://apify.com/zendren)'
    assert request.headers['accept-encoding'] == 'gzip'
    for name in ('authorization', 'cookie', 'proxy-authorization'):
        assert name not in request.headers


def test_ipv6_pinned_with_brackets():
    seen = []
    def handler(request):
        seen.append(request); return Response(200, content=b'<a/>')
    fetch('http://v6public.test/x', handler)
    assert seen[0].url.host == '2606:2800:220:1:248:1893:25c8:1946' and seen[0].headers['host'] == 'v6public.test'


def test_no_cookies_forwarded_across_hops():
    seen = []
    def handler(request):
        seen.append(request)
        if len(seen) == 1:
            return Response(302, headers={'location': 'https://other.test/y', 'set-cookie': 'sid=1; Domain=.test'})
        return Response(200, content=b'<a/>')
    fetch('https://example.test/x', handler)
    assert 'cookie' not in seen[1].headers


def test_dns_failure_is_fetch_failed():
    with pytest.raises(FetchError) as info:
        fetch('http://nonexistent.test/')
    assert info.value.code == 'FETCH_FAILED'


@pytest.mark.parametrize('target', [
    'http://127.0.0.1/admin', 'http://169.254.169.254/latest/', 'http://internal.test/', 'http://localhost/', 'file:///etc/passwd',
    'http://example.test:22/', 'http://u:p@example.test/', 'ftp://example.test/',
])
def test_redirect_to_blocked_target_rejected(target):
    seen = []
    def handler(request):
        seen.append(request)
        return Response(302, headers={'location': target})
    with pytest.raises(FetchError) as info:
        fetch('http://example.test/start', handler)
    assert info.value.code == 'URL_BLOCKED'
    assert len(seen) == 1, 'redirect target must never be requested'


def test_redirects_followed_relative_and_absolute():
    seen = []
    def handler(request):
        seen.append(request)
        if request.url.path == '/a':
            return Response(301, headers={'location': '/b'})
        if request.url.path == '/b':
            return Response(307, headers={'location': 'https://other.test/c'})
        return Response(200, content=b'<a/>')
    assert fetch('http://example.test/a', handler) == '<a/>'
    assert [r.headers['host'] for r in seen] == ['example.test', 'example.test', 'other.test']


def test_too_many_redirects():
    seen = []
    def handler(request):
        seen.append(request); return Response(302, headers={'location': '/loop'})
    with pytest.raises(FetchError) as info:
        fetch('http://example.test/loop', handler)
    assert info.value.code == 'FETCH_FAILED' and 'redirects' in info.value.message
    assert len(seen) == 4  # first request + 3 redirects


def test_three_redirects_allowed():
    def handler(request):
        hops = int(request.url.path.strip('/') or 0)
        if hops < 3:
            return Response(302, headers={'location': f'/{hops + 1}'})
        return Response(200, content=b'<ok/>')
    assert fetch('http://example.test/0', handler) == '<ok/>'


def test_redirect_without_location():
    with pytest.raises(FetchError) as info:
        fetch('http://example.test/', lambda r: Response(302))
    assert info.value.code == 'FETCH_FAILED'


@pytest.mark.parametrize('status', [204, 400, 403, 404, 429, 500, 503])
def test_http_errors(status):
    with pytest.raises(FetchError) as info:
        fetch('http://example.test/', lambda r: Response(status, content=b'<a/>'))
    assert info.value.code == 'FETCH_FAILED' and str(status) in info.value.message


def test_size_cap_content_length():
    body = b'<' + b'a' * fetcher.MAX_BYTES
    with pytest.raises(FetchError) as info:
        fetch('http://example.test/', lambda r: Response(200, content=body, headers={'content-length': str(len(body))}))
    assert info.value.code == 'TOO_LARGE'


def test_size_cap_enforced_while_streaming():
    chunks = {'sent': 0}
    async def stream():
        for _ in range(100):
            chunks['sent'] += 1
            yield b'<' + b'a' * 65535
    with pytest.raises(FetchError) as info:
        fetch('http://example.test/', lambda r: Response(200, content=stream()))  # no content-length header
    assert info.value.code == 'TOO_LARGE'
    assert chunks['sent'] <= 18, 'stream must be abandoned right after the 1 MiB cap'


def test_exactly_at_cap_ok():
    body = b'<' + b'a' * (fetcher.MAX_BYTES - 1)
    assert len(fetch('http://example.test/', lambda r: Response(200, content=body))) == fetcher.MAX_BYTES


def test_gzip_accepted():
    payload = b'<?xml version="1.0"?><root>' + b'x' * 5000 + b'</root>'
    text = fetch('http://example.test/', lambda r: Response(200, content=gzip.compress(payload), headers={'content-encoding': 'gzip'}))
    assert text == payload.decode()


def test_gzip_bomb_capped_by_decompressed_size():
    bomb = gzip.compress(b'<' + b'0' * 200_000_000, 9)
    assert len(bomb) < fetcher.MAX_BYTES
    with pytest.raises(FetchError) as info:
        fetch('http://example.test/', lambda r: Response(200, content=bomb, headers={'content-encoding': 'gzip'}))
    assert info.value.code == 'TOO_LARGE'


def test_corrupt_and_truncated_gzip():
    good = gzip.compress(b'<a>' + b'x' * 1000 + b'</a>')
    for body in (b'not gzip at all', good[:20]):
        with pytest.raises(FetchError) as info:
            fetch('http://example.test/', lambda r, body=body: Response(200, content=body, headers={'content-encoding': 'gzip'}))
        assert info.value.code == 'FETCH_FAILED'


def test_unsupported_encoding():
    with pytest.raises(FetchError) as info:
        fetch('http://example.test/', lambda r: Response(200, content=b'<a/>', headers={'content-encoding': 'br'}))
    assert info.value.code == 'FETCH_FAILED'


def test_wrong_content():
    for expect, body in (('pdf', b'<html></html>'), ('xml', b'%PDF-1.4 binary'), ('xml', b'\xff\xfe<\x00a\x00/\x00>\x00'), ('xml', b'')):
        with pytest.raises(FetchError) as info:
            fetch('http://example.test/', lambda r, body=body: Response(200, content=body), expect=expect)
        assert info.value.code == 'WRONG_CONTENT'


def test_pdf_returns_bytes_and_xml_strips_bom():
    assert fetch('http://example.test/', lambda r: Response(200, content=b'%PDF-1.4\n...'), expect='pdf') == b'%PDF-1.4\n...'
    assert fetch('http://example.test/', lambda r: Response(200, content=b'\xef\xbb\xbf<a/>')) == '<a/>'


def test_timeouts_map_to_fetch_timeout():
    def handler(request):
        raise httpx.ReadTimeout('slow', request=request)
    with pytest.raises(FetchError) as info:
        fetch('http://example.test/', handler)
    assert info.value.code == 'FETCH_TIMEOUT'


def test_total_timeout(monkeypatch):
    monkeypatch.setattr(fetcher, 'TOTAL_TIMEOUT', 0.2)
    async def slow(request):
        await asyncio.sleep(5)
        return Response(200, content=b'<a/>')
    with pytest.raises(FetchError) as info:
        fetch('http://example.test/', slow)
    assert info.value.code == 'FETCH_TIMEOUT'


def test_connect_error_falls_back_to_next_vetted_address(monkeypatch):
    DNS['multi.test'] = [PUBLIC, '93.184.216.36']
    seen = []
    def handler(request):
        seen.append(request.url.host)
        if request.url.host == PUBLIC:
            raise httpx.ConnectError('refused', request=request)
        return Response(200, content=b'<a/>')
    assert fetch('http://multi.test/', handler) == '<a/>'
    assert seen == [PUBLIC, '93.184.216.36']


def test_connect_error_all_addresses():
    def handler(request):
        raise httpx.ConnectError('refused', request=request)
    with pytest.raises(FetchError) as info:
        fetch('http://example.test/', handler)
    assert info.value.code == 'FETCH_FAILED'


def test_dns_rebinding_second_answer_is_never_used():
    """Resolve once and connect to the vetted address: a changing DNS answer must not matter."""
    answers = iter([[PUBLIC], ['127.0.0.1']])
    calls = []
    async def rebinding(host, port):
        calls.append(host); return next(answers)
    seen = []
    def handler(request):
        seen.append(request.url.host); return Response(200, content=b'<a/>')
    run(fetcher.fetch_document('http://rebind.test/', 'xml', resolver=rebinding, transport=httpx.MockTransport(handler)))
    assert seen == [PUBLIC] and calls == ['rebind.test']


def test_safe_url_redacts_credentials():
    assert fetcher.safe_url('http://user:secret@example.test/x') == 'http://example.test/x'
    assert 'secret' not in fetcher.safe_url('http://user:secret@127.0.0.1:22/')
    assert fetcher.safe_url(None) is None
    try:
        fetch('http://user:secret@example.test/x')
    except FetchError as error:
        assert 'secret' not in fetcher.safe_url(error.url) and 'secret' not in error.message


@pytest.mark.live
def test_live_prefill_url():
    """Opt-in (pytest -m live): hits the real prefill URL through the real resolver/transport."""
    url = {'pdf': 'https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf',
           'sitemap': 'https://www.sitemaps.org/sitemap.xml',
           'feed': 'https://www.python.org/downloads/feed.rss'}[__import__('config').KIND]
    body = run(fetcher.fetch_document(url, 'pdf' if __import__('config').KIND == 'pdf' else 'xml'))
    assert body
