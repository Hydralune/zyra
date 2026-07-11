from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for package_path in [
    ROOT / "packages" / "core",
    ROOT / "packages" / "commands",
    ROOT / "packages" / "skills",
]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_commands import default_command_registry, parse_slash_command
from zyra_core import create_task_state
from zyra_skills import default_skill_registry


class CommandSkillTests(unittest.TestCase):
    def test_change_command_maps_to_requirement_change_event_hint(self) -> None:
        state = create_task_state("Handle changes.")
        parsed = parse_slash_command(
            "/change switch to a stricter verification target",
            run_id=state.run_id,
            task_id=state.task_id,
        )

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.control_command.name, "/change")
        self.assertEqual(parsed.control_command.metadata["event_hint"], "requirement_change")
        self.assertEqual(parsed.argument_text, "switch to a stricter verification target")

    def test_default_commands_cover_m2_required_surface(self) -> None:
        names = {command.name for command in default_command_registry().list()}

        expected = {
            "/status",
            "/graph",
            "/trace",
            "/agents",
            "/artifacts",
            "/tools",
            "/permissions",
            "/help",
            "/bashes",
            "/clear",
            "/compact",
            "/context",
            "/rewind",
            "/resume",
            "/export",
            "/memory",
            "/init",
            "/model",
            "/doctor",
            "/cost",
            "/usage",
            "/mcp",
            "/skills",
            "/hooks",
            "/plan",
            "/goal",
            "/team-onboarding",
            "/inject",
            "/change",
            "/verify",
            "/eval",
        }

        self.assertTrue(expected.issubset(names))

    def test_usage_command_maps_to_budget_event_hint(self) -> None:
        state = create_task_state("Inspect usage.")
        parsed = parse_slash_command(
            "/usage",
            run_id=state.run_id,
            task_id=state.task_id,
        )

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.control_command.name, "/usage")
        self.assertEqual(parsed.control_command.metadata["event_hint"], "budget_updated")
        self.assertEqual(parsed.control_command.metadata["category"], "model_resource")

    def test_default_skills_are_versioned_zyra_owned_runtime_entries(self) -> None:
        skills = default_skill_registry()
        code_change = skills.get("code-change")
        web_research = skills.get("web-research")

        self.assertEqual(code_change.provenance.source_kind, "builtin")
        self.assertTrue(code_change.version_ref.content_digest)
        self.assertTrue(code_change.version_ref.policy_digest)
        self.assertNotIn("vendor", code_change.skill_root.replace("\\", "/").split("/"))
        self.assertEqual(web_research.metadata.invocation.mode, "fork")
        self.assertEqual(web_research.metadata.invocation.agent, "Researcher")


if __name__ == "__main__":
    unittest.main()
