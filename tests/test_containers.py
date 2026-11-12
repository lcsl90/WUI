"""Live-Übersicht der Container (Docker / Podman): Auswertung der ps-Ausgabe und das Fenster unten."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from rich.console import Console

import wslui
from conftest import FakeClipboard, wait_until

DOCKER_PS = "\n".join(
    json.dumps(row)
    for row in [
        {"Names": "api", "Image": "corp/api:1.4", "State": "running", "Status": "Up 3 hours (healthy)",
         "Ports": "0.0.0.0:8080->80/tcp, [::]:8080->80/tcp"},
        {"Names": "migrate", "Image": "corp/migrate:1.4", "State": "exited", "Status": "Exited (0) 3 hours ago", "Ports": ""},
        {"Names": "worker", "Image": "corp/worker:1.4", "State": "exited", "Status": "Exited (1) 5 minutes ago", "Ports": ""},
        {"Names": "db", "Image": "postgres:16", "State": "running", "Status": "Up 3 hours", "Ports": "5432/tcp"},
        {"Names": "cache", "Image": "redis:7", "State": "restarting", "Status": "Restarting (2) 3 seconds ago", "Ports": ""},
        {"Names": "web", "Image": "nginx", "State": "running", "Status": "Up 5 seconds (health: starting)", "Ports": ""},
        {"Names": "sick", "Image": "nginx", "State": "running", "Status": "Up 2 hours (unhealthy)", "Ports": ""},
    ]
)

PODMAN_PS = json.dumps(
    [
        {"Names": ["web"], "Image": "docker.io/library/nginx:latest", "State": "running", "Status": "Up 2 hours",
         "ExitCode": 0, "Ports": [{"host_ip": "", "host_port": 8080, "container_port": 80, "protocol": "tcp"}]},
        {"Names": ["job"], "Image": "localhost/job:latest", "State": "exited", "Status": "Exited (3) 1 minute ago",
         "ExitCode": 3, "Ports": None},
    ]
)


def test_docker_output_is_parsed_and_problems_come_first() -> None:
    containers = wslui.parse_containers(DOCKER_PS)

    assert [(c.name, c.health) for c in containers] == [
        ("cache", "error"),
        ("sick", "error"),
        ("worker", "error"),
        ("web", "warn"),
        ("api", "ok"),
        ("db", "ok"),
        ("migrate", "off"),
    ]
    by_name = {c.name: c for c in containers}
    assert by_name["api"].ports == "8080->80/tcp"  # IPv4- und IPv6-Eintrag zusammengefasst
    assert by_name["worker"].exit_code == 1
    assert by_name["api"].status == "Up 3 hours (healthy)"


def test_podman_json_array_is_parsed() -> None:
    web, job = sorted(wslui.parse_containers(PODMAN_PS), key=lambda c: c.name, reverse=True)

    assert (web.name, web.health, web.ports) == ("web", "ok", "8080->80/tcp")
    assert (job.name, job.health, job.exit_code) == ("job", "error", 3)


def test_empty_output_means_no_containers() -> None:
    assert wslui.parse_containers("") == []
    assert wslui.parse_containers("[]\n") == []


def test_summary_counts_by_condition() -> None:
    assert wslui.container_summary(wslui.parse_containers(DOCKER_PS)) == "2 laufen · 3 Fehler · 1 im Übergang · 1 beendet"
    assert wslui.container_summary([]) == "0 laufen"


@pytest.mark.parametrize("width", [24, 46, 70, 120])
def test_table_always_fits_and_keeps_name_and_status(width: int) -> None:
    containers = wslui.parse_containers(DOCKER_PS)
    console = Console(width=200, record=True)

    console.print(wslui.container_table(containers, width))
    lines = console.export_text().splitlines()

    assert max(len(line.rstrip()) for line in lines) <= width
    assert "NAME" in lines[0] and "STATUS" in lines[0]
    assert lines[1].startswith("✖  cache")


def test_narrow_table_drops_ports_before_image_and_never_the_name() -> None:
    containers = wslui.parse_containers(DOCKER_PS)

    assert wslui.container_column_widths(containers, 200) == {"name": 7, "status": 31, "image": 16, "ports": 12}
    medium = wslui.container_column_widths(containers, 50)
    assert medium["ports"] == 0 and medium["image"] > 0
    narrow = wslui.container_column_widths(containers, 20)
    assert narrow["image"] == 0 and narrow["name"] == 7 and narrow["status"] >= 8


def test_missing_program_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wslui.shutil, "which", lambda name: None)

    assert wslui.run_container_ps("podman") == ([], "Podman ist nicht installiert ('podman' nicht gefunden).")
    assert wslui.available_engines() == ["docker"]  # der Docker-Knopf bleibt und zeigt den Hinweis


def test_only_podman_installed_offers_only_podman(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wslui.shutil, "which", lambda name: "/usr/bin/podman" if name == "podman" else None)

    assert wslui.available_engines() == ["podman"]


# ---------- Fenster ----------


@pytest.fixture
def engines(monkeypatch: pytest.MonkeyPatch) -> dict[str, tuple[str, str | None]]:
    """Ersetzt den Aufruf von docker/podman: Programm → (ps-Ausgabe, Fehlermeldung)."""
    outputs: dict[str, tuple[str, str | None]] = {"docker": (DOCKER_PS, None), "podman": (PODMAN_PS, None)}

    def fake_ps(engine: str) -> tuple[list[wslui.Container], str | None]:
        output, error = outputs[engine]
        return wslui.parse_containers(output), error

    monkeypatch.setattr(wslui, "run_container_ps", fake_ps)
    monkeypatch.setattr(wslui, "available_engines", lambda: list(outputs))
    monkeypatch.setattr(wslui.ContainerPanel, "REFRESH_SECONDS", 0.05)
    return outputs


def make_app(folder: Path, clipboard: FakeClipboard) -> wslui.WSLUI:
    app = wslui.WSLUI(folder, clipboard=clipboard)
    app.cfg["layout"] = "1"
    return app


def topbar_text(app: wslui.WSLUI) -> str:
    return str(app.screen.query_one("#topbar", wslui.ClickBar).content)


async def test_d_opens_the_live_docker_view_below_the_files(tmp_path: Path, clipboard: FakeClipboard, engines) -> None:
    app = make_app(tmp_path, clipboard)

    async with app.run_test(size=(170, 40)) as pilot:
        await wait_until(pilot, lambda: len(app.screen.panes) == 1)
        screen = app.screen
        panel = screen.container_panel("docker")
        assert not panel.is_open and not screen.dock.display

        await pilot.press("d")
        await wait_until(pilot, lambda: len(panel.containers) == 7)

        assert panel.is_open and screen.dock.display
        assert panel.region.y >= screen.panes[0].region.bottom
        assert panel.has_class("-problem")  # es gibt abgestürzte Container
        assert "3 Fehler" in str(panel.border_subtitle)
        assert not screen.container_panel("podman").is_open
        assert app.cfg["containers"] == ["docker"]

        await pilot.press("d")
        await pilot.pause()
        assert not panel.is_open and not screen.dock.display
        assert app.cfg["containers"] == []


async def test_view_follows_changes_without_any_key(tmp_path: Path, clipboard: FakeClipboard, engines) -> None:
    app = make_app(tmp_path, clipboard)

    async with app.run_test(size=(170, 40)) as pilot:
        await wait_until(pilot, lambda: len(app.screen.panes) == 1)
        panel = app.screen.container_panel("docker")
        await pilot.press("d")
        await wait_until(pilot, lambda: len(panel.containers) == 7)

        engines["docker"] = (json.dumps({"Names": "api", "State": "running", "Status": "Up 1 second"}), None)

        await wait_until(pilot, lambda: [c.name for c in panel.containers] == ["api"])
        assert not panel.has_class("-problem")


async def test_unreachable_daemon_shows_the_message_instead_of_a_table(
    tmp_path: Path, clipboard: FakeClipboard, engines
) -> None:
    engines["docker"] = ("", "Cannot connect to the Docker daemon")
    app = make_app(tmp_path, clipboard)

    async with app.run_test(size=(170, 40)) as pilot:
        await wait_until(pilot, lambda: len(app.screen.panes) == 1)
        panel = app.screen.container_panel("docker")

        await pilot.press("d")
        await wait_until(pilot, lambda: panel.error is not None)

        assert panel.error == "Cannot connect to the Docker daemon"
        assert "nicht erreichbar" in str(panel.border_subtitle)


async def test_topbar_buttons_toggle_docker_and_podman_side_by_side(
    tmp_path: Path, clipboard: FakeClipboard, engines
) -> None:
    app = make_app(tmp_path, clipboard)

    async with app.run_test(size=(170, 40)) as pilot:
        await wait_until(pilot, lambda: len(app.screen.panes) == 1)
        screen = app.screen
        await pilot.pause()
        bar = topbar_text(app)
        assert "▦ Docker" in bar and "▦ Podman" in bar

        await pilot.click("#topbar", offset=(bar.index("▦ Docker") + 3, 0))
        await pilot.click("#topbar", offset=(bar.index("▦ Podman") + 3, 0))
        docker, podman = screen.container_panel("docker"), screen.container_panel("podman")
        await wait_until(pilot, lambda: len(docker.containers) == 7 and len(podman.containers) == 2)

        assert docker.region.right <= podman.region.x and docker.region.y == podman.region.y
        assert app.cfg["containers"] == ["docker", "podman"]


async def test_open_views_come_back_on_the_next_start(tmp_path: Path, clipboard: FakeClipboard, engines) -> None:
    app = make_app(tmp_path, clipboard)
    app.cfg["containers"] = ["podman"]

    async with app.run_test(size=(170, 40)) as pilot:
        await wait_until(pilot, lambda: len(app.screen.panes) == 1)
        panel = app.screen.container_panel("podman")

        await wait_until(pilot, lambda: len(panel.containers) == 2)
        assert panel.is_open and not app.screen.container_panel("docker").is_open


async def test_narrow_window_keeps_the_most_important_buttons(tmp_path: Path, clipboard: FakeClipboard, engines) -> None:
    app = make_app(tmp_path, clipboard)

    async with app.run_test(size=(125, 40)) as pilot:
        await wait_until(pilot, lambda: len(app.screen.panes) == 1)
        await pilot.pause()

        bar = topbar_text(app)
        assert ">_ Terminal" in bar and "? Hilfe" in bar and "▦ Podman" in bar
        assert "Favoriten" not in bar  # früher verschwanden bei Platzmangel alle Knöpfe rechts
