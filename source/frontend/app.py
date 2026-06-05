"""Dash UI for the audio EQ. The layout is intentionally minimal: a file
upload, a cutoff slider that updates while audio is playing, and a few
transport buttons. Callbacks delegate everything to PlaybackController so the
UI code stays free of audio/threading logic.
"""

import base64
import binascii
import tempfile
from pathlib import Path

import numpy as np
from scipy import signal as sig
import plotly.graph_objects as go
from dash import Dash, html, dcc, Input, Output, State, no_update

from controller import (
    PlaybackController,
    STATE_IDLE, STATE_PLAYING, STATE_PAUSED, STATE_DONE, STATE_ERROR,
)

# Spectrogram window. nperseg trades frequency vs time resolution; 1024 with
# 75% overlap is a reasonable default for music-band signals.
SPEC_NPERSEG = 1024
SPEC_NOVERLAP = 768
SPEC_DB_FLOOR = -80.0  # clamp dB so the colorbar isn't dominated by silence

# The figures are sent to the browser as JSON every spec-tick and redrawn by
# Plotly. A full-resolution spectrogram is ~0.5M values (~3 MB JSON each), which
# both holds the server's GIL during serialization (stalling control callbacks)
# and bogs the browser's render loop. Downsampling the matrix to these caps and
# rounding to 1 dB keeps the picture readable while shrinking the payload ~30x.
SPEC_MAX_T = 160   # max time columns sent
SPEC_MAX_F = 200   # max frequency rows sent


# Highpass cutoff must satisfy 0 < f < Nyquist (sample_rate / 2). Nyquist is
# only known once a file is uploaded, so the slider's max/marks are set from
# the file's sample rate in the upload callback. These are just the initial
# (no-file-loaded) values shown before anything is picked.
CUTOFF_MIN = 1
CUTOFF_INIT_MAX = 8000
CUTOFF_DEFAULT = 1000


def _fmt_mmss(seconds):
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


def _fmt_hz(hz):
    hz = int(round(hz))
    if hz >= 1000:
        s = f"{hz / 1000:.1f}".rstrip("0").rstrip(".")
        return f"{s}k"
    return str(hz)


def _cutoff_marks(max_hz):
    """Slider marks spanning CUTOFF_MIN..max_hz (max_hz sits just below the
    file's Nyquist frequency)."""
    positions = [CUTOFF_MIN, max_hz * 0.25, max_hz * 0.5, max_hz * 0.75, max_hz]
    return {int(round(p)): _fmt_hz(p) for p in positions}


def _save_upload(contents, filename, upload_dir):
    """Decode a dcc.Upload payload and write it to upload_dir.

    contents is a 'data:<mime>;base64,<payload>' string. We keep the original
    filename (basename only, so a malicious name can't escape upload_dir) so the
    status line shows something recognizable, and reuse it on repeat uploads so
    the temp dir doesn't grow without bound. Returns the written path.
    Raises ValueError if the payload can't be decoded.
    """
    _, _, b64 = contents.partition(",")
    try:
        raw = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError) as e:
        raise ValueError(f"Could not decode upload: {e}")

    name = Path(filename or "upload").name or "upload"
    dest = Path(upload_dir) / name
    dest.write_bytes(raw)
    return str(dest)


def _empty_spectrogram(title):
    fig = go.Figure()
    fig.update_layout(
        title=title,
        xaxis_title="Time (s)",
        yaxis_title="Frequency (Hz)",
        margin={"l": 50, "r": 20, "t": 40, "b": 40},
        height=240,
        annotations=[{
            "text": "(no audio captured yet)",
            "xref": "paper", "yref": "paper",
            "x": 0.5, "y": 0.5, "showarrow": False,
            "font": {"color": "#888"},
        }],
    )
    return fig


