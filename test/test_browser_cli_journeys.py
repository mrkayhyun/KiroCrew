"""Lifecycle journeys for browsing: what an operator does, and what survives it.

The sequences here are the ones that broke the previous browser stack: a settings
change, a gateway restart, and an update. The property under test is the same in
every case and is stated as a guarantee rather than an implementation detail:
nothing Kiro Crew does to enable browsing writes, rewrites, or deletes
configuration the operator owns.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from kiro_crew.browser_cli import install, snapshots, view


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(snapshots, "config_dir", lambda: tmp_path / "crew")
    (tmp_path / "crew").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _operator_cli_config(home: Path) -> Path:
    """The config file the CLI reads, with fields only an operator would set."""
    path = home / "project" / ".playwright" / "cli.config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "browser": {
                    "launchOptions": {"args": ["--proxy-server=http://corp:3128"]},
                    "contextOptions": {"viewport": {"width": 1920, "height": 1080}},
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


class TestOperatorConfigIsNotOurs:
    """The CLI's config file belongs to the operator, so we never write it."""

    def test_the_package_has_exactly_one_destructive_operation(self):
        # A guard on the property the other cases depend on: if a future change adds
        # a config write, this fails and the reviewer has to justify it rather than
        # discovering it from a support report.
        #
        # Exactly ONE write is sanctioned: token.py persists the optional attach
        # token, which has to survive a restart to be worth configuring. It is
        # named by module so that a second write in token.py still fails here.
        sanctioned = {"token.py"}
        pkg = Path(install.__file__).parent
        writes = []
        for source in sorted(pkg.glob("*.py")):
            for lineno, line in enumerate(source.read_text().splitlines(), 1):
                if any(
                    call in line
                    for call in ("write_text(", "write_bytes(", "rmtree(", "atomic_write(")
                ):
                    writes.append(f"{source.name}:{lineno}")
        unexpected = [w for w in writes if w.split(":", 1)[0] not in sanctioned]
        assert unexpected == [], f"browser_cli must not write files: {unexpected}"
        # And the sanctioned one must still be there: a token that stopped being
        # persisted would silently stop working across restarts.
        assert any(w.startswith("token.py:") for w in writes), "token.py must persist the token"

    def test_a_gateway_restart_leaves_the_config_alone(self, home: Path):
        config = _operator_cli_config(home)
        before = config.read_text(encoding="utf-8")

        # What a restart actually does for browsing: republish the output directory
        # and prune. Neither reads nor writes operator config.
        os.environ.update(snapshots.cli_env_overrides())
        snapshots.prune()

        assert config.read_text(encoding="utf-8") == before

    def test_an_update_leaves_the_config_alone(self, home: Path):
        config = _operator_cli_config(home)
        before = config.read_text(encoding="utf-8")

        # An update re-runs detection. Detection is a PATH and version read.
        install.detect()

        assert config.read_text(encoding="utf-8") == before


class TestSnapshotDirectoryIsOursAlone:
    """Pruning is the one destructive act, and it is scoped to derived output."""

    def test_pruning_cannot_reach_outside_the_snapshot_directory(self, home: Path):
        outside = home / "crew" / "playwright-config-of-mine.json"
        outside.write_text("{}", encoding="utf-8")
        snapshots.snapshot_dir().mkdir(parents=True, exist_ok=True)
        old = time.time() - (10 * 24 * 60 * 60)
        stale = snapshots.snapshot_dir() / "page-2026-01-01T00-00-00-000Z.yml"
        stale.write_text("- generic", encoding="utf-8")
        os.utime(stale, (old, old))
        (snapshots.snapshot_dir() / "page-2026-06-01T00-00-00-000Z.yml").write_text("- generic", encoding="utf-8")

        snapshots.prune(max_age_s=60.0)

        assert outside.exists(), "prune must not touch a sibling of its own directory"
        assert not stale.exists()

    def test_the_output_directory_is_stable_across_calls(self, home: Path, tmp_path: Path):
        # The agent's working directory moves between turns; the directory the
        # service prunes must not move with it.
        first = snapshots.cli_env_overrides()
        monkey_cwd = tmp_path / "elsewhere"
        monkey_cwd.mkdir()
        os.chdir(monkey_cwd)
        assert snapshots.cli_env_overrides() == first
        assert Path(first["PLAYWRIGHT_MCP_OUTPUT_DIR"]).is_absolute()


