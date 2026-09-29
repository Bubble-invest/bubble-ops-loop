"""Source-shape fixture for the #1120 canonical-renderer contract."""

LEGACY_OS_USER = "claude"


def agent_workdir(slug: str, os_user: str) -> str:
    return f"/home/claude/agents/{slug}" if os_user == LEGACY_OS_USER else f"/srv/agents/{slug}"


def render_dropin(slug: str, os_user: str) -> str:
    workdir = agent_workdir(slug, os_user)
    return f"WorkingDirectory={workdir}"
