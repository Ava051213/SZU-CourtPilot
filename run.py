"""Start the local web dashboard."""

from __future__ import annotations

import argparse
import socket
import threading
import time
import webbrowser

import uvicorn


def _free_port(start: int = 8765) -> int:
    for port in range(start, start + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return start


def main() -> None:
    parser = argparse.ArgumentParser(description="SZU-CourtPilot")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    port = args.port or _free_port()
    url = f"http://{args.host}:{port}"
    if not args.no_browser:
        def open_later() -> None:
            time.sleep(1.2)
            webbrowser.open(url)

        threading.Thread(target=open_later, daemon=True).start()

    print(f"\n  SZU-CourtPilot  ->  {url}\n  按 Ctrl+C 退出\n")
    uvicorn.run("szu_booking.web:app", host=args.host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
