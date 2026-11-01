import webbrowser

import uvicorn

from readme_batch.app import create_app

HOST = "127.0.0.1"
PORT = 8765


def main() -> None:
    webbrowser.open(f"http://{HOST}:{PORT}/")
    uvicorn.run(create_app(), host=HOST, port=PORT, access_log=False)


if __name__ == "__main__":
    main()
