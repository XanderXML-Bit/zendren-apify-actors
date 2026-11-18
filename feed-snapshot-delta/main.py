import asyncio
import base64
import json
import os
import sys
from pathlib import Path
from apify import Actor
from config import KIND, EVENT
from fetcher import FetchError, fetch_document, safe_url
from input_guard import decode_bounded

URL_FIELD = {'pdf': 'pdfUrls', 'sitemap': 'sitemapUrls', 'feed': 'feedUrls'}[KIND]
# Top-level defaults that apply to every URL item (never to pasted documents).
URL_DEFAULTS = {'pdf': ('chunkSize', 'chunkOverlap'), 'sitemap': (), 'feed': ('knownIds',)}[KIND]

async def load_input():
    store_id = os.environ.get('ACTOR_DEFAULT_KEY_VALUE_STORE_ID')
    if Actor.is_at_home():
        if not store_id:
            raise ValueError('Default input storage is unavailable')
        chunks = bytearray()
        async with Actor.apify_client.key_value_store(store_id).stream_record('INPUT') as record:
            if record is None:
                return None
            async for chunk in record['value'].aiter_bytes():
                if len(chunks) + len(chunk) > 8_000_000:
                    raise ValueError('Input exceeds 8 MB')
                chunks.extend(chunk)
        return decode_bounded(bytes(chunks))
    path = Path('INPUT.example.json')
    with path.open('rb') as file:
        return decode_bounded(file.read(8_000_001))

async def session_guard(actor=Actor):
    # Fail closed on same-run restart/migration. SDK retry idempotency does not dedupe application replays.
    if await actor.get_value('SESSION_STARTED') is not None:
        await actor.set_status_message('Same-run restart is not supported; existing results retained and no work recharged')
        return False
    await actor.set_value('SESSION_STARTED', {'started': True})
    return True


def shape_valid(item, allowed):
    if not isinstance(item, dict) or set(item) - allowed:
        return False
    for key, value in item.items():
        if key in ('xml', 'previousXml', 'pdfBase64'):
            if not isinstance(value, str) or len(value) > 1_398_104:
                return False
        elif key in ('chunkSize', 'chunkOverlap'):
            if type(value) is not int:
                return False
        elif key == 'knownIds':
            if not isinstance(value, list) or len(value) > 5000 or any(not isinstance(entry, str) or len(entry) > 4096 for entry in value):
                return False
    return True

async def run_worker(item):
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(Path(__file__).with_name('worker.py')),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        env={'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'LANG': 'C.UTF-8'},
    )
    try:
        out, _ = await asyncio.wait_for(process.communicate(json.dumps(item).encode()), timeout=20)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return {'ok': False, 'error': 'PROCESSING_LIMIT', 'message': 'Document exceeded the 20-second processing limit'}
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    if process.returncode != 0 or not out:
        return {'ok': False, 'error': 'PROCESSING_LIMIT', 'message': 'Document exceeded resource limits or could not be processed'}
    try:
        return json.loads(out)
    except (ValueError, UnicodeDecodeError):
        return {'ok': False, 'error': 'PROCESSING_ERROR', 'message': 'Document could not be processed'}

async def build_url_item(spec, defaults, fetcher, summary):
    """Fetch the URL(s) for one item in the main process. Returns (worker item, sourceUrl, previousSourceUrl, fetch count)."""
    if KIND == 'sitemap' and isinstance(spec, str):
        spec = {'url': spec}
    if KIND == 'sitemap':
        valid = isinstance(spec, dict) and not set(spec) - {'url', 'previousUrl'} and isinstance(spec.get('url'), str) and isinstance(spec.get('previousUrl', ''), str)
    else:
        valid = isinstance(spec, str)
    if not valid:
        raise ValueError('URL entry must be ' + ('a string or {url, previousUrl?}' if KIND == 'sitemap' else 'a string'))
    url = spec['url'] if KIND == 'sitemap' else spec
    previous_url = spec.get('previousUrl') if KIND == 'sitemap' else None
    expect = 'pdf' if KIND == 'pdf' else 'xml'
    fetched = 0
    async def get(target):
        nonlocal fetched
        try:
            content = await fetcher(target, expect)
        except FetchError as error:
            error.url = error.url or target
            raise
        except Exception:
            raise FetchError('FETCH_FAILED', 'Could not fetch URL', target) from None
        fetched += 1
        summary['fetchedUrls'] += 1
        return content
    content = await get(url)
    if KIND == 'pdf':
        item = {'pdfBase64': base64.b64encode(content).decode('ascii'), **defaults}
    else:
        item = {'xml': content, **defaults}
    if previous_url:
        item['previousXml'] = await get(previous_url)
    return item, url, previous_url or None, fetched

