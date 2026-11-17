#!/usr/bin/env python3
"""Regenerate hooks/rules.ts from ../rules.json (the mod cannot import JSON)."""

import json
import os

here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "..", "rules.json"), encoding="utf-8") as fh:
    rules = json.load(fh)
body = json.dumps(rules, indent=2, ensure_ascii=False)
with open(os.path.join(here, "hooks", "rules.ts"), "w", encoding="utf-8") as fh:
    fh.write("// Generated from ../../rules.json by sync-rules.py. Do not edit.\n")
    fh.write("import type { Rules } from './core'\n\n")
    fh.write("export const DEFAULT_RULES: Rules = " + body + "\n")
