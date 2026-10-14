from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.package_skill import inspect_skill


class SkillHubPackagingRegressionTests(unittest.TestCase):
    def make_skill(self, base: Path) -> Path:
        root = base / "demo-skill"
        root.mkdir()
        (root / "SKILL.md").write_text(
            "---\nname: demo-skill\ndescription: 用于验证提交包。\n---\n\n# Demo\n",
            encoding="utf-8",
        )
        return root

    def test_rejects_platform_specific_wrapper_filename(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_skill(Path(directory))
            agents = root / "agents"
            agents.mkdir()
            wrapper_name = "open" + "ai.yaml"
            (agents / wrapper_name).write_text("interface: {}\n", encoding="utf-8")

            _, errors, _ = inspect_skill(root)

            self.assertTrue(any("platform-rejected wrapper metadata" in error for error in errors))

    def test_rejects_documented_wrapper_path_reference(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_skill(Path(directory))
            references = root / "references"
            references.mkdir()
            wrapper_path = "agents/" + "open" + "ai.yaml"
            (references / "engineering.md").write_text(
                f"Do not package {wrapper_path}.\n",
                encoding="utf-8",
            )

            _, errors, _ = inspect_skill(root)

            self.assertTrue(any("platform-rejected wrapper reference" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
