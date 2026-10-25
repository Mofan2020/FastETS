#!/usr/bin/env python3
"""FastETS launcher.

Usage:
    python run.py                 # start the desktop app
    python run.py --dev           # also open the UI in the system browser
    python run.py --reset-disclaimer
"""

from __future__ import annotations

import sys

from fastets.ui.app import main

if __name__ == "__main__":
    sys.exit(main())