async def process_input(actor_input, actor=Actor, worker=run_worker, fetcher=fetch_document):
    summary = {'processed': 0, 'errors': [], 'stoppedForSpendingLimit': False, 'fetchedUrls': 0}
    allowed_top = {'publicDataOnly', 'documents', URL_FIELD, *URL_DEFAULTS}
    if not isinstance(actor_input, dict) or actor_input.get('publicDataOnly') is not True:
        summary['errors'].append({'error': 'INPUT_POLICY', 'message': 'Set publicDataOnly=true only for permitted public non-personal documents'})
    elif set(actor_input) - allowed_top:
        summary['errors'].append({'error': 'INPUT_INVALID', 'message': 'Unsupported top-level input field'})
    else:
        documents = actor_input.get('documents')
        urls = actor_input.get(URL_FIELD)
        documents = [] if documents is None else documents
        urls = [] if urls is None else urls
        defaults = {key: actor_input[key] for key in URL_DEFAULTS if actor_input.get(key) is not None}
        if not isinstance(documents, list) or not isinstance(urls, list) or not 1 <= len(documents) + len(urls) <= 5:
            summary['errors'].append({'error': 'INPUT_LIMIT', 'message': f'Supply 1-5 items in total across documents and {URL_FIELD}, with total JSON input under 8 MB'})
        elif not shape_valid(defaults, set(URL_DEFAULTS)):
            summary['errors'].append({'error': 'INPUT_INVALID', 'message': 'Top-level ' + '/'.join(URL_DEFAULTS) + ' has an invalid type or size'})
        else:
            manager = actor.get_charging_manager()
            allowed = {'pdf': {'pdfBase64', 'chunkSize', 'chunkOverlap'}, 'sitemap': {'xml', 'previousXml'}, 'feed': {'xml', 'knownIds'}}[KIND]
            # documentIndex counts pasted documents first, then URL entries.
            for index, (is_url, entry) in enumerate([(False, d) for d in documents] + [(True, u) for u in urls]):
                # Check before any expensive work (fetching included), not just after a charge.
                if manager.is_event_charge_limit_reached(EVENT):
                    summary['stoppedForSpendingLimit'] = True
                    break
                source_url = previous_url = None
                fetched = 0
                if is_url:
                    try:
                        item, source_url, previous_url, fetched = await build_url_item(entry, defaults, fetcher, summary)
                    except FetchError as error:
                        summary['errors'].append({'documentIndex': index, 'error': error.code, 'message': error.message, 'url': safe_url(error.url)})
                        continue
                    except ValueError as error:
                        summary['errors'].append({'documentIndex': index, 'error': 'INPUT_INVALID', 'message': str(error)})
                        continue
                    if not shape_valid(item, allowed):
                        summary['errors'].append({'documentIndex': index, 'error': 'INPUT_INVALID', 'message': 'Fetched document has unsupported fields or exceeds limits', 'url': safe_url(source_url)})
                        continue
                else:
                    item = entry
                    if not shape_valid(item, allowed):
                        summary['errors'].append({'documentIndex': index, 'error': 'INPUT_INVALID', 'message': 'Document has unsupported fields or is not an object'})
                        continue
                outcome = await worker(item)
                if not outcome.get('ok'):
                    error = {'documentIndex': index, 'error': outcome.get('error', 'INPUT_INVALID'), 'message': outcome.get('message', 'Unsupported document')}
                    if source_url:
                        error['url'] = safe_url(source_url)
                    summary['errors'].append(error)
                    continue
                result = {'documentIndex': index, 'kind': KIND, **outcome['data'], 'sourceUrl': source_url, 'fetchedUrls': fetched}
                if KIND == 'sitemap':
                    result['previousSourceUrl'] = previous_url
                # SDK saves result and charges it together, with its own spending-limit enforcement/idempotency.
                charge_result = await actor.push_data(result, charged_event_name=EVENT)
                summary['processed'] += 1
                if charge_result.event_charge_limit_reached:
                    summary['stoppedForSpendingLimit'] = True
                    break
    await actor.set_value('OUTPUT', summary)
    message = 'Spending limit reached' if summary['stoppedForSpendingLimit'] else ('Completed with input errors (not charged)' if summary['errors'] else 'Completed')
    await actor.set_status_message(f"{message}; {summary['processed']} documents processed")
    return summary

async def main():
    async with Actor:
        if not await session_guard():
            return
        try:
            actor_input = await load_input()
        except (ValueError, UnicodeDecodeError, RecursionError):
            await Actor.set_value('OUTPUT', {'processed': 0, 'errors': [{'error': 'INPUT_LIMIT', 'message': 'Input is invalid or exceeds JSON byte/depth/complexity bounds'}], 'stoppedForSpendingLimit': False, 'fetchedUrls': 0})
            await Actor.set_status_message('Input rejected before processing; no document event charged')
            return
        await process_input(actor_input)

if __name__ == '__main__':
    asyncio.run(main())
