"""Pluggable OCR backends (§6.1).

The vision pipeline depends only on the :class:`OcrBackend` protocol
(``read_text(image) -> str``), so the heavy EasyOCR/torch stack is never a hard
requirement. ``easyocr`` is preferred on M1 (GPU); ``pytesseract`` is the
fallback; :class:`NullBackend` and :class:`CallableBackend` let the parsing
logic be unit-tested with no OCR engine installed at all.
"""

from __future__ import annotations

from typing import Callable, Optional, Protocol

import numpy as np


class OcrBackend(Protocol):
    def read_text(self, image: np.ndarray) -> str:  # pragma: no cover - protocol
        """Return the recognised text for a BGR image (best effort)."""
        ...


class NullBackend:
    """No OCR available — always returns empty text."""

    def read_text(self, image: np.ndarray) -> str:
        return ""


class CallableBackend:
    """Wrap a plain function as a backend (used in tests / replay)."""

    def __init__(self, fn: Callable[[np.ndarray], str]) -> None:
        self._fn = fn

    def read_text(self, image: np.ndarray) -> str:
        return self._fn(image)


class EasyOcrBackend:
    """EasyOCR backend (lazy-imported; preferred on Apple Silicon)."""

    def __init__(self, languages: Optional[list[str]] = None, gpu: bool = True) -> None:
        import easyocr  # noqa: F401

        self._reader = easyocr.Reader(languages or ["en"], gpu=gpu)

    def read_text(self, image: np.ndarray) -> str:
        results = self._reader.readtext(image, detail=0, paragraph=True)
        return " ".join(results)


class PytesseractBackend:
    """pytesseract backend (lazy-imported fallback)."""

    def __init__(self, config: str = "") -> None:
        import pytesseract  # noqa: F401

        self._pt = pytesseract
        self._config = config

    def read_text(self, image: np.ndarray) -> str:
        return self._pt.image_to_string(image, config=self._config)


def get_ocr_backend(name: str = "auto", **kwargs) -> OcrBackend:
    """Construct an OCR backend by name.

    ``auto`` tries EasyOCR, then pytesseract, then falls back to a NullBackend
    so the app still launches (manual entry remains available).
    """
    name = name.lower()
    if name == "easyocr":
        return EasyOcrBackend(**kwargs)
    if name == "pytesseract":
        return PytesseractBackend(**kwargs)
    if name == "null":
        return NullBackend()
    if name == "auto":
        for ctor in (EasyOcrBackend, PytesseractBackend):
            try:
                return ctor()  # type: ignore[call-arg]
            except Exception:
                continue
        return NullBackend()
    raise ValueError(f"unknown OCR backend: {name!r}")
