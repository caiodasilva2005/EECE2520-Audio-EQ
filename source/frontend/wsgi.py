"""WSGI entry point for serving the Dash frontend under gunicorn.

gunicorn imports a module-level WSGI callable rather than calling main(), so
this module builds the app at import time and exposes Dash's underlying Flask
server as `server`. Configuration comes from environment variables since
gunicorn doesn't forward CLI args:

    FRONTEND_SAMPLES   directory scanned for audio files (default: ./samples)
    FRONTEND_SOCKET    DAC writer daemon socket (default: controller default)
    FRONTEND_DRY_RUN   set to 1/true to run without the DAC daemon

Run locally:
    gunicorn --chdir source/frontend wsgi:server
"""

import os

from app import create_app
from controller import PlaybackController, DEFAULT_SOCKET_PATH


def _truthy(value):
    return str(value).lower() in ("1", "true", "yes", "on")


samples_dir = os.environ.get("FRONTEND_SAMPLES", "./samples")
socket_path = os.environ.get("FRONTEND_SOCKET", DEFAULT_SOCKET_PATH)
dry_run = _truthy(os.environ.get("FRONTEND_DRY_RUN", ""))

controller = PlaybackController(socket_path=socket_path, dry_run=dry_run)
app = create_app(controller, samples_dir)

# gunicorn target: `wsgi:server`
server = app.server
