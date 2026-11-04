"""deploy.sh must never print a token value (terminal output gets copied and pasted)."""

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class DeployScriptTest(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, "deploy.sh"), encoding="utf-8") as fh:
            self.lines = [ln for ln in fh.read().splitlines() if not ln.lstrip().startswith("#")]

    def test_no_output_command_expands_a_token_value(self):
        out = re.compile(r"\b(printf|echo|ok|say|tee)\b")
        token_value = re.compile(r"getv\s+\"?\$\{?key|getv\s+\"?EIG_\w*TOKEN|\$\{?EIG_\w*TOKEN\b|\$NEW_[AD]\b")
        bad = [ln for ln in self.lines if out.search(ln) and token_value.search(ln)]
        self.assertEqual(bad, [])

    def test_sudo_never_prompts(self):
        body = "\n".join(self.lines)
        self.assertIn('SUDO="sudo -n"', body)
        self.assertNotRegex(body, r"\$SUDO\s+-v|\bsudo\s+-v\b")


if __name__ == "__main__":
    unittest.main()
