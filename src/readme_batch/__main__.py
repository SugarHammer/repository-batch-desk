import webbrowser

import uvicorn

from readme_batch.app import create_app

HOST = "127.0.0.1"
PORT = 8765
WINDOWED_LOG_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"null": {"class": "logging.NullHandler"}},
    "loggers": {
        "uvicorn": {"handlers": ["null"], "level": "INFO", "propagate": False},
        "uvicorn.error": {"handlers": ["null"], "level": "INFO", "propagate": False},
        "uvicorn.access": {"handlers": ["null"], "level": "INFO", "propagate": False},
    },
}


def main() -> None:
    app = create_app()
    config = uvicorn.Config(
        app, host=HOST, port=PORT, access_log=False, log_config=WINDOWED_LOG_CONFIG
    )
    server = uvicorn.Server(config)
    app.state.server = server
    webbrowser.open(f"http://{HOST}:{PORT}/")
    server.run()


if __name__ == "__main__":
    main()
