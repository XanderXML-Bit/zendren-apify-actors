"""Offline tests: pasted documents and URL inputs (injected fake fetcher) charge exactly one processed-document per success."""
import asyncio, base64, json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import pytest
import main
from config import KIND, EVENT
from fetcher import FetchError

INLINE = json.loads((ROOT / 'tests' / 'fixtures' / 'inline_example.json').read_text())
URL_FIELD = main.URL_FIELD
URL = 'https://example.test/doc'
URL_B = 'https://example.test/doc-b'


class _Res:
    event_charge_limit_reached = False


class _Mgr:
    def __init__(self, limit=False):
        self.limit = limit
    def is_event_charge_limit_reached(self, name):
        return self.limit


class FakeActor:
    def __init__(self, limit=False):
        self.pushed, self.kv, self.status, self.limit = [], {}, None, limit
    def get_charging_manager(self):
        return _Mgr(self.limit)
    async def push_data(self, item, charged_event_name=None):
        self.pushed.append((item, charged_event_name)); return _Res()
    async def set_value(self, k, v):
        self.kv[k] = v
    async def set_status_message(self, m):
        self.status = m


def inline_content():
    doc = INLINE['documents'][0]
    if KIND == 'pdf':
        return base64.b64decode(doc['pdfBase64'])
    return doc['xml']


def previous_content():
    return INLINE['documents'][0]['previousXml']


class FakeFetcher:
    """Serves the inline fixture for every URL except those registered as failures."""
    def __init__(self, failures=None, contents=None):
        self.calls, self.failures, self.contents = [], failures or {}, contents or {}
    async def __call__(self, url, expect):
        self.calls.append((url, expect))
        if url in self.failures:
            raise self.failures[url]
        if url in self.contents:
            return self.contents[url]
        return previous_content() if url.endswith('/previous') else inline_content()


def url_entry(url=URL):
    return url


def run(actor_input, actor=None, fetcher=None, worker=None):
    actor = actor or FakeActor()
    kwargs = {'fetcher': fetcher or FakeFetcher()}
    if worker:
        kwargs['worker'] = worker
    summary = asyncio.run(main.process_input(actor_input, actor=actor, **kwargs))
    return summary, actor


def test_inline_example_charges_event():
    actor = FakeActor()
    s = asyncio.run(main.process_input(INLINE, actor=actor))
    assert s['processed'] == len(INLINE['documents']) and not s['errors'] and s['fetchedUrls'] == 0
    assert all(ev == EVENT == 'processed-document' for _, ev in actor.pushed)
    assert all(item['sourceUrl'] is None and item['fetchedUrls'] == 0 for item, _ in actor.pushed)


def test_example_input_uses_urls_and_charges_one_event_each():
    inp = json.loads((ROOT / 'INPUT.example.json').read_text())
    assert inp['publicDataOnly'] is True and URL_FIELD in inp and 'documents' not in inp
    fetcher = FakeFetcher()
    s, actor = run(inp, fetcher=fetcher)
    assert s['processed'] == len(inp[URL_FIELD]) and not s['errors']
    assert len(actor.pushed) == s['processed'] and all(ev == 'processed-document' for _, ev in actor.pushed)
    assert [c[1] for c in fetcher.calls] == [('pdf' if KIND == 'pdf' else 'xml')] * len(fetcher.calls)
    item = actor.pushed[0][0]
    assert item['sourceUrl'].startswith('https://') and item['documentIndex'] == 0 and item['kind'] == KIND
    assert item['fetchedUrls'] == s['fetchedUrls'] // len(actor.pushed)
    assert actor.kv['OUTPUT'] == s


def test_policy_flag_required_and_uncharged():
    actor = FakeActor()
    s = asyncio.run(main.process_input({'publicDataOnly': False, 'documents': []}, actor=actor))
    assert s['errors'] and not actor.pushed


def test_policy_flag_required_for_urls_and_nothing_fetched():
    fetcher = FakeFetcher()
    s, actor = run({'publicDataOnly': False, URL_FIELD: [url_entry()]}, fetcher=fetcher)
    assert s['errors'][0]['error'] == 'INPUT_POLICY' and not fetcher.calls and not actor.pushed


def test_fetch_failures_reported_with_index_code_url_and_not_charged():
    failures = {
        'https://example.test/blocked': FetchError('URL_BLOCKED', 'URL resolves to a non-public network address'),
        'https://example.test/big': FetchError('TOO_LARGE', 'Response exceeds the 1 MiB limit'),
        'https://example.test/gone': FetchError('FETCH_FAILED', 'HTTP status 404'),
    }
    entries = [url_entry(u) for u in ('https://example.test/blocked', URL, 'https://example.test/big', 'https://example.test/gone')]
    s, actor = run({'publicDataOnly': True, URL_FIELD: entries}, fetcher=FakeFetcher(failures))
    assert s['processed'] == 1 and len(actor.pushed) == 1 and actor.pushed[0][1] == EVENT
    assert actor.pushed[0][0]['documentIndex'] == 1 and actor.pushed[0][0]['sourceUrl'] == URL
    got = {e['documentIndex']: (e['error'], e['url']) for e in s['errors']}
    assert got == {0: ('URL_BLOCKED', 'https://example.test/blocked'), 2: ('TOO_LARGE', 'https://example.test/big'), 3: ('FETCH_FAILED', 'https://example.test/gone')}
    assert s['fetchedUrls'] == 1


