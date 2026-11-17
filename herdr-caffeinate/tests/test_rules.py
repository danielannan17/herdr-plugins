import unittest

from caffeinate_lib import rules


class ClassifyTest(unittest.TestCase):
    def setUp(self):
        self.rules = rules.load_rules("/nonexistent/rules.json")

    def cls(self, command, description=""):
        return rules.classify(command, description, self.rules)[0]

    def test_persistent_commands(self):
        for command in (
            "npm run dev", "pnpm dev", "yarn start", "bun run dev", "next dev -p 3001",
            "npx vite", "vite --host", "jest --watch", "nodemon server.js", "tail -f app.log",
            "npx serve out", "python3 -m http.server 8000", "expo start", "npx expo start --ios",
            "supabase start", "docker compose up", "docker-compose up web", "tsc --watch",
            "wrangler dev", "vitest",
        ):
            self.assertEqual(self.cls(command), "persistent", command)

    def test_finite_commands(self):
        for command in (
            "npm run build", "pnpm test", "vite build", "vitest run", "pytest -x", "cargo build --release",
            "cargo test", "xcodebuild -scheme App", "supabase db push", "npm run migrate", "vercel deploy",
            "tsc --noEmit", "eslint .", "./gradlew assembleDebug",
            "jest --watchAll=false", "npm ci",
        ):
            self.assertEqual(self.cls(command), "finite", command)

    def test_unknown_when_nothing_matches(self):
        self.assertEqual(self.cls("./run-my-thing.sh --fast"), "unknown")

    def test_description_decides_only_when_command_does_not(self):
        self.assertEqual(self.cls("./go.sh", "Run the test suite"), "finite")
        self.assertEqual(self.cls("./go.sh", "Start the dev server"), "persistent")
        self.assertEqual(self.cls("npm run build", "Start the dev server"), "finite")
        self.assertEqual(self.cls("./go.sh", "Build then serve the app"), "unknown")

    def test_label_names_the_rule(self):
        self.assertEqual(rules.classify("npm run build", "", self.rules), ("finite", "build"))

    def test_normalize_command(self):
        self.assertEqual(rules.normalize_command("  npm   run\tbuild \n"), "npm run build")

    def test_bad_user_rule_is_skipped(self):
        bad = {"command": {"persistent": [{"name": "x", "pattern": "("}], "finite": []}}
        self.assertEqual(rules.classify("sleep 1", "", {**self.rules, "command": {
            "persistent": bad["command"]["persistent"], "finite": []}})[0], "unknown")


if __name__ == "__main__":
    unittest.main()
