import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backup_service import _rotate, _sha256


class BackupServiceTests(unittest.TestCase):
    def test_sha256_is_stable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "x.sql.gz"
            path.write_bytes(b"verified backup")
            self.assertEqual(_sha256(path), _sha256(path))

    def test_rotation_keeps_configured_daily_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            for day in range(1, 6):
                (root / f"ems_gpt_2026-09-{day:02d}_02-20-00.sql.gz").write_bytes(b"x")
            _rotate(root, "ems_gpt", daily=2, weekly=0, monthly=0)
            self.assertEqual(len(list(root.glob("*.sql.gz"))), 2)


if __name__ == "__main__":
    unittest.main()
