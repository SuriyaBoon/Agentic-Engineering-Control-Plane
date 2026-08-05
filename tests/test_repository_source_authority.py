import unittest
from pathlib import Path

from ae_control_plane.development import DevelopmentController, RepositoryRegistry


ROOT = Path(__file__).resolve().parents[1]


class RepositorySourceAuthorityTests(unittest.TestCase):
    def test_sentinelgrc_uses_authoritative_remote_source(self):
        registry = RepositoryRegistry(ROOT / "config" / "repositories.json")
        repository = registry.get("SentinelGRC")
        controller = DevelopmentController.__new__(DevelopmentController)
        controller.registry = registry

        self.assertNotIn("local_path", repository)
        self.assertEqual(controller._source(repository), repository["clone_url"])
        self.assertEqual(
            repository["clone_url"],
            "https://github.com/SuriyaBoon/SentinelGRC.git",
        )


if __name__ == "__main__":
    unittest.main()
