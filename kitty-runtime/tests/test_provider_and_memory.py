import tempfile
import unittest
from pathlib import Path

from kitty.agent.providers.openai_compatible import OpenAICompatibleProvider
from kitty.memory.file_context import FileContext


class ProviderAndMemoryTests(unittest.TestCase):
    def test_parses_openai_compatible_tool_call(self):
        response = OpenAICompatibleProvider.parse_response(
            {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "function": {"name": "lookup", "arguments": '{"q":"hello"}'},
                                }
                            ],
                        }
                    }
                ]
            }
        )
        self.assertEqual(response.tool_calls[0].name, "lookup")
        self.assertEqual(response.tool_calls[0].arguments, {"q": "hello"})

    def test_loads_project_guidance_read_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "AGENTS.md").write_text("agent rules", encoding="utf-8")
            (root / "MEMORY.md").write_text("long memory", encoding="utf-8")
            context = FileContext.load(root)
            rendered = context.render()
        self.assertIn("agent rules", rendered)
        self.assertIn("long memory", rendered)
