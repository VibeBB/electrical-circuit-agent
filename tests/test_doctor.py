from types import SimpleNamespace
from typing import Any

from _pytest.monkeypatch import MonkeyPatch

from circuit import doctor


def test_doctor_is_fail_closed_when_tools_are_missing(monkeypatch: MonkeyPatch) -> None:
    def missing(_name: str) -> str | None:
        return None

    monkeypatch.setattr(doctor.shutil, "which", missing)
    result = doctor.checks()
    assert any(item["name"] == "kicad-cli" and item["status"] == "fail" for item in result)
    assert any(item["name"] == "konnect" and item["status"] == "fail" for item in result)


def test_doctor_passes_with_available_tools(monkeypatch: MonkeyPatch) -> None:
    def available(_name: str) -> str:
        return "/usr/bin/tool"

    def fake_run(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

    def is_dir(_self: doctor.Path) -> bool:
        return True

    monkeypatch.setattr(doctor.shutil, "which", available)
    monkeypatch.setattr(doctor, "version", lambda: "10.99.0")
    monkeypatch.setattr(doctor.subprocess, "run", fake_run)
    monkeypatch.setattr(doctor.Path, "is_dir", is_dir)
    result = doctor.checks()
    assert all(item["status"] == "ok" for item in result)
