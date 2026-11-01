from readme_batch.__main__ import main


def test_main_opens_loopback_ui_and_serves_without_reload(monkeypatch):
    calls = {}
    monkeypatch.setattr("readme_batch.__main__.webbrowser.open", lambda url: calls.setdefault("url", url))
    def fake_run(app, **kwargs):
        calls["app"] = app
        calls["kwargs"] = kwargs

    monkeypatch.setattr("readme_batch.__main__.uvicorn.run", fake_run)

    main()

    assert calls["url"] == "http://127.0.0.1:8765/"
    assert calls["kwargs"]["host"] == "127.0.0.1"
    assert calls["kwargs"]["port"] == 8765
    assert calls["kwargs"]["host"] != "0.0.0.0"
    assert "reload" not in calls["kwargs"]
    assert "debug" not in calls["kwargs"]
