# Changelog

## 2026-11-03

Maintenance release. No input, output or pricing changes.

- Security: `pypdf` 6.20.0 → 6.21.4. Fixes 16 published denial-of-service advisories triggered by crafted PDFs (infinite loops, memory blow-ups, very long parsing). Each document is still parsed in a separate process with a 20-second limit.
- Apify Python SDK 4.1.0 → 4.2.0.
- Added `requirements-dev.txt` for the test suite; tests now run in CI on every change.