def _spectrogram_figure(samples, sample_rate, title):
    if sample_rate is None or samples.size < SPEC_NPERSEG:
        return _empty_spectrogram(title)
    f, t, Sxx = sig.spectrogram(
        samples, fs=sample_rate,
        nperseg=SPEC_NPERSEG, noverlap=SPEC_NOVERLAP,
        scaling="spectrum",
    )
    Sxx_db = 10.0 * np.log10(Sxx + 1e-12)
    Sxx_db = np.maximum(Sxx_db, SPEC_DB_FLOOR)

    # Downsample (stride) and round before handing off to Plotly to keep the
    # JSON payload small — this is what keeps the UI responsive.
    f_step = max(1, len(f) // SPEC_MAX_F)
    t_step = max(1, len(t) // SPEC_MAX_T)
    Sxx_db = np.round(Sxx_db[::f_step, ::t_step], 1)
    f = f[::f_step]
    t = t[::t_step]

    fig = go.Figure(
        data=go.Heatmap(
            z=Sxx_db, x=t, y=f,
            colorscale="Viridis",
            zmin=SPEC_DB_FLOOR, zmax=0,
            colorbar={"title": "dB"},
        )
    )
    fig.update_layout(
        title=title,
        xaxis_title="Time (s)",
        yaxis_title="Frequency (Hz)",
        margin={"l": 50, "r": 20, "t": 40, "b": 40},
        height=240,
    )
    return fig


def _status_text(status):
    state = status["state"]
    cutoff = status["cutoff"]
    order = status["order"]
    file_ = Path(status["file"]).name if status["file"] else "(none)"
    conn = "connected" if status["connected"] else ("dry-run" if status["dry_run"] else "disconnected")
    line = f"State: {state} | File: {file_} | Cutoff: {cutoff} Hz | Order: {order} | DAC: {conn}"
    if status["error"]:
        line += f"\nError: {status['error']}"
    return line


def _upload_children(filename=None):
    """Contents shown inside the upload drop zone. Without a filename it's the
    drag/drop prompt; once a file is chosen it shows a small icon + the name so
    the box visibly reflects the selection (clicking still re-opens the picker).
    """
    if filename:
        return html.Div([
            html.Span("🎵", style={"marginRight": "8px"}),
            html.Span(filename, style={"fontWeight": "600"}),
        ])
    return html.Div("Drag & drop or click to select an audio file")


def build_layout():
    # Controls + diagnostics live in the left column, spectrograms in the right.
    # flexWrap lets the two columns stack vertically on narrow screens.
    controls = html.Div(
        style={"flex": "1 1 320px", "minWidth": "300px"},
        children=[
            html.Div([
                html.Label("Audio file"),
                dcc.Upload(
                    id="file-upload",
                    children=_upload_children(),
                    multiple=False,
                    style={
                        "width": "100%",
                        "padding": "24px",
                        "borderWidth": "1px",
                        "borderStyle": "dashed",
                        "borderRadius": "6px",
                        "borderColor": "#aaa",
                        "textAlign": "center",
                        "cursor": "pointer",
                        "color": "#555",
                        "boxSizing": "border-box",
                    },
                ),
            ], style={"marginBottom": "16px"}),

            # Single play/pause toggle. The icon (▶ / ⏸) is driven by the
            # status tick from the controller state.
            html.Div([
                html.Button("▶", id="btn-toggle", n_clicks=0,
                            style={"fontSize": "20px", "padding": "4px 16px"}),
            ], style={"marginBottom": "20px"}),

            # Seek slider doubles as a live progress bar: the status tick writes
            # its value/max, and a user drag (on release) seeks. marks=None
            # because raw block counts make poor labels — seek-time carries the
            # readable position.
            html.Div([
                html.Label(id="seek-time", children="0:00 / 0:00"),
                dcc.Slider(
                    id="seek-slider",
                    min=0, max=1, step=1, value=0,
                    marks=None,
                    updatemode="mouseup",
                    tooltip={"placement": "bottom", "always_visible": False},
                ),
            ], style={"marginBottom": "20px"}),
            # Last position the server pushed into seek-slider, so the seek
            # callback can tell a tick echo apart from a real user drag.
            dcc.Store(id="seek-echo", data=0),

            html.Div([
                html.Label(id="cutoff-label", children=f"Cutoff: {CUTOFF_DEFAULT} Hz"),
                dcc.Slider(
                    id="cutoff-slider",
                    min=CUTOFF_MIN, max=CUTOFF_INIT_MAX, step=1,
                    value=CUTOFF_DEFAULT,
                    marks=_cutoff_marks(CUTOFF_INIT_MAX),
                    updatemode="drag",
                    tooltip={"placement": "bottom", "always_visible": False},
                ),
            ], style={"marginBottom": "20px"}),

            html.Pre(id="status-display",
                    style={"background": "#f5f5f5", "padding": "12px",
                           "borderRadius": "6px", "whiteSpace": "pre-wrap"}),
        ],
    )

    spectrograms = html.Div(
        style={"flex": "2 1 480px", "minWidth": "360px"},
        children=[
            html.H3("Spectrograms", style={"marginTop": "0"}),
            dcc.Graph(id="spec-original", figure=_empty_spectrogram("Original signal")),
            dcc.Graph(id="spec-low", figure=_empty_spectrogram("Low band")),
            dcc.Graph(id="spec-high", figure=_empty_spectrogram("High band")),
        ],
    )

    return html.Div(
        style={"maxWidth": "1200px", "margin": "32px auto", "fontFamily": "system-ui, sans-serif"},
        children=[
            html.H2("Audio EQ"),

            html.Div(
                # alignItems:center vertically centers the (shorter) controls
                # column against the taller spectrograms column.
                style={"display": "flex", "flexWrap": "wrap", "gap": "32px",
                       "alignItems": "center"},
                children=[controls, spectrograms],
            ),

            dcc.Interval(id="status-tick", interval=500, n_intervals=0),
            # Spectrograms recompute on a slower tick than the status line: even
            # downsampled, the FFT + Plotly redraw is the heaviest GUI work, so
            # running it less often keeps the controls responsive.
            dcc.Interval(id="spec-tick", interval=2000, n_intervals=0),
        ],
    )


def register_callbacks(app, controller, upload_dir):

    # File upload → save then load. Saving decodes the base64 payload to a real
    # file in upload_dir (the controller re-opens by path on replay, so an
    # on-disk file is required). Loading is synchronous and cheap (opens the
    # file, builds the SOS); it doesn't start playback.
    @app.callback(
        Output("status-display", "children", allow_duplicate=True),
        Output("file-upload", "children"),
        Output("cutoff-slider", "max"),
        Output("cutoff-slider", "marks"),
        Output("cutoff-slider", "value"),
        Input("file-upload", "contents"),
        State("file-upload", "filename"),
        prevent_initial_call=True,
    )
    def on_file_change(contents, filename):
        if not contents:
            return no_update, no_update, no_update, no_update, no_update
        try:
            filepath = _save_upload(contents, filename, upload_dir)
        except (ValueError, OSError) as e:
            return (f"State: error | Upload failed: {e}", _upload_children(),
                    no_update, no_update, no_update)

        # Remember the desired cutoff, then drop to a value valid for any sample
        # rate so building the filter in load() can't fail if the previous
        # cutoff was at/above this file's Nyquist.
        desired = controller.status()["cutoff"]
        controller.set_cutoff(CUTOFF_MIN)
        controller.load(filepath)

        name = Path(filepath).name
        status = controller.status()
        sr = controller.sampling_frequency()
        if status["state"] == STATE_ERROR or sr is None:
            controller.set_cutoff(desired)  # restore; nothing was reconfigured
            return (_status_text(status), _upload_children(name),
                    no_update, no_update, no_update)

        # Highpass requires 0 < cutoff < Nyquist. -1 keeps the slider's top
        # value strictly below Nyquist so butter() stays valid there.
        cutoff_max = max(CUTOFF_MIN, int(sr / 2) - 1)
        value = min(max(desired, CUTOFF_MIN), cutoff_max)
        controller.set_cutoff(value)
        return (_status_text(controller.status()), _upload_children(name),
                cutoff_max, _cutoff_marks(cutoff_max), value)

    # Single play/pause toggle: route on the current state. Playing → pause;
    # paused → resume; anything else (idle/done/error) → start.
    @app.callback(
        Output("status-display", "children", allow_duplicate=True),
        Input("btn-toggle", "n_clicks"),
        prevent_initial_call=True,
    )
    def on_toggle(_n):
        state = controller.status()["state"]
        if state == STATE_PLAYING:
            controller.pause()
        elif state == STATE_PAUSED:
            controller.resume()
        else:
            controller.start()
        return _status_text(controller.status())

    # Seek slider. The status tick also writes this slider's value every tick to
    # show progress, which fires this callback too — so compare against the
    # echoed value to skip those programmatic writes and only seek on a real
    # user drag.
    @app.callback(
        Output("seek-time", "children", allow_duplicate=True),
        Input("seek-slider", "value"),
        State("seek-echo", "data"),
        prevent_initial_call=True,
    )
    def on_seek(value, echo):
        if value == echo:
            return no_update  # programmatic echo from the progress tick
        controller.set_position(value)
        status = controller.status()
        block_sec = status["duration_seconds"] / max(status["total_blocks"], 1)
        return f"{_fmt_mmss(value * block_sec)} / {_fmt_mmss(status['duration_seconds'])}"

    # Cutoff slider — fires continuously while dragging because
    # updatemode='drag'. setCutoffFrequency just rebuilds the SOS, which is
    # cheap enough to do per slider tick.
    @app.callback(
        Output("cutoff-label", "children"),
        Input("cutoff-slider", "value"),
    )
    def on_cutoff(value):
        controller.set_cutoff(value)
        return f"Cutoff: {value} Hz"

    # Background poll so the status line reflects state changes (e.g. the
    # worker thread hitting end-of-file) that aren't triggered by user input.
    # Also drives the toggle icon and the seek slider's live progress.
    @app.callback(
        Output("status-display", "children"),
        Output("btn-toggle", "children"),
        Output("seek-slider", "value"),
        Output("seek-slider", "max"),
        Output("seek-time", "children"),
        Output("seek-echo", "data"),
        Input("status-tick", "n_intervals"),
    )
    def on_tick(_):
        status = controller.status()
        icon = "⏸" if status["state"] == STATE_PLAYING else "▶"
        position = status["position"]
        total_blocks = status["total_blocks"]
        time_label = (f"{_fmt_mmss(status['position_seconds'])} / "
                      f"{_fmt_mmss(status['duration_seconds'])}")
        return (
            _status_text(status),
            icon,
            position,
            max(total_blocks - 1, 1),
            time_label,
            position,  # echo so on_seek can ignore this programmatic write
        )

    @app.callback(
        Output("spec-original", "figure"),
        Output("spec-low", "figure"),
        Output("spec-high", "figure"),
        Input("spec-tick", "n_intervals"),
    )
    def on_spec_tick(_):
        raw, lf, hf, sr = controller.recent_audio()
        return (
            _spectrogram_figure(raw, sr, "Original signal"),
            _spectrogram_figure(lf, sr, "Low band"),
            _spectrogram_figure(hf, sr, "High band"),
        )


def create_app(controller, upload_dir=None):
    # One temp dir per app instance for uploaded audio. mkdtemp gives a private
    # 0700 dir; OS temp cleanup eventually reclaims it (uploads are ephemeral).
    if upload_dir is None:
        upload_dir = tempfile.mkdtemp(prefix="audioeq-")
    app = Dash(__name__, suppress_callback_exceptions=True)
    app.title = "Audio EQ"
    app.layout = build_layout()
    register_callbacks(app, controller, upload_dir)
    return app
