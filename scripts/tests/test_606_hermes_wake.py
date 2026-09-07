"""Hermes floor wake uses gateway control + /loop state, never a model spawn."""

from pathlib import Path
from types import SimpleNamespace
import sys

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wake_hermes_gateway as hw  # noqa: E402


def _profile(tmp_path: Path) -> Path:
    home = tmp_path / "maya"
    home.mkdir(mode=0o700, exist_ok=True)
    (home / "config.yaml").write_text(
        yaml.safe_dump({"platforms": {"telegram": {"home_chat_id": "42"}}})
    )
    return home


def _control(home, verb):
    return (
        {"pid": 1234, "hermes_home": str(home)}
        if verb == "identify"
        else {"gateway_state": "running", "answering_pid": 1234}
    )


class _DB:
    rows = [{
        "id": "session-1",
        "source": "telegram",
        "chat_id": "42",
        "chat_type": "dm",
        "user_id": "42",
        "session_key": "opaque-key",
        "ended_at": None,
    }]

    def list_sessions_rich(self, **kwargs):
        assert kwargs["source"] == "telegram"
        return list(self.rows)


def test_missing_loop_arms_one_shot_for_exact_home_session(tmp_path):
    calls = []

    class Manager:
        state = None

        def __init__(self, session_id):
            assert session_id == "session-1"

        def set(self, prompt, **kwargs):
            calls.append((prompt, kwargs))

    rc = hw.wake_gateway_loop(
        _profile(tmp_path), tmp_path, "run floor",
        control_query=_control,
        session_db_factory=_DB,
        loop_factory=Manager,
    )

    assert rc == 0
    assert calls == [("run floor", {
        "times": 1,
        "route": {
            "platform": "telegram",
            "chat_id": "42",
            "chat_type": "dm",
            "user_id": "42",
        },
    })]


def test_active_loop_rearms_without_replacing_prompt(tmp_path):
    calls = []

    class Manager:
        state = SimpleNamespace(status="active", awaiting_response=False)

        def __init__(self, session_id):
            pass

        def resume(self):
            calls.append("resume")
            return self.state

        def set(self, *_args, **_kwargs):
            raise AssertionError("active loop replaced")

    assert hw.wake_gateway_loop(
        _profile(tmp_path), tmp_path, "unused replacement",
        control_query=_control,
        session_db_factory=_DB,
        loop_factory=Manager,
    ) == 0
    assert calls == ["resume"]


def test_paused_or_running_loop_is_never_overridden(tmp_path):
    for state in (
        SimpleNamespace(status="paused", awaiting_response=False),
        SimpleNamespace(status="active", awaiting_response=True),
    ):
        class Manager:
            def __init__(self, session_id):
                self.state = state

            def set(self, *_args, **_kwargs):
                raise AssertionError("protected loop replaced")

        assert hw.wake_gateway_loop(
            _profile(tmp_path), tmp_path, "floor",
            control_query=_control,
            session_db_factory=_DB,
            loop_factory=Manager,
        ) == 4


def test_missing_gateway_or_ambiguous_home_session_fails_closed(tmp_path):
    class AmbiguousDB(_DB):
        rows = _DB.rows * 2

    never = lambda _sid: (_ for _ in ()).throw(AssertionError("loop touched"))
    assert hw.wake_gateway_loop(
        _profile(tmp_path), tmp_path, "floor",
        control_query=lambda *_args: None,
        session_db_factory=_DB,
        loop_factory=never,
    ) == 3
    assert hw.wake_gateway_loop(
        _profile(tmp_path), tmp_path, "floor",
        control_query=_control,
        session_db_factory=AmbiguousDB,
        loop_factory=never,
    ) == 3


def test_control_socket_must_identify_the_exact_profile(tmp_path):
    home = _profile(tmp_path)
    wrong = tmp_path / "other-profile"
    wrong.mkdir()
    assert hw.wake_gateway_loop(
        home, tmp_path, "floor",
        control_query=lambda _home, verb: (
            {"pid": 1234, "hermes_home": str(wrong)}
            if verb == "identify" else
            {"gateway_state": "running", "answering_pid": 1234}
        ),
        session_db_factory=_DB,
        loop_factory=lambda _sid: None,
    ) == 3


def test_profile_symlink_is_rejected_before_control_access(tmp_path):
    real = _profile(tmp_path)
    link = tmp_path / "linked"
    link.symlink_to(real, target_is_directory=True)
    assert hw.wake_gateway_loop(
        link, tmp_path, "floor",
        control_query=lambda *_args: (_ for _ in ()).throw(AssertionError("queried")),
        session_db_factory=_DB,
        loop_factory=lambda _sid: None,
    ) == 2
