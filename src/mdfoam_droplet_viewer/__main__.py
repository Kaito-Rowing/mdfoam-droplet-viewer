"""Start the local server and open the browser: python -m mdfoam_droplet_viewer [--port 8765] [--no-browser]."""

import argparse
import threading
import webbrowser


def main() -> None:
    ap = argparse.ArgumentParser(prog="mdfoam_droplet_viewer")
    ap.add_argument("--port", type=int, default=8765, help="port to listen on (default 8765, only on 127.0.0.1)")
    ap.add_argument("--no-browser", action="store_true", help="do not open the browser automatically")
    args = ap.parse_args()
    import uvicorn

    url = f"http://127.0.0.1:{args.port}/"
    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    print(f"mdFOAM Droplet Viewer: {url}  (Ctrl+C to quit)")
    uvicorn.run("mdfoam_droplet_viewer.server:app", host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