def test_unexpected_fetcher_exception_is_fetch_failed_and_uncharged():
    s, actor = run({'publicDataOnly': True, URL_FIELD: [url_entry()]}, fetcher=FakeFetcher(failures={URL: RuntimeError('boom secret')}))
    assert s['errors'][0]['error'] == 'FETCH_FAILED' and 'secret' not in json.dumps(s) and not actor.pushed


def test_error_url_never_echoes_credentials():
    cred = 'https://user:hunter2@example.test/x'
    s, actor = run({'publicDataOnly': True, URL_FIELD: [url_entry(cred)]}, fetcher=FakeFetcher(failures={cred: FetchError('URL_BLOCKED', 'URLs with embedded credentials are not allowed')}))
    assert 'hunter2' not in json.dumps(s) and not actor.pushed


def test_fetched_but_unprocessable_is_not_charged():
    junk = b'not a document' if KIND == 'pdf' else '<notasitemap/>'
    s, actor = run({'publicDataOnly': True, URL_FIELD: [url_entry()]}, fetcher=FakeFetcher(contents={URL: junk}))
    assert s['processed'] == 0 and not actor.pushed and s['fetchedUrls'] == 1
    assert s['errors'][0]['documentIndex'] == 0 and s['errors'][0]['url'] == URL


def test_pasted_documents_and_urls_share_the_five_item_limit_and_indexing():
    fetcher = FakeFetcher()
    s, actor = run({'publicDataOnly': True, 'documents': INLINE['documents'], URL_FIELD: [url_entry(), url_entry(URL_B)]}, fetcher=fetcher)
    assert s['processed'] == 3 and [i['documentIndex'] for i, _ in actor.pushed] == [0, 1, 2]
    assert [i['sourceUrl'] for i, _ in actor.pushed] == [None, URL, URL_B]
    too_many = {'publicDataOnly': True, 'documents': INLINE['documents'] * 3, URL_FIELD: [url_entry()] * 3}
    fetcher = FakeFetcher()
    s, actor = run(too_many, fetcher=fetcher)
    assert s['errors'][0]['error'] == 'INPUT_LIMIT' and not fetcher.calls and not actor.pushed
    s, _ = run({'publicDataOnly': True, URL_FIELD: [url_entry()] * 6})
    assert s['errors'][0]['error'] == 'INPUT_LIMIT'


@pytest.mark.parametrize('bad', [{'publicDataOnly': True}, {'publicDataOnly': True, 'documents': []}, {'publicDataOnly': True, URL_FIELD: []},
                                 {'publicDataOnly': True, URL_FIELD: 'https://example.test/'}, {'publicDataOnly': True, 'documents': 'x'}])
def test_at_least_one_item_required(bad):
    s, actor = run(bad)
    assert s['errors'][0]['error'] == 'INPUT_LIMIT' and not actor.pushed


def test_unknown_and_foreign_top_level_keys_rejected():
    foreign = [k for k in ('pdfUrls', 'sitemapUrls', 'feedUrls', 'chunkSize', 'knownIds') if k != URL_FIELD and k not in main.URL_DEFAULTS]
    for key in ['bogus'] + foreign:
        fetcher = FakeFetcher()
        s, actor = run({'publicDataOnly': True, URL_FIELD: [url_entry()], key: [] if key.endswith('s') else 1}, fetcher=fetcher)
        assert s['errors'][0]['error'] == 'INPUT_INVALID' and not fetcher.calls and not actor.pushed, key


def test_bad_url_entry_types_are_per_item_errors():
    s, actor = run({'publicDataOnly': True, URL_FIELD: [123, None, url_entry()]})
    assert [e['documentIndex'] for e in s['errors']] == [0, 1] and all(e['error'] == 'INPUT_INVALID' for e in s['errors'])
    assert s['processed'] == 1 and actor.pushed[0][0]['documentIndex'] == 2


def test_spending_limit_stops_before_fetching():
    fetcher = FakeFetcher()
    s, actor = run({'publicDataOnly': True, URL_FIELD: [url_entry()]}, actor=FakeActor(limit=True), fetcher=fetcher)
    assert s['stoppedForSpendingLimit'] and not fetcher.calls and not actor.pushed


def test_fetched_content_reaches_worker_not_url():
    seen = []
    async def worker(item):
        seen.append(item); return {'ok': False, 'error': 'INPUT_INVALID', 'message': 'x'}
    run({'publicDataOnly': True, URL_FIELD: [url_entry()]}, worker=worker)
    assert 'pdfBase64' in seen[0] if KIND == 'pdf' else 'xml' in seen[0]
    assert URL not in json.dumps(seen)  # the network-free worker never sees URLs


