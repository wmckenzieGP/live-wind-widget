"""Always-on-top desktop launcher for the Live Wind Widget.

Opens the deployed app in a small borderless-ish window pinned above other
windows, so it can sit in a screen corner while you work. The .pyw extension
means Windows launches it without a console window.

    pip install -r widget-requirements.txt
    python widget.pyw          (or just double-click it)

This is a local convenience wrapper. It loads the same URL as a browser would,
so the Streamlit websocket and the 3 s live refresh behave identically -- an
always-on-top window is never occluded, so its timers are never throttled.
"""
import os
import sys

import webview

URL = os.environ.get("WIND_WIDGET_URL", "https://blackfoilswindwidget.streamlit.app/")

WIDTH, HEIGHT = 480, 470
MIN_SIZE = (360, 380)

# Frameless drops the title bar for a cleaner look, but then there is no close
# button and the window is dragged by its body. Off by default so the window
# stays easy to move, minimise and close; pass --frameless to try it.
FRAMELESS = "--frameless" in sys.argv


def main() -> None:
    webview.create_window(
        "Course Wind",
        URL,
        width=WIDTH,
        height=HEIGHT,
        min_size=MIN_SIZE,
        on_top=True,
        frameless=FRAMELESS,
        easy_drag=FRAMELESS,   # only meaningful without a title bar
    )
    # Streamlit Cloud sleeps idle apps; the first load after a quiet spell can
    # take ~30 s to wake, so the window may sit blank briefly on startup.
    webview.start()


if __name__ == "__main__":
    main()
