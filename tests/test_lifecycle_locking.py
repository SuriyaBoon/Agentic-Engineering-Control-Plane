from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from ae_control_plane.locking import FileMutex


class LifecycleLockTests(unittest.TestCase):
    def test_file_mutex_blocks_a_second_process_and_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            lock_path = Path(temporary) / "lifecycle.lock"
            repository_root = Path(__file__).parents[1]
            child = (
                "import sys\n"
                "from ae_control_plane.locking import FileMutex\n"
                "try:\n"
                "    with FileMutex(sys.argv[1], timeout_seconds=0.2):\n"
                "        raise SystemExit(2)\n"
                "except TimeoutError:\n"
                "    raise SystemExit(0)\n"
            )
            with FileMutex(lock_path, timeout_seconds=1):
                result = subprocess.run(
                    [sys.executable, "-c", child, str(lock_path)],
                    cwd=repository_root,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
            self.assertEqual(result.returncode, 0, result.stderr)
            with FileMutex(lock_path, timeout_seconds=1):
                pass


if __name__ == "__main__":
    unittest.main()
