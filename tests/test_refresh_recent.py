from datetime import date
from types import SimpleNamespace

import refresh_recent as recent


def test_recent_refresh_starts_and_stops_its_container(monkeypatch):
    calls = []
    monkeypatch.setattr(recent, "ensure_unflare", lambda: True)
    monkeypatch.setattr(recent, "refresh", lambda *args: calls.append(("refresh", *args)))
    monkeypatch.setattr(recent, "docker", lambda *args, **kwargs: (
        calls.append(("docker", *args)) or SimpleNamespace(returncode=0, stderr="")
    ))

    assert recent.main(["--through", "2026-10-15", "--no-tests"]) == 0
    assert calls == [
        ("refresh", date(2026, 8, 15), date(2026, 10, 15), None, True, False),
        ("docker", "stop", recent.CONTAINER),
    ]


def test_missing_container_is_created(monkeypatch):
    calls = []
    ready = iter([False, True])
    monkeypatch.setattr(recent, "port_ready", lambda: next(ready))

    def fake_docker(*args, **kwargs):
        calls.append(args)
        if args[0] == "inspect":
            return SimpleNamespace(returncode=1, stdout="", stderr="error: no such object: contract-history-unflare")
        return SimpleNamespace(returncode=0, stdout="container-id", stderr="")

    monkeypatch.setattr(recent, "docker", fake_docker)
    assert recent.ensure_unflare() is True
    assert calls == [
        ("inspect", "--format", "{{.State.Running}}", recent.CONTAINER),
        ("run", "-d", "--rm", "--name", recent.CONTAINER,
         "-p", "127.0.0.1:5002:5002", recent.IMAGE),
    ]


def test_browser_mode_does_not_start_docker(monkeypatch):
    calls = []
    monkeypatch.setattr(recent, "ensure_unflare", lambda: calls.append("docker"))
    monkeypatch.setattr(recent, "refresh", lambda *args: calls.append(args))
    assert recent.main(["--browser", "--through", "2026-10-15"]) == 0
    assert calls == [(date(2026, 8, 15), date(2026, 10, 15), None, False, True)]


def test_days_option_still_selects_a_day_window(monkeypatch):
    calls = []
    monkeypatch.setattr(recent, "refresh", lambda *args: calls.append(args))
    assert recent.main(["--browser", "--through", "2026-10-15", "--days", "21"]) == 0
    assert calls[0][:2] == (date(2026, 9, 24), date(2026, 10, 15))


def test_recent_refresh_uses_checked_pipeline_and_snapshot_sync(monkeypatch):
    calls = []
    monkeypatch.setattr(recent.pipeline, "banner", lambda *args: None)
    monkeypatch.setattr(recent.pipeline, "run", lambda cmd, cwd: (
        calls.append(("run", cmd[1:], cwd)) or SimpleNamespace(returncode=0)
    ))
    monkeypatch.setattr(recent.pipeline, "step_build", lambda args: calls.append(("build", args.psx, args.dry_run)))
    monkeypatch.setattr(recent.pipeline, "step_sync", lambda: calls.append(("sync",)))
    monkeypatch.setattr(recent.pipeline, "step_tests", lambda: calls.append(("tests",)))
    monkeypatch.setattr(recent.pipeline, "step_summary", lambda: calls.append(("summary",)))

    recent.refresh(date(2026, 9, 24), date(2026, 10, 15), None, False)

    assert calls == [
        ("run", ["psx_contracts_v2.py", "--since", "2026-09-24", "--through", "2026-10-15"], recent.HIST),
        ("run", ["psx_crosswalk.py"], recent.HIST),
        ("build", True, False),
        ("sync",),
        ("tests",),
        ("summary",),
    ]


def test_unflare_failure_retries_in_visible_browser(monkeypatch):
    calls = []
    monkeypatch.setattr(recent.pipeline, "banner", lambda *args: None)
    monkeypatch.setattr(recent.pipeline, "run", lambda cmd, cwd: (
        calls.append(cmd[1:]) or SimpleNamespace(returncode=1 if len(calls) == 1 else 0)
    ))
    monkeypatch.setattr(recent, "docker", lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr=""))
    monkeypatch.setattr(recent.pipeline, "step_build", lambda args: None)
    monkeypatch.setattr(recent.pipeline, "step_sync", lambda: None)
    monkeypatch.setattr(recent.pipeline, "step_tests", lambda: None)
    monkeypatch.setattr(recent.pipeline, "step_summary", lambda: None)

    recent.refresh(date(2026, 9, 24), date(2026, 10, 15), None, False)

    assert calls[:3] == [
        ["psx_contracts_v2.py", "--since", "2026-09-24", "--through", "2026-10-15"],
        ["psx_contracts_v2.py", "--since", "2026-09-24", "--through", "2026-10-15", "--browser"],
        ["psx_crosswalk.py"],
    ]
