"""Worker has no inherited Apify token and only parses bounded inline data."""
import json
import resource
import sys

resource.setrlimit(resource.RLIMIT_AS, (160 * 1024 * 1024, 160 * 1024 * 1024))
resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
from config import KIND
from transform import InputError, transform

try:
    item = json.loads(sys.stdin.buffer.read(8_000_001))
    outcome = {'ok': True, 'data': transform(KIND, item)}
except InputError as error:
    outcome = {'ok': False, 'error': 'INPUT_INVALID', 'message': str(error)}
except Exception:
    outcome = {'ok': False, 'error': 'PROCESSING_ERROR', 'message': 'Document could not be processed; no event charged'}
sys.stdout.write(json.dumps(outcome, ensure_ascii=False))
