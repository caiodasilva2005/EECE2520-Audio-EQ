"""Dash UI for the audio EQ. The layout is intentionally minimal: a file
picker, a cutoff slider that updates while audio is playing, and a few
transport buttons. Callbacks delegate everything to PlaybackController so the
UI code stays free of audio/threading logic.
"""

from pathlib import Path

import numpy as np
from scipy import signal as sig
import plotly.graph_objects as go
from dash import Dash, html, dcc, Input, Output, State, no_update, ctx

from controller import (
    PlaybackController,
    STATE_IDLE, STATE_PLAYING, STATE_PAUSED, STATE_DONE, STATE_ERROR,
)

# Spectrogram window. nperseg trades frequency vs time resolution; 1024 with
# 75% overlap is a reasonable default for music-band signals.
SPEC_NPERSEG = 1024
SPEC_NOVERLAP = 768
SPEC_DB_FLOOR = -80.0  # clamp dB so the colorbar isn't dominated by silence


AUDIO_EXTS = {".wav", ".flac", ".ogg", ".mp3", ".aiff", ".aif"}

CUTOFF_MIN = 50
CUTOFF_MAX = 8000
CUTOFF_DEFAULT = 1000
ORDER_MIN = 1
ORDER_MAX = 10
ORDER_DEFAULT = 10


def scan_samples(samples_dir):
    p = Path(samples_dir)
    if not p.is_dir():
        return []
    return sorted(
        str(f) for f in p.iterdir()
        if f.is_file() and f.suffix.lower() in AUDIO_EXTS
    )


def _file_options(samples_dir):
    return [{"label": Path(f).name, "value": f} for f in scan_samples(samples_dir)]


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


def build_layout(samples_dir):
    return html.Div(
        style={"maxWidth": "780px", "margin": "32px auto", "fontFamily": "system-ui, sans-serif"},
        children=[
            html.H2("Audio EQ"),

            html.Div([
                html.Label("Audio file"),
                dcc.Dropdown(
                    id="file-dropdown",
                    options=_file_options(samples_dir),
                    placeholder="Pick a sample…",
                    clearable=False,
                ),
            ], style={"marginBottom": "16px"}),

            html.Div([
                html.Button("Play", id="btn-play", n_clicks=0,
                            style={"marginRight": "8px"}),
                html.Button("Pause", id="btn-pause", n_clicks=0,
                            style={"marginRight": "8px"}),
                html.Button("Resume", id="btn-resume", n_clicks=0),
            ], style={"marginBottom": "20px"}),

            html.Div([
                html.Label(id="cutoff-label", children=f"Cutoff: {CUTOFF_DEFAULT} Hz"),
                dcc.Slider(
                    id="cutoff-slider",
                    min=CUTOFF_MIN, max=CUTOFF_MAX, step=10,
                    value=CUTOFF_DEFAULT,
                    marks={
                        CUTOFF_MIN: f"{CUTOFF_MIN}",
                        500: "500",
                        1000: "1k",
                        2000: "2k",
                        4000: "4k",
                        CUTOFF_MAX: f"{CUTOFF_MAX}",
                    },
                    updatemode="drag",
                    tooltip={"placement": "bottom", "always_visible": False},
                ),
            ], style={"marginBottom": "20px"}),

            html.Div([
                html.Label(id="order-label", children=f"Filter order: {ORDER_DEFAULT}"),
                dcc.Slider(
                    id="order-slider",
                    min=ORDER_MIN, max=ORDER_MAX, step=1,
                    value=ORDER_DEFAULT,
                    marks={i: str(i) for i in range(ORDER_MIN, ORDER_MAX + 1)},
                    updatemode="mouseup",
                ),
                html.Small(
                    "Order only updates between songs — changing it mid-play "
                    "would resize the filter state and could glitch the audio thread.",
                    style={"color": "#666"},
                ),
            ], style={"marginBottom": "20px"}),

            html.Pre(id="status-display",
                    style={"background": "#f5f5f5", "padding": "12px",
                           "borderRadius": "6px", "whiteSpace": "pre-wrap"}),

            html.H3("Spectrograms", style={"marginTop": "24px"}),
            dcc.Graph(id="spec-original", figure=_empty_spectrogram("Original signal")),
            dcc.Graph(id="spec-low", figure=_empty_spectrogram("Low band")),
            dcc.Graph(id="spec-high", figure=_empty_spectrogram("High band")),

            dcc.Interval(id="status-tick", interval=500, n_intervals=0),
            # Spectrograms recompute on a slower tick than the status line
            # because the FFT + plotly redraw is more expensive.
            dcc.Interval(id="spec-tick", interval=1000, n_intervals=0),
        ],
    )


def register_callbacks(app, controller, samples_dir):

    # File pick → load. Loading is synchronous and cheap (opens the file,
    # builds the SOS); it doesn't start playback.
    @app.callback(
        Output("status-display", "children", allow_duplicate=True),
        Input("file-dropdown", "value"),
        prevent_initial_call=True,
    )
    def on_file_change(filepath):
        if not filepath:
            return no_update
        controller.load(filepath)
        return _status_text(controller.status())

    # Transport buttons share a single callback so we know which one fired.
    @app.callback(
        Output("status-display", "children", allow_duplicate=True),
        Input("btn-play", "n_clicks"),
        Input("btn-pause", "n_clicks"),
        Input("btn-resume", "n_clicks"),
        prevent_initial_call=True,
    )
    def on_transport(_p, _ps, _r):
        triggered = ctx.triggered_id
        if triggered == "btn-play":
            controller.start()
        elif triggered == "btn-pause":
            controller.pause()
        elif triggered == "btn-resume":
            controller.resume()
        return _status_text(controller.status())

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

    @app.callback(
        Output("order-label", "children"),
        Input("order-slider", "value"),
    )
    def on_order(value):
        controller.set_order(value)
        return f"Filter order: {value}"

    # Background poll so the status line reflects state changes (e.g. the
    # worker thread hitting end-of-file) that aren't triggered by user input.
    @app.callback(
        Output("status-display", "children"),
        Input("status-tick", "n_intervals"),
    )
    def on_tick(_):
        return _status_text(controller.status())

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


def create_app(controller, samples_dir):
    app = Dash(__name__, suppress_callback_exceptions=True)
    app.title = "Audio EQ"
    app.layout = build_layout(samples_dir)
    register_callbacks(app, controller, samples_dir)
    return app
