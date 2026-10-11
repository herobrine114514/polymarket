from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path


def load_runtime_settings(data_dir: Path) -> dict:
    path = data_dir / "settings.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_runtime_settings(data_dir: Path, updates: dict) -> dict:
    data_dir.mkdir(parents=True, exist_ok=True)
    current = load_runtime_settings(data_dir)
    current.update(updates)
    dest = data_dir / "settings.json"
    tmp = data_dir / "settings.json.tmp"
    tmp.write_text(json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(dest)
    return current


@dataclass(frozen=True)
class Config:
    data_dir: Path = Path(os.getenv("POLYCAST_DATA_DIR", ".polycast"))
    initial_cash: float = float(os.getenv("POLYCAST_INITIAL_CASH", "1000"))
    min_edge: float = float(os.getenv("POLYCAST_MIN_EDGE", "0.02"))
    min_roi: float = float(os.getenv("POLYCAST_MIN_ROI", "0.15"))
    min_price: float = float(os.getenv("POLYCAST_MIN_PRICE", "0.15"))
    max_price: float = float(os.getenv("POLYCAST_MAX_PRICE", "0.85"))
    probability_haircut: float = float(os.getenv("POLYCAST_PROBABILITY_HAIRCUT", "0.0"))
    max_market_fraction: float = float(os.getenv("POLYCAST_MAX_MARKET_FRACTION", "0.005"))
    max_event_fraction: float = float(os.getenv("POLYCAST_MAX_EVENT_FRACTION", "0.02"))
    max_total_fraction: float = float(os.getenv("POLYCAST_MAX_TOTAL_FRACTION", "0.10"))
    max_daily_loss_fraction: float = float(os.getenv("POLYCAST_MAX_DAILY_LOSS_FRACTION", "0.02"))
    max_drawdown_fraction: float = float(os.getenv("POLYCAST_MAX_DRAWDOWN_FRACTION", "0.05"))
    min_liquidity: float = float(os.getenv("POLYCAST_MIN_LIQUIDITY", "5000"))
    quote_max_age_seconds: int = int(os.getenv("POLYCAST_QUOTE_MAX_AGE_SECONDS", "60"))
    btc5m_book_max_drift: float = float(os.getenv("POLYCAST_BTC5M_BOOK_MAX_DRIFT", "0.03"))
    btc5m_basis_max_age_seconds: int = int(os.getenv("POLYCAST_BTC5M_BASIS_MAX_AGE_SECONDS", "30"))
    evidence_cooldown_minutes: int = int(os.getenv("POLYCAST_EVIDENCE_COOLDOWN_MINUTES", "15"))
    max_analyses_per_cycle: int = int(os.getenv("POLYCAST_MAX_ANALYSES_PER_CYCLE", "2"))
    max_model_calls_per_utc_day: int = int(os.getenv("POLYCAST_MAX_MODEL_CALLS_PER_DAY", "8"))
    max_deepseek_calls_per_utc_day: int = int(os.getenv("POLYCAST_MAX_DEEPSEEK_CALLS_PER_DAY", "24"))
    analysis_cooldown_hours: int = int(os.getenv("POLYCAST_ANALYSIS_COOLDOWN_HOURS", "12"))

    @property
    def database(self) -> Path:
        return self.data_dir / "polycast.sqlite3"

    def five_minute_auto_enabled(self) -> bool:
        return load_runtime_settings(self.data_dir).get("btc5m_auto_enabled", True) is True

    def model_call_limit(self, provider: str) -> int:
        settings = load_runtime_settings(self.data_dir)
        if provider == "deepseek":
            val = settings.get("max_deepseek_calls_per_utc_day", settings.get("deepseek_daily_limit"))
            if val is not None:
                try:
                    val_int = int(val)
                    if val_int >= 1:
                        return val_int
                except (ValueError, TypeError):
                    pass
            return self.max_deepseek_calls_per_utc_day
        val = settings.get("max_model_calls_per_utc_day", settings.get("codex_daily_limit"))
        if val is not None:
            try:
                val_int = int(val)
                if val_int >= 1:
                    return val_int
            except (ValueError, TypeError):
                pass
        return self.max_model_calls_per_utc_day

    def validate(self) -> None:
        for name in ("initial_cash", "min_edge", "min_roi", "min_price", "max_price",
                     "probability_haircut", "max_market_fraction", "max_event_fraction",
                     "max_total_fraction", "max_daily_loss_fraction",
                     "max_drawdown_fraction", "min_liquidity", "btc5m_book_max_drift"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{name} must be a finite number")
        if self.initial_cash <= 0:
            raise ValueError("POLYCAST_INITIAL_CASH must be positive")
        for value in (self.min_edge, self.probability_haircut, self.max_market_fraction,
                      self.max_event_fraction, self.max_total_fraction,
                      self.max_daily_loss_fraction, self.max_drawdown_fraction):
            if not 0 <= value <= 1:
                raise ValueError("probabilities and risk fractions must be in [0, 1]")
        if self.min_roi < 0 or not 0 <= self.min_price <= self.max_price <= 1:
            raise ValueError("invalid min_roi or price band limits")
        if not 0 <= self.btc5m_book_max_drift <= 1 or type(self.btc5m_basis_max_age_seconds) is not int or self.btc5m_basis_max_age_seconds < 1:
            raise ValueError("invalid BTC5m forecast basis limits")
        if not 0 < self.max_market_fraction <= self.max_event_fraction <= self.max_total_fraction <= 1:
            raise ValueError("risk limits must satisfy market <= event <= total")
        if not 0 < self.max_daily_loss_fraction < 1 or not 0 < self.max_drawdown_fraction < 1:
            raise ValueError("loss and drawdown stops must be positive and below 1")
        if (self.max_analyses_per_cycle < 0 or self.max_model_calls_per_utc_day < 1 or
                self.max_deepseek_calls_per_utc_day < 1 or
                self.quote_max_age_seconds < 1 or self.evidence_cooldown_minutes < 0 or
                self.analysis_cooldown_hours < 1 or
                self.min_liquidity < 0):
            raise ValueError("invalid analysis or quote limits")

