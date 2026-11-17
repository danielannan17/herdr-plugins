import json
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ModRulesTest(unittest.TestCase):
    def test_mod_rules_are_generated_from_rules_json(self):
        with open(os.path.join(ROOT, "rules.json"), encoding="utf-8") as fh:
            shipped = json.load(fh)
        with open(os.path.join(ROOT, "claude-mod", "hooks", "rules.ts"), encoding="utf-8") as fh:
            text = fh.read()
        embedded = json.loads(re.search(r"DEFAULT_RULES: Rules = (\{.*\})\s*$", text, re.S).group(1))
        self.assertEqual(embedded, shipped, "run: python3 claude-mod/sync-rules.py")


if __name__ == "__main__":
    unittest.main()
