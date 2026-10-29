#!/usr/bin/env python
"""Serve synthetic listings for extension E2E tests (stdlib only).

Serves fixtures/synthetic-listings on http://127.0.0.1:8901 — the exact
origin in the extension's host_permissions. Run from the repo root:
  .venv/Scripts/python.exe fixtures/serve.py
"""

from __future__ import annotations

import functools
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = "127.0.0.1"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8901
ROOT = Path(__file__).resolve().parent / "synthetic-listings"


def main() -> None:
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(ROOT))
    with ThreadingHTTPServer((HOST, PORT), handler) as server:
        print(f"serving {ROOT} at http://{HOST}:{PORT} (Ctrl+C to stop)")
        server.serve_forever()


if __name__ == "__main__":
    main()
