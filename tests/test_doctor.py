from collections.abc import Iterator
from pathlib import Path
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


def test_doctor_honors_circuit_cern_libs_env(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    def available(_name: str) -> str:
        return "/usr/bin/tool"

    def fake_run(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

    cern = tmp_path / "cern-kicad-libs"
    cern.mkdir()
    monkeypatch.setenv("CIRCUIT_CERN_LIBS", str(cern))
    monkeypatch.setattr(doctor.shutil, "which", available)
    monkeypatch.setattr(doctor, "version", lambda: "10.99.0")
    monkeypatch.setattr(doctor.subprocess, "run", fake_run)

    def is_dir(_self: Path) -> bool:
        return True

    monkeypatch.setattr(doctor.Path, "is_dir", is_dir)
    result = doctor.checks()
    item = next(i for i in result if i["name"] == "cern-libraries")
    assert item["detail"] == str(cern)
    assert item["status"] == "ok"


def test_doctor_cern_libraries_falls_back_to_home_opt(monkeypatch: MonkeyPatch) -> None:
    def available(_name: str) -> str:
        return "/usr/bin/tool"

    def fake_run(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

    expected = Path.home() / "opt/circuit/libraries/cern-kicad-libs"
    monkeypatch.delenv("CIRCUIT_CERN_LIBS", raising=False)
    monkeypatch.setattr(doctor.shutil, "which", available)
    monkeypatch.setattr(doctor, "version", lambda: "10.99.0")
    monkeypatch.setattr(doctor.subprocess, "run", fake_run)

    def is_dir(self: Path) -> bool:
        return str(self).startswith(str(expected))

    def no_glob(*_args: Any) -> Iterator[Path]:
        return iter(())

    monkeypatch.setattr(doctor.Path, "is_dir", is_dir)
    monkeypatch.setattr(doctor.Path, "glob", no_glob)
    result = doctor.checks()
    item = next(i for i in result if i["name"] == "cern-libraries")
    assert item["status"] == "ok"
    assert item["detail"] == str(expected)


def test_doctor_cern_libraries_reports_searched_paths(monkeypatch: MonkeyPatch) -> None:
    def available(_name: str) -> str:
        return "/usr/bin/tool"

    def fake_run(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

    monkeypatch.delenv("CIRCUIT_CERN_LIBS", raising=False)
    monkeypatch.setattr(doctor.shutil, "which", available)
    monkeypatch.setattr(doctor, "version", lambda: "10.99.0")
    monkeypatch.setattr(doctor.subprocess, "run", fake_run)

    def no_dirs(_self: Path) -> bool:
        return False

    def no_glob(*_args: Any) -> Iterator[Path]:
        return iter(())

    monkeypatch.setattr(doctor.Path, "is_dir", no_dirs)
    monkeypatch.setattr(doctor.Path, "glob", no_glob)
    result = doctor.checks()
    item = next(i for i in result if i["name"] == "cern-libraries")
    assert item["status"] == "fail"
    detail = item["detail"]
    assert isinstance(detail, str)
    assert "searched" in detail
    assert "/opt/circuit/libraries/cern-kicad-libs" in detail


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
    assert all(item["status"] != "fail" for item in result)
    vision = next(i for i in result if i["name"] == "vision")
    assert vision["status"] == "ok"
    assert "vision=materialize-only" in str(vision["detail"])


def test_doctor_vision_probe_model_lane(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("OPENHANDS_LLM_MODEL", "kimi-k3")
    item = doctor._vision_probe()  # pyright: ignore[reportPrivateUsage]
    assert item["status"] == "ok"
    detail = item["detail"]
    assert isinstance(detail, str)
    assert detail.startswith("vision=model (kimi-k3)")


def test_doctor_vision_probe_profile_lane(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("OPENHANDS_AGENT_PROFILE", "kimi-k3-vision")
    monkeypatch.delenv("OPENHANDS_LLM_MODEL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    item = doctor._vision_probe()  # pyright: ignore[reportPrivateUsage]
    assert item["status"] == "ok"
    detail = item["detail"]
    assert isinstance(detail, str)
    assert detail.startswith("vision=profile (kimi-k3-vision)")


def test_doctor_vision_probe_none_warns_only(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("OPENHANDS_AGENT_PROFILE", raising=False)
    monkeypatch.delenv("CIRCUIT_VISION_PROFILE", raising=False)
    monkeypatch.delenv("OPENHANDS_LLM_MODEL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.setenv("CIRCUIT_AGENT_EVENTS_DIR", str(tmp_path / "missing"))
    item = doctor._vision_probe()  # pyright: ignore[reportPrivateUsage]
    assert item["status"] == "warn"
    assert "vision=none" in str(item["detail"])


def test_doctor_vision_probe_lists_rasterizers(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("OPENHANDS_LLM_MODEL", "kimi-k3")

    def found(_name: str) -> str:
        return "/usr/bin/x"

    monkeypatch.setattr(doctor.shutil, "which", found)
    item = doctor._vision_probe()  # pyright: ignore[reportPrivateUsage]
    assert "rasterizers=pdftoppm,rsvg-convert" in str(item["detail"])
