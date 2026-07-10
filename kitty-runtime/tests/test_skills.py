import tempfile
import unittest
from pathlib import Path

from kitty.skills.loader import SkillCatalog


class SkillCatalogTests(unittest.TestCase):
    def test_discovers_and_selects_skill(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / ".agents" / "demo" / "skills" / "screenshot" / "SKILL.md"
            path.parent.mkdir(parents=True)
            path.write_text(
                """---
name: web-screenshot
description: Capture a webpage.
triggers: [screenshot, 截图]
---
Use the screenshot tool.
""",
                encoding="utf-8",
            )

            catalog = SkillCatalog.discover(root)
            selected = catalog.select("帮我截图这个网页")

            self.assertEqual(len(catalog.skills), 1)
            self.assertEqual(selected[0].name, "web-screenshot")
            self.assertIn("Use the screenshot tool", catalog.render_context(selected))
