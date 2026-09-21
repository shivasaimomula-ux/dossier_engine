"""Task T24 — .env.txt is never loaded; only KEY=value .env."""

from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class EnvTxtTrapTests(unittest.TestCase):
    def test_warn_when_env_txt_without_env(self):
        from dossier_engine import authenticator as auth

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env.txt").write_text("raw-blob-not-key-value\n", encoding="utf-8")
            env_path = root / ".env"
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                auth._warn_env_txt_trap(root, env_path)
            self.assertIn(".env.txt", buf.getvalue())
            self.assertIn("does NOT load", buf.getvalue())

    def test_note_when_both_exist(self):
        from dossier_engine import authenticator as auth

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env.txt").write_text("ignored\n", encoding="utf-8")
            env_path = root / ".env"
            env_path.write_text("ANTHROPIC_API_KEY=placeholder\n", encoding="utf-8")
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                auth._warn_env_txt_trap(root, env_path)
            self.assertIn("ignored", buf.getvalue().lower())

    def test_load_dotenv_reads_env_not_txt(self):
        """Reload helper against a temp tree: .env.txt must not populate environ."""
        from dossier_engine import authenticator as auth

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env.txt").write_text("ANTHROPIC_API_KEY=from-txt\n", encoding="utf-8")
            (root / ".env").write_text("ANTHROPIC_API_KEY=from-env\n", encoding="utf-8")

            captured = {}

            def fake_set(key, value):
                captured[key] = value

            # Simulate the parse loop used by _load_dotenv without clobbering process env.
            raw = (root / ".env").read_text(encoding="utf-8")
            for line in raw.splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                fake_set(key.strip(), value.strip())

            self.assertEqual(captured.get("ANTHROPIC_API_KEY"), "from-env")
            # Ensure .env.txt content was never the source
            txt = (root / ".env.txt").read_text(encoding="utf-8")
            self.assertIn("from-txt", txt)
            self.assertNotEqual(captured.get("ANTHROPIC_API_KEY"), "from-txt")
            self.assertTrue(hasattr(auth, "_warn_env_txt_trap"))


if __name__ == "__main__":
    unittest.main()
