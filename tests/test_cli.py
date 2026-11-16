import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from x2vault.cli import default_credentials


class DefaultCredentialsTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.new = self.home / ".config/x2vault/credentials.json"
        self.old = self.home / ".config/x-bookmark-exporter/credentials.json"

    def resolve(self):
        out = io.StringIO()
        with patch.object(Path, "home", return_value=self.home), redirect_stdout(out):
            return default_credentials(), out.getvalue()

    def test_new_path_by_default(self):
        self.assertEqual(self.resolve(), (self.new, ""))

    def test_falls_back_to_old_path_with_notice(self):
        self.old.parent.mkdir(parents=True)
        self.old.write_text("{}")
        path, notice = self.resolve()
        self.assertEqual(path, self.old)
        self.assertIn("old credentials", notice)

    def test_new_path_wins_when_both_exist(self):
        for path in (self.old, self.new):
            path.parent.mkdir(parents=True)
            path.write_text("{}")
        self.assertEqual(self.resolve(), (self.new, ""))


if __name__ == "__main__":
    unittest.main()
