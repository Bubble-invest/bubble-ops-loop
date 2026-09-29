"""The headless skill must authorize the native paired-eval tool path (#1496)."""
from pathlib import Path

import yaml


REPO = Path(__file__).resolve().parents[1]


def test_skillsmith_authorizes_native_eval_and_evidence_tools():
    text = (REPO / "skills/skill-authoring/SKILL.md").read_text()
    frontmatter = yaml.safe_load(text.split("---", 2)[1])
    # Task executes paired probes in the parent session; Read/Write retain
    # their evidence. Bash/Skill support the existing collectors and routing.
    assert {"Task", "Read", "Write", "Bash", "Skill"} <= set(
        frontmatter.get("allowed-tools", [])
    )
