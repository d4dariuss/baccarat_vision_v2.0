"""Type-safe configuration (§3) via pydantic + YAML.

Loads ``config/default.yaml`` (and optionally a per-casino profile that
overrides it), validates it, and exposes typed accessors plus helpers to build
the runtime :class:`PayoutTable` and :class:`PredictionConfig`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

import yaml
from pydantic import BaseModel, Field

from .betting.payout_table import PayoutTable
from .engine.predictor import PredictionConfig

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "default.yaml"


class CaptureRegion(BaseModel):
    x: int = 0
    y: int = 0
    width: int = 1920
    height: int = 1080


class CaptureConfig(BaseModel):
    region: CaptureRegion = Field(default_factory=CaptureRegion)
    fps: int = 2
    display_id: int = 0


class SubRegion(BaseModel):
    x: int
    y: int
    w: int
    h: int


class UIConfig(BaseModel):
    always_on_top: bool = True
    opacity: float = 0.95
    theme: str = "dark"


class PredictionSettings(BaseModel):
    min_hands_before_predicting: int = 10
    composition_weight: float = 1.0
    road_weight: float = 0.0
    confidence_floor: float = 0.0
    confidence_ceiling: float = 1.0


class VisionSettings(BaseModel):
    ocr_backend: str = "auto"  # "auto" | "easyocr" | "pytesseract" | "null"
    # Step 9 (stretch): read individual card values for true counting. Requires
    # card_player_* / card_banker_* sub-regions in `regions:`. Off by default
    # because card OCR during the deal animation is unreliable.
    read_cards: bool = False
    # Cards burned at the start of each shoe (SpinQuest burns 10). Removed from
    # the fresh shoe so the "cards left" countdown matches the real shoe.
    burn_cards: int = 10


class AppConfig(BaseModel):
    """The full validated application config."""

    capture: CaptureConfig = Field(default_factory=CaptureConfig)
    regions: Dict[str, SubRegion] = Field(default_factory=dict)
    decks_per_shoe: int = 8
    penetration_pct: float = 75.0
    banker_rule: str = "ez_baccarat"
    payouts: Dict[str, Any] = Field(default_factory=dict)
    max_bets: Dict[str, float] = Field(default_factory=dict)
    min_bet: float = 1.0
    ui: UIConfig = Field(default_factory=UIConfig)
    prediction: PredictionSettings = Field(default_factory=PredictionSettings)
    vision: VisionSettings = Field(default_factory=VisionSettings)

    # -- derived runtime objects ------------------------------------------ #
    def payout_table(self) -> PayoutTable:
        return PayoutTable.from_config(self.payouts)

    def prediction_config(self) -> PredictionConfig:
        p = self.prediction
        return PredictionConfig(
            min_hands_before_predicting=p.min_hands_before_predicting,
            composition_weight=p.composition_weight,
            road_weight=p.road_weight,
            confidence_floor=p.confidence_floor,
            confidence_ceiling=p.confidence_ceiling,
        )


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def save_config(config: AppConfig, path: Optional[Path] = None) -> None:
    """Persist a config back to YAML (used after region calibration, §7)."""
    path = path or DEFAULT_CONFIG_PATH
    data = config.model_dump(mode="json")
    with open(path, "w") as fh:
        yaml.safe_dump(data, fh, sort_keys=False, default_flow_style=False)


def load_config(
    path: Optional[Path] = None,
    casino_profile: Optional[Path] = None,
) -> AppConfig:
    """Load and validate config, optionally layering a casino profile on top."""
    path = path or DEFAULT_CONFIG_PATH
    with open(path, "r") as fh:
        data = yaml.safe_load(fh) or {}
    if casino_profile is not None:
        with open(casino_profile, "r") as fh:
            data = _deep_merge(data, yaml.safe_load(fh) or {})
    return AppConfig.model_validate(data)