class TestBothOnboardingPaths:
    """A host that has the CLI already, and a host that does not."""

    def test_an_existing_install_is_used_as_is(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(install, "cli_path", lambda: "/usr/local/bin/playwright-cli")
        monkeypatch.setattr(install, "_first_version", lambda text: "0.1.18")
        monkeypatch.setattr(install, "_node_version", lambda: "22.1.0")
        monkeypatch.setattr(install, "_run", lambda argv, timeout: (0, "0.1.18", ""))

        state = install.detect()

        assert state["installed"] is True
        # Presence is consent, so browsing is available without any further act by
        # the operator and without us reinstalling over their copy.
        assert install.available() is True

    def test_a_fresh_host_reports_what_is_missing_rather_than_guessing(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(install, "cli_path", lambda: None)
        monkeypatch.setattr(install, "_node_version", lambda: "18.0.0")

        state = install.detect()

        assert state["installed"] is False
        assert install.available() is False
        # node_ok is independent of installed, so the card can say "install Node"
        # instead of offering a button that would fail.
        assert state["node_ok"] is False
        assert state["node_version"] == "18.0.0"

    def test_a_missing_binary_refuses_to_start_the_view(self, monkeypatch: pytest.MonkeyPatch):
        # `view` imported the resolver by name, so patching it on `install` would
        # leave view's own reference bound to the real one and start a real server.
        monkeypatch.setattr(view, "cli_path", lambda: None)
        assert view.ensure_running() is None
        assert view.status()["status"] == "unavailable"


class TestAppTokensCannotArmBrowsing:
    """Route scoping is not capability scoping.

    The auth middleware only proves the ROUTE is in a calling app's manifest. It
    does not decide whether an app may ARM the browser, and these mutations are
    exactly the wrong reach for one: the install is what activates browser
    auto-approval, and the attach token silences the browser's own per-attach
    prompt -- the last human checkpoint before a program drives a logged-in
    session. Reads stay open; writes are dashboard-owner only.
    """

    @staticmethod
    def _app_request(path: str, body: dict | None = None):
        from unittest.mock import MagicMock

        req = MagicMock()
        req.path = path
        req.get = lambda key, default=None: "some-app" if key == "app" else default

        async def _json():
            return body or {}

        req.json = _json
        return req

    def _run(self, handler, req):
        import asyncio

        return asyncio.run(handler(req))

    def test_the_token_write_refuses_an_app_token(self):
        from kiro_crew.dashboard.handlers import messaging as msg

        resp = self._run(
            msg.api_browser_token_put,
            self._app_request("/api/browser/token", {"token": "x"}),
        )
        assert resp.status == 403

    def test_the_install_refuses_an_app_token(self):
        from kiro_crew.dashboard.handlers import messaging as msg

        resp = self._run(msg.api_browser_install_start, self._app_request("/api/browser/install"))
        assert resp.status == 403

    def test_the_engine_download_refuses_an_app_token(self):
        from kiro_crew.dashboard.handlers import messaging as msg

        resp = self._run(
            msg.api_browser_engine_install,
            self._app_request("/api/browser/engine", {"engine": "firefox"}),
        )
        assert resp.status == 403


class TestTheViewDoesNotOutliveTheGateway:
    def test_shutdown_stops_the_view(self):
        """`show` runs in its own session, so only an explicit stop reaps it.

        Without this hook an ordinary restart leaves the dashboard process alive
        while the new gateway loses its pid, and the next request starts a second
        process tree.
        """
        import inspect

        from kiro_crew.dashboard import server

        src = inspect.getsource(server)
        assert "_browser_view_shutdown" in src
        assert "app.on_cleanup.append(_browser_view_shutdown)" in src
        assert "browser_cli_view.stop" in src


class TestOneInstallSlotIsNotAFoldedLie:
    """Folding is right for the CLI install; for engines it reports the wrong target.

    The gateway has ONE install slot. The CLI install has one target, so a second
    click means the same work and folding it into a 200 is honest. Engines are
    three DISTINCT targets sharing that slot: answering 200 while a different
    engine downloads makes the panel show WebKit installing when Firefox is.
    """

    def _app_request(self, body):
        from unittest.mock import MagicMock

        req = MagicMock()
        req.path = "/api/browser/engine"
        req.get = lambda key, default=None: default  # not an app token

        async def _json():
            return body

        req.json = _json
        return req

    def test_a_second_engine_request_is_refused_while_one_runs(self):
        import asyncio

        from kiro_crew.dashboard.handlers import messaging as msg

        async def _drive():
            state = type("S", (), {})()
            never_done = asyncio.get_event_loop().create_future()
            state._browser_install_task = never_done
            state._browser_install_error = None
            req = self._app_request({"engine": "webkit"})
            req.app = {"state": state}
            resp = await msg.api_browser_engine_install(req)
            never_done.cancel()
            return resp

        resp = asyncio.run(_drive())
        assert resp.status == 409
        import json as _json

        assert _json.loads(resp.text)["code"] == "install_already_running"
