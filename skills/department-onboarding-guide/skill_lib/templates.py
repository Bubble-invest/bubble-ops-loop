"""
templates.py — Jinja2-based renderer for the 4 onboarding templates.

Templates live under skills/department-onboarding-guide/templates/.
Renderers are deterministic: same input -> same output.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import jinja2

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


def _env() -> jinja2.Environment:
    """Build a Jinja2 env that resolves templates from TEMPLATES_DIR."""
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=False,
        keep_trailing_newline=True,
        undefined=jinja2.StrictUndefined,  # fail loud on missing vars
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_template(name: str, context: Dict[str, Any]) -> str:
    """
    Render a template by short name.

    Aliases (the short name -> template filename):
        dept.yaml       -> dept.yaml.template
        mission.yaml    -> mission.yaml.template
        gate_policy.yaml-> gate_policy.yaml.template
        test_fixture    -> test_fixture.template.yaml

    Returns the rendered string. Raises jinja2.UndefinedError if a required
    context key is missing (StrictUndefined).
    """
    aliases = {
        "dept.yaml": "dept.yaml.template",
        "mission.yaml": "mission.yaml.template",
        "gate_policy.yaml": "gate_policy.yaml.template",
        "test_fixture": "test_fixture.template.yaml",
    }
    fname = aliases.get(name, name)
    env = _env()
    tmpl = env.get_template(fname)
    return tmpl.render(**context)
