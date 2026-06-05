"""Entry point for the Dash frontend.

Audio is provided by uploading a file in the browser, so there's no samples
directory to configure.

Run on the Pi (daemon present):
    python source/frontend/main.py

Run on a dev machine (no DAC daemon):
    python source/frontend/main.py --dry-run
"""

import argparse

from app import create_app
from controller import PlaybackController, DEFAULT_SOCKET_PATH


def main():
    parser = argparse.ArgumentParser(description="Dash frontend for the audio EQ.")
    parser.add_argument("--socket", default=DEFAULT_SOCKET_PATH,
                        help=f"DAC writer daemon socket (default: {DEFAULT_SOCKET_PATH}).")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Bind address (default: 127.0.0.1).")
    parser.add_argument("--port", type=int, default=8050,
                        help="HTTP port (default: 8050).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run without the DAC daemon — useful for UI dev.")
    parser.add_argument("--debug", action="store_true",
                        help="Enable Dash debug mode (hot reload).")
    args = parser.parse_args()

    controller = PlaybackController(socket_path=args.socket, dry_run=args.dry_run)
    app = create_app(controller)
    # use_reloader=False so the worker thread isn't spawned twice in debug.
    app.run(host=args.host, port=args.port, debug=args.debug, use_reloader=False)


if __name__ == "__main__":
    main()
