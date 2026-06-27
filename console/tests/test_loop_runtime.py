"""Tests for console.services.loop_runtime — parsing the boot-inject runtime
/loop prompt from a systemd drop-in (board #331)."""
from __future__ import annotations

from console.services import loop_runtime


_SELF_PACED_CONF = (
    "[Service]\n"
    "ExecStartPost=/bin/sh -c \"sleep 8 && printf '%s\\n' "
    "'Resume your OODA loop. Arm your OWN next wake with a single CronCreate. "
    "Never hardcode an hourly cron.' >> /home/claude/.claude/channels/telegram-ben/inject\"\n"
)

_FIXED_CONF = (
    "[Service]\n"
    "ExecStartPost=/bin/sh -c \"sleep 8 && printf "
    "'Arm a /loop cron every 1h. Then run your full tick NOW.\\n' "
    ">> /home/claude/.claude/channels/telegram-ben/inject\"\n"
)


def test_extract_prompt_self_paced_printf_pct_s_form():
    p = loop_runtime._extract_prompt(_SELF_PACED_CONF)
    assert p is not None
    assert p.startswith("Resume your OODA loop")
    assert "%s" not in p
    assert not p.endswith("\\n")


def test_extract_prompt_fixed_template_form():
    p = loop_runtime._extract_prompt(_FIXED_CONF)
    assert p is not None
    assert p.startswith("Arm a /loop cron every 1h")
    assert "inject" not in p  # only the prompt, not the redirect target


def test_extract_prompt_no_execstartpost_returns_none():
    assert loop_runtime._extract_prompt("[Service]\nExecStart=/bin/true\n") is None


def test_cadence_label():
    assert loop_runtime._cadence_label("... self-paced ...") == "self-paced"
    assert loop_runtime._cadence_label("Arm a /loop cron every 1h") == "fixed-interval"
    assert loop_runtime._cadence_label("something else") == "unknown"


def test_load_rejects_bad_slug():
    assert loop_runtime.load_loop_runtime_prompt("../../etc/passwd") is None
    assert loop_runtime.load_loop_runtime_prompt("") is None
    assert loop_runtime.load_loop_runtime_prompt("Bad Slug!") is None


def test_load_missing_dropin_returns_none(tmp_path, monkeypatch):
    # Point the template at a dir with no drop-in → None, no raise.
    monkeypatch.setattr(
        loop_runtime, "_DROPIN", str(tmp_path / "ops-loop-{slug}.service.d/boot-inject.conf")
    )
    assert loop_runtime.load_loop_runtime_prompt("ghost") is None


def test_load_reads_and_parses(tmp_path, monkeypatch):
    d = tmp_path / "ops-loop-ben.service.d"
    d.mkdir(parents=True)
    (d / "boot-inject.conf").write_text(_SELF_PACED_CONF, encoding="utf-8")
    monkeypatch.setattr(
        loop_runtime, "_DROPIN", str(tmp_path / "ops-loop-{slug}.service.d/boot-inject.conf")
    )
    r = loop_runtime.load_loop_runtime_prompt("ben")
    assert r is not None
    assert r["cadence"] == "self-paced"
    assert r["prompt"].startswith("Resume your OODA loop")
    assert r["source"].endswith("boot-inject.conf")


# ─── Route integration: the panel renders on /dept/<slug> (board #331) ──────

def test_dept_page_shows_runtime_panel_empty_state(client):
    """fixture dept has no boot-inject drop-in → panel shows the title + the
    graceful 'non disponible' note, and the page still renders 200."""
    r = client.get("/dept/fixture")
    assert r.status_code == 200
    assert "Rythme de la boucle" in r.text
    assert "non disponible" in r.text


def test_dept_page_renders_self_paced_prompt(client, monkeypatch):
    """When loop_runtime returns a self-paced prompt, the panel shows the
    'Auto-rythmée' verdict + the prompt text."""
    from console.routes import dept as dept_route
    monkeypatch.setattr(
        dept_route.loop_runtime, "load_loop_runtime_prompt",
        lambda slug: {
            "prompt": "Resume your OODA loop. Never hardcode an hourly cron.",
            "source": "/etc/systemd/system/ops-loop-fixture.service.d/boot-inject.conf",
            "cadence": "self-paced",
        },
    )
    r = client.get("/dept/fixture")
    assert r.status_code == 200
    assert "Auto-rythmée" in r.text
    assert "Resume your OODA loop" in r.text
    assert "boot-inject.conf" in r.text


def test_dept_page_fixed_interval_is_warn_not_fail(client, monkeypatch):
    """A fixed-interval dept is correctly configured → amber 'warn', not red
    'fail' (which would alarm operators). Only 'unknown' is fail."""
    from console.routes import dept as dept_route
    monkeypatch.setattr(
        dept_route.loop_runtime, "load_loop_runtime_prompt",
        lambda slug: {
            "prompt": "Arm a /loop cron every 1h.",
            "source": "/etc/systemd/system/ops-loop-fixture.service.d/boot-inject.conf",
            "cadence": "fixed-interval",
        },
    )
    r = client.get("/dept/fixture")
    assert r.status_code == 200
    assert "backup-latest--warn" in r.text
    assert "backup-latest--fail" not in r.text.split("Rythme de la boucle")[1].split("Filet")[0]
    assert "Intervalle fixe" in r.text
