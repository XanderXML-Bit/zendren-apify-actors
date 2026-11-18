"""Pure, bounded offline public-document transforms. No URL fetching."""
import base64
import binascii
import hashlib
import io
import json
from urllib.parse import urlsplit
from defusedxml.ElementTree import fromstring
from defusedxml.common import DefusedXmlException
from xml.etree.ElementTree import ParseError

MAX_BYTES = 1_048_576
MAX_OUTPUT_BYTES = 1_000_000

class InputError(ValueError):
    pass

def text_value(value, maximum=MAX_BYTES):
    if not isinstance(value, str) or not value or len(value.encode('utf-8')) > maximum:
        raise InputError('Expected nonempty text within the documented byte limit')
    return value

def integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise InputError('Integer setting is outside the documented bounds')
    return value

def public_url(value):
    if not isinstance(value, str) or len(value) > 4096:
        raise InputError('Invalid public HTTP(S) URL in document')
    try:
        parts = urlsplit(value)
        if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
            raise ValueError
        _ = parts.port
        if any(ord(ch) < 32 or ch.isspace() for ch in value):
            raise ValueError
    except ValueError:
        raise InputError('Invalid public HTTP(S) URL in document') from None
    return value

def xml_root(value):
    text_value(value)
    try:
        return fromstring(value, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except (ParseError, DefusedXmlException):
        raise InputError('Malformed XML or forbidden DTD/entity declaration') from None

def pdf_document(item):
    from pypdf import PdfReader
    encoded = text_value(item.get('pdfBase64'), 4 * ((MAX_BYTES + 2) // 3))
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise InputError('Invalid base64 PDF') from None
    if len(raw) > MAX_BYTES or not raw.startswith(b'%PDF-'):
        raise InputError('Expected a PDF no larger than 1 MiB')
    size = integer(item.get('chunkSize', 1500), 250, 4000)
    overlap = integer(item.get('chunkOverlap', 150), 0, min(1000, size-1))
    try:
        reader = PdfReader(io.BytesIO(raw), strict=True)
        if reader.is_encrypted:
            raise InputError('Encrypted PDFs are not supported; do not supply passwords')
        if not 1 <= len(reader.pages) <= 20:
            raise InputError('PDF must contain 1 to 20 pages')
        pages, chunks, char_count = [], [], 0
        for number, page in enumerate(reader.pages, 1):
            text = page.extract_text() or ''
            char_count += len(text)
            if char_count > 200_000:
                raise InputError('Extracted text exceeds 200,000 characters')
            pages.append({'page': number, 'text': text})
            start = 0
            while start < len(text):
                end = min(start+size, len(text))
                chunks.append({'page': number, 'start': start, 'end': end, 'text': text[start:end]})
                if end == len(text):
                    break
                start = end-overlap
        if not char_count:
            raise InputError('PDF has no extractable text; OCR is not provided')
        return {'sha256': hashlib.sha256(raw).hexdigest(), 'pageCount': len(pages), 'pages': pages, 'chunks': chunks, 'characterCount': char_count}
    except InputError:
        raise
    except Exception:
        raise InputError('PDF parsing failed; malformed or unsupported PDF') from None

SM_NS = 'http://www.sitemaps.org/schemas/sitemap/0.9'

def sitemap_parse(xml):
    root = xml_root(xml)
    if root.tag not in ('{' + SM_NS + '}urlset', '{' + SM_NS + '}sitemapindex'):
        raise InputError('Expected a standard namespaced sitemap urlset or sitemapindex')
    is_index = root.tag.endswith('sitemapindex')
    tag = 'sitemap' if is_index else 'url'
    expected_tag = '{' + SM_NS + '}' + tag
    if any(child.tag != expected_tag for child in root):
        raise InputError('Unsupported top-level sitemap element')
    if len(root) > 5000:
        raise InputError('Sitemap exceeds 5,000 entries')
    entries, duplicates, warnings = {}, [], []
    for pos, entry in enumerate(root):
        prefix = '{' + SM_NS + '}'
        loc = public_url((entry.findtext(prefix+'loc') or '').strip())
        record = {'url': loc, 'lastmod': (entry.findtext(prefix+'lastmod') or '').strip() or None}
        if record['lastmod'] is not None and len(record['lastmod']) > 64:
            raise InputError('lastmod exceeds 64 characters')
        if not is_index:
            record['changefreq'] = (entry.findtext(prefix+'changefreq') or '').strip() or None
            if record['changefreq'] not in (None, 'always', 'hourly', 'daily', 'weekly', 'monthly', 'yearly', 'never'):
                warnings.append({'entry': pos, 'code': 'INVALID_CHANGEFREQ'})
                record['changefreq'] = None
            priority = (entry.findtext(prefix+'priority') or '').strip()
            record['priority'] = None
            if priority:
                try:
                    value = float(priority)
                    if not 0 <= value <= 1:
                        raise ValueError
                    record['priority'] = value
                except ValueError:
                    warnings.append({'entry': pos, 'code': 'INVALID_PRIORITY'})
        if loc in entries:
            duplicates.append(loc)
        else:
            entries[loc] = record
    return {'type': 'index' if is_index else 'urlset', 'entries': entries, 'duplicates': duplicates, 'warnings': warnings}

def sitemap_document(item):
    now = sitemap_parse(item.get('xml'))
    previous = sitemap_parse(item['previousXml']) if item.get('previousXml') is not None else None
    if previous is not None and previous['type'] != now['type']:
        raise InputError('Previous and current sitemap types must match')
    current_urls = set(now['entries'])
    previous_urls = set(previous['entries']) if previous else set()
    return {'type': now['type'], 'entryCount': len(current_urls), 'entries': [now['entries'][url] for url in sorted(current_urls)], 'duplicates': now['duplicates'], 'warnings': now['warnings'], 'comparedToPrevious': previous is not None, 'added': sorted(current_urls-previous_urls), 'removed': sorted(previous_urls-current_urls), 'changed': sorted(url for url in current_urls & previous_urls if now['entries'][url] != previous['entries'][url]) if previous else [], 'sha256': hashlib.sha256(item['xml'].encode()).hexdigest(), 'fetchedUrls': 0}

ATOM_NS = '{http://www.w3.org/2005/Atom}'

def feed_document(item):
    root = xml_root(item.get('xml'))
    if root.tag == 'rss':
        channel = root.find('channel')
        if channel is None:
            raise InputError('RSS channel is missing')
        raw_entries = channel.findall('item')
        feed_type = 'rss2'
    elif root.tag == ATOM_NS+'feed':
        raw_entries = root.findall(ATOM_NS+'entry')
        feed_type = 'atom'
    else:
        raise InputError('Only RSS 2.0 and Atom feeds are supported')
    if len(raw_entries) > 500:
        raise InputError('Feed exceeds 500 entries')
    known = item.get('knownIds', [])
    if not isinstance(known, list) or len(known) > 5000 or any(not isinstance(k, str) or not k or len(k.encode()) > 4096 for k in known):
        raise InputError('knownIds must contain at most 5,000 bounded nonempty strings')
    known_set, seen, entries, duplicate_count = set(known), set(), [], 0
    for entry in raw_entries:
        if feed_type == 'rss2':
            link = public_url((entry.findtext('link') or '').strip())
            identifier = (entry.findtext('guid') or link).strip()
            title = entry.findtext('title') or ''
            published = entry.findtext('pubDate') or ''
        else:
            links = [node for node in entry.findall(ATOM_NS+'link') if node.get('rel', 'alternate') == 'alternate']
            link = public_url(links[0].get('href', '').strip() if links else '')
            identifier = (entry.findtext(ATOM_NS+'id') or link).strip()
            title = entry.findtext(ATOM_NS+'title') or ''
            published = entry.findtext(ATOM_NS+'published') or entry.findtext(ATOM_NS+'updated') or ''
        if not identifier or len(identifier.encode()) > 4096 or len(title.encode()) > 2000 or len(published) > 128:
            raise InputError('Feed item identity/title/date exceeds limits')
        if identifier in seen:
            duplicate_count += 1
            continue
        seen.add(identifier)
        entries.append({'id': identifier, 'title': title, 'url': link, 'publishedAt': published or None, 'isNew': identifier not in known_set})
    return {'type': feed_type, 'entries': entries, 'entryCount': len(entries), 'newCount': sum(e['isNew'] for e in entries), 'duplicateCount': duplicate_count, 'sha256': hashlib.sha256(item['xml'].encode()).hexdigest(), 'fetchedUrls': 0}

def transform(kind, item):
    if not isinstance(item, dict):
        raise InputError('Every document must be an object')
    result = {'pdf': pdf_document, 'sitemap': sitemap_document, 'feed': feed_document}[kind](item)
    if len(json.dumps(result, ensure_ascii=False).encode()) > MAX_OUTPUT_BYTES:
        raise InputError('Output exceeds 1 MB; use smaller documents')
    return result
