#!/usr/bin/env python3
"""Prepare/audit owner-only fleet admin artifacts; never elevates privileges."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.fleet_admin import main

if __name__ == '__main__':
    raise SystemExit(main())
