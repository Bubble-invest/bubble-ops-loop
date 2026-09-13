"""Hermes floor wake uses gateway control + /loop state, never a model spawn."""

from pathlib import Path
from types import SimpleNamespace
import sys
import time

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wake_hermes_gateway as hw  # noqa: E402


EXPECTED_ROUTE = {
    "platform": "telegram",
    "chat_id": "42",
    "chat_type": "dm",
    "user_id": "42",
}


def _loop_state(
    prompt="existing loop",
    *,
    route=None,
    status="active",
    awaiting_response=False,
    next_due_at=None,
    times=0,
):
    return SimpleNamespace(
        prompt=prompt,
        route=dict(EXPECTED_ROUTE if route is None else route),
        status=status,
        awaiting_response=awaiting_response,
        next_due_at=time.time() + 60 if next_due_at is None else next_due_at,
        times=times,
    )


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
        def __init__(self, session_id):
            assert session_id == "session-1"
            self.state = None
            self.persisted = None

        def set(self, prompt, **kwargs):
            calls.append((prompt, kwargs))
            self.state = _loop_state(
                prompt,
                route=kwargs["route"],
                next_due_at=time.time(),
                times=kwargs["times"],
            )
            self.persisted = self.state
            return self.state

        def refresh(self):
            self.state = self.persisted

    rc = hw.wake_gateway_loop(
        _profile(tmp_path), tmp_path, "run floor",
        control_query=_control,
        session_db_factory=_DB,
        loop_factory=Manager,
    )

    assert rc == 0
    assert calls == [("run floor", {
        "times": 1,
        "route": EXPECTED_ROUTE,
    })]


def test_active_loop_rearms_without_replacing_prompt(tmp_path):
    calls = []

    class Manager:
        def __init__(self, session_id):
            self.state = _loop_state(next_due_at=time.time() + 600)
            self.persisted = self.state

        def resume(self):
            calls.append("resume")
            self.state = _loop_state(next_due_at=time.time() + 1)
            self.persisted = self.state
            return self.state

        def refresh(self):
            self.state = self.persisted

        def set(self, *_args, **_kwargs):
            raise AssertionError("active loop replaced")

    assert hw.wake_gateway_loop(
        _profile(tmp_path), tmp_path, "unused replacement",
        control_query=_control,
        session_db_factory=_DB,
        loop_factory=Manager,
    ) == 0
    assert calls == ["resume"]


def test_active_loop_with_missing_or_wrong_route_is_replaced_by_exact_rescue(tmp_path):
    for bad_route in ({}, {"platform": "telegram", "chat_id": "999"}):
        calls = []

        class Manager:
            def __init__(self, session_id):
                self.state = _loop_state(route=bad_route)
                self.persisted = self.state

            def resume(self):
                raise AssertionError("wrong-route loop must not be resumed")

            def set(self, prompt, **kwargs):
                calls.append((prompt, kwargs))
                self.state = _loop_state(
                    prompt,
                    route=kwargs["route"],
                    next_due_at=time.time(),
                    times=kwargs["times"],
                )
                self.persisted = self.state
                return self.state

            def refresh(self):
                self.state = self.persisted

        assert hw.wake_gateway_loop(
            _profile(tmp_path), tmp_path, "floor rescue",
            control_query=_control,
            session_db_factory=_DB,
            loop_factory=Manager,
        ) == 0
        assert calls == [("floor rescue", {"times": 1, "route": EXPECTED_ROUTE})]


def test_silent_resume_persistence_failure_returns_nonzero(tmp_path):
    class Manager:
        def __init__(self, session_id):
            self.persisted = _loop_state(next_due_at=123.0)
            self.state = self.persisted

        def resume(self):
            # Mirrors LoopManager's swallowed save error: in-memory state looks
            # successful, but the persisted row remains unchanged.
            self.state = _loop_state(next_due_at=time.time() + 1)
            return self.state

        def refresh(self):
            self.state = self.persisted

    assert hw.wake_gateway_loop(
        _profile(tmp_path), tmp_path, "floor",
        control_query=_control,
        session_db_factory=_DB,
        loop_factory=Manager,
    ) == 4


def test_silent_set_persistence_failure_returns_nonzero(tmp_path):
    class Manager:
        def __init__(self, session_id):
            self.state = None

        def set(self, prompt, **kwargs):
            self.state = _loop_state(
                prompt,
                route=kwargs["route"],
                next_due_at=time.time(),
                times=kwargs["times"],
            )
            return self.state

        def refresh(self):
            self.state = None

    assert hw.wake_gateway_loop(
        _profile(tmp_path), tmp_path, "floor",
        control_query=_control,
        session_db_factory=_DB,
        loop_factory=Manager,
    ) == 4


def test_paused_or_running_loop_is_never_overridden(tmp_path):
    for state in (
        _loop_state(status="paused"),
        _loop_state(awaiting_response=True),
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
