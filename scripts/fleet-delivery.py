#!/usr/bin/env python3
"""Stable HP-side entry point; owner-approved inputs are outside source bundles."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.fleet_delivery import main
if __name__ == '__main__':
    raise SystemExit(main())