if KIND == 'pdf':
    def test_pdf_top_level_chunk_settings_apply_to_url_items_only():
        seen = []
        async def worker(item):
            seen.append(item); return {'ok': False, 'error': 'INPUT_INVALID', 'message': 'x'}
        run({'publicDataOnly': True, 'documents': INLINE['documents'], 'pdfUrls': [URL], 'chunkSize': 500, 'chunkOverlap': 50}, worker=worker)
        assert 'chunkSize' not in seen[0] and seen[1]['chunkSize'] == 500 and seen[1]['chunkOverlap'] == 50

    def test_pdf_url_chunking_and_output_shape():
        s, actor = run({'publicDataOnly': True, 'pdfUrls': [URL], 'chunkSize': 250, 'chunkOverlap': 0})
        item = actor.pushed[0][0]
        assert item['pageCount'] == 1 and item['chunks'] and item['sourceUrl'] == URL and item['fetchedUrls'] == 1 and s['fetchedUrls'] == 1

    @pytest.mark.parametrize('bad', [{'chunkSize': '500'}, {'chunkSize': True}, {'chunkOverlap': 1.5}])
    def test_pdf_bad_chunk_defaults_rejected(bad):
        s, actor = run({'publicDataOnly': True, 'pdfUrls': [URL], **bad})
        assert s['errors'][0]['error'] == 'INPUT_INVALID' and not actor.pushed

    def test_pdf_chunk_size_out_of_bounds_is_uncharged_item_error():
        s, actor = run({'publicDataOnly': True, 'pdfUrls': [URL], 'chunkSize': 10})
        assert s['errors'][0]['error'] == 'INPUT_INVALID' and s['errors'][0]['documentIndex'] == 0 and not actor.pushed

if KIND == 'sitemap':
    def test_sitemap_objects_strings_and_previous_url():
        prev = 'https://example.test/previous'
        fetcher = FakeFetcher()
        s, actor = run({'publicDataOnly': True, 'sitemapUrls': [{'url': URL, 'previousUrl': prev}, URL_B, {'url': URL}]}, fetcher=fetcher)
        assert s['processed'] == 3 and s['fetchedUrls'] == 4
        first, second, third = (i for i, _ in actor.pushed)
        assert first['comparedToPrevious'] and first['sourceUrl'] == URL and first['previousSourceUrl'] == prev and first['fetchedUrls'] == 2
        assert not second['comparedToPrevious'] and second['sourceUrl'] == URL_B and second['previousSourceUrl'] is None and second['fetchedUrls'] == 1
        assert not third['comparedToPrevious']
        assert all(ev == EVENT for _, ev in actor.pushed)

    def test_sitemap_previous_url_failure_fails_item_uncharged():
        prev = 'https://example.test/previous'
        s, actor = run({'publicDataOnly': True, 'sitemapUrls': [{'url': URL, 'previousUrl': prev}]}, fetcher=FakeFetcher({prev: FetchError('FETCH_FAILED', 'HTTP status 404')}))
        assert not actor.pushed and s['errors'][0]['url'] == prev and s['errors'][0]['error'] == 'FETCH_FAILED' and s['fetchedUrls'] == 1

    @pytest.mark.parametrize('bad', [{'url': URL, 'xml': '<a/>'}, {'previousUrl': URL}, {'url': 5}, {'url': URL, 'previousUrl': 5}])
    def test_sitemap_bad_objects_rejected(bad):
        s, actor = run({'publicDataOnly': True, 'sitemapUrls': [bad]})
        assert s['errors'][0]['error'] == 'INPUT_INVALID' and not actor.pushed

if KIND == 'feed':
    def test_feed_top_level_known_ids_apply_to_url_items_only():
        first = json.loads(json.dumps(INLINE['documents'][0]))
        known = first['knownIds']
        s, actor = run({'publicDataOnly': True, 'documents': [{'xml': first['xml']}], 'feedUrls': [URL], 'knownIds': known})
        pasted, fetched = (i for i, _ in actor.pushed)
        assert pasted['newCount'] == pasted['entryCount'] and fetched['newCount'] == fetched['entryCount'] - len(known)
        assert fetched['sourceUrl'] == URL and fetched['fetchedUrls'] == 1

    @pytest.mark.parametrize('bad', [{'knownIds': 'x'}, {'knownIds': [1]}, {'knownIds': ['a'] * 5001}, {'knownIds': ['a' * 5000]}])
    def test_feed_bad_known_ids_rejected(bad):
        fetcher = FakeFetcher()
        s, actor = run({'publicDataOnly': True, 'feedUrls': [URL], **bad}, fetcher=fetcher)
        assert s['errors'][0]['error'] == 'INPUT_INVALID' and not fetcher.calls and not actor.pushed
