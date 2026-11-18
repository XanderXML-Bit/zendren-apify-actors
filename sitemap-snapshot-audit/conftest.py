import pytest

def pytest_addoption(parser):
    parser.addoption('--live', action='store_true', help='run live network tests')

def pytest_configure(config):
    config.addinivalue_line('markers', 'live: hits the real network; skipped unless -m live or --live is given')

def pytest_collection_modifyitems(config, items):
    if config.getoption('--live') or 'live' in (config.getoption('-m') or ''):
        return
    skip = pytest.mark.skip(reason='live network test (use --live)')
    for item in items:
        if 'live' in item.keywords:
            item.add_marker(skip)
