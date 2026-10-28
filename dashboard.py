#!/usr/bin/env python3
"""Entry point: `dashboard.py open` (action) and `dashboard.py run` (popup pane)."""
import sys

if sys.version_info < (3, 11):
    sys.exit("herdr-dashboard needs Python 3.11+ (tomllib); found %d.%d" % sys.version_info[:2])

from herdr_dashboard.cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
