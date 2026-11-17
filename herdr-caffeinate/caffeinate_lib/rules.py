"""Pattern rules that sort a background command into finite or persistent work.

rules.json is read by this module and by the Claude Code mod, so patterns stick
to the regex subset Python and JavaScript share, and match case-insensitively.
Persistent rules are tried first, so `jest --watch` is persistent although
`jest` alone is finite. A rule's optional `unless` pattern vetoes it.
"""

import os
import re

from . import paths
from .fsutil import read_json

FINITE = "finite"
PERSISTENT = "persistent"
UNKNOWN = "unknown"


def default_rules_path():
    return os.path.join(paths.plugin_root(), "rules.json")


def load_rules(extra_path=None):
    """The shipped rules, with the user's rules.json in the config dir tried first."""
    rules = read_json(default_rules_path(), {}) or {}
    user = read_json(extra_path or os.path.join(paths.config_dir(), "rules.json"), {}) or {}
    merged = {}
    for section in ("command", "description"):
        merged[section] = {}
        for kind in (PERSISTENT, FINITE):
            merged[section][kind] = list((user.get(section) or {}).get(kind, [])) + list(
                (rules.get(section) or {}).get(kind, [])
            )
    return merged


def _first_hit(text, rule_list):
    for rule in rule_list:
        try:
            if re.search(rule["pattern"], text, re.I) and not (
                rule.get("unless") and re.search(rule["unless"], text, re.I)
            ):
                return rule.get("name", rule["pattern"])
        except (re.error, KeyError):
            continue  # a bad user rule must not break classification
    return None


def _classify_text(text, section):
    persistent = _first_hit(text, section.get(PERSISTENT, []))
    if persistent:
        return PERSISTENT, persistent
    finite = _first_hit(text, section.get(FINITE, []))
    if finite:
        return FINITE, finite
    return UNKNOWN, None


def classify(command, description="", rules=None):
    """(class, label) for a command and the description its agent gave it.

    The command decides when it matches a rule. Otherwise the description
    decides, but only when it points one way: a description that reads as both
    finite and persistent stays unknown.
    """
    rules = rules or load_rules()
    cls, label = _classify_text(command or "", rules.get("command", {}))
    if cls != UNKNOWN:
        return cls, label
    persistent = _first_hit(description or "", rules.get("description", {}).get(PERSISTENT, []))
    finite = _first_hit(description or "", rules.get("description", {}).get(FINITE, []))
    if persistent and not finite:
        return PERSISTENT, persistent
    if finite and not persistent:
        return FINITE, finite
    return UNKNOWN, None


def normalize_command(command):
    """Whitespace-collapsed command, the key a repeated command is recognised by."""
    return re.sub(r"\s+", " ", (command or "").strip())
