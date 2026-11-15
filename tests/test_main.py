import sys

import uvicorn

from readme_batch.__main__ import WINDOWED_LOG_CONFIG, main


def test_main_opens_loopback_ui_and_serves_without_reload(monkeypatch):
    calls = {}

    class FakeServer:
        def __init__(self, config):
            self.config = config
            self.should_exit = False
            calls["server"] = self

        def run(self):
            calls["ran"] = True

    monkeypatch.setattr("readme_batch.__main__.uvicorn.Server", FakeServer)
    monkeypatch.setattr("readme_batch.__main__.webbrowser.open", lambda url: calls.setdefault("url", url))

    main()

    config = calls["server"].config
    assert calls["url"] == "http://127.0.0.1:8765/"
    assert calls["ran"] is True
    assert config.host == "127.0.0.1"
    assert config.port == 8765
    assert config.host != "0.0.0.0"
    assert config.log_config is WINDOWED_LOG_CONFIG
    assert config.app.state.server is calls["server"]
    assert config.reload is False


def test_windowed_logging_does_not_require_stdout_or_stderr(monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    uvicorn.Config(app=object(), log_config=WINDOWED_LOG_CONFIG)
