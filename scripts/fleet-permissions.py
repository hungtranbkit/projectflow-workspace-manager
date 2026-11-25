#!/usr/bin/env python3
"""Stable absolute-path entry point; target cwd need not contain ProjectFlow."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.fleet_permissions import main

if __name__ == '__main__':
    raise SystemExit(main())
