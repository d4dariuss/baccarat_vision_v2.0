"""Application entry point: ``python -m baccarat_vision.app``.

Build-order step 3: launches the dashboard driven by manual hand entry. The
screen-capture / OCR / CV pipeline (steps 4+) is not wired in yet, so the
macOS Screen Recording permission check is a non-fatal advisory for now.
"""

from __future__ import annotations

import os
import sys

from .controller import AppController
from .settings import load_config


def check_screen_recording_permission() -> bool:
    """Best-effort macOS Screen Recording check (advisory until capture lands).

    Returns ``True`` if permission appears granted or cannot be determined
    (e.g. non-macOS or the Quartz framework isn't installed yet). Never raises.
    """
    if sys.platform != "darwin":
        return True
    try:
        from Quartz import CGPreflightScreenCaptureAccess  # type: ignore
    except Exception:
        return True  # pyobjc not installed yet (step 4 dependency)
    try:
        return bool(CGPreflightScreenCaptureAccess())
    except Exception:
        return True


def _check_python_for_gui() -> None:
    """Fail fast with guidance if the interpreter can't run the PySide6 GUI.

    PySide6 (6.11) does not support CPython 3.14 — its Qt platform plugin
    aborts at ``QApplication`` construction (SIGABRT) rather than raising a
    catchable error. We can't try/except that, so we refuse up front with a
    clear message. Set ``BACCARAT_FORCE_GUI=1`` to attempt it anyway.
    """
    if sys.version_info[:2] >= (3, 14) and not os.environ.get("BACCARAT_FORCE_GUI"):
        sys.exit(
            "\nThis GUI needs Python 3.11–3.13 — PySide6 can't initialize its Qt\n"
            f"platform plugin on Python {sys.version_info.major}.{sys.version_info.minor} "
            "(it crashes at startup).\n\n"
            "Fix:\n"
            "  python3.13 -m venv .venv313\n"
            "  .venv313/bin/pip install -e \".[ocr]\"\n"
            "  .venv313/bin/python -m baccarat_vision.app\n\n"
            "(The engine + tests run fine on 3.14; only the Qt GUI needs <=3.13.)\n"
        )


def main() -> int:
    _check_python_for_gui()
    config = load_config()

    if not check_screen_recording_permission():
        print(
            "⚠ Screen Recording permission not granted.\n"
            "  System Settings → Privacy & Security → Screen Recording → enable "
            "this app.\n"
            "  (Manual hand entry works without it; live capture (step 4+) will "
            "require it.)",
            file=sys.stderr,
        )

    # Import Qt lazily so the engine/tests don't require a display.
    from PySide6.QtWidgets import QApplication

    from .ui.main_window import MainWindow

    app = QApplication.instance() or QApplication(sys.argv)
    controller = AppController(config)
    window = MainWindow(controller)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
