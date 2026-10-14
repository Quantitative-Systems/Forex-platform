"""MT5 broker adapter — MetaTrader 5 live-routing gateway (law 7 gated).

The adapter wraps the official `MetaTrader5` Python SDK so the engine can
route orders to any MT5-compatible broker (IC Markets, Pepperstone, FTMO,
Eightcap, etc.) through one stable interface.

SAFETY: this adapter performs NO real trading until explicitly armed with
`allow_live=True`. The engine's capital_ladder (Law 7) requires human
confirmation before `allow_live` can ever be flipped. Unit tests and paper
runs must exercise `simulate_only` paths.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

try:  # MT5 SDK is optional at import time; tests use the simulated path
    import MetaTrader5 as _mt5
except Exception:  # pragma: no cover - environment dependent
    _mt5 = None


@dataclass
class MT5Config:
    login: int = 0
    server: str = ""
    password: str = ""
    allow_live: bool = False              # Law 7 gate — NEVER auto-true
    magic: int = 100888
    deviation_points: int = 20
    comment: str = "FRACTAL_ENGINE"


class MT5BrokerAdapter:
    def __init__(self, config: Optional[MT5Config] = None) -> None:
        self.config = config or MT5Config()
        self._available = _mt5 is not None
        self._initialized = False

    # -- lifecycle --------------------------------------------------------------
    def initialize(self) -> bool:
        if not self._available:
            return False
        if not self.config.login or not self.config.server:
            return False
        ok = _mt5.initialize(
            login=self.config.login,
            server=self.config.server,
            password=self.config.password,
        )
        self._initialized = bool(ok)
        return self._initialized

    def shutdown(self) -> None:
        if self._available and self._initialized:
            _mt5.shutdown()
        self._initialized = False

    # -- quote / contract utilities ------------------------------------------------
    def tick(self, symbol: str) -> Optional[dict]:
        if not (self._available and self._initialized):
            return None
        t = _mt5.symbol_info_tick(symbol)
        if t is None:
            return None
        return {"bid": Decimal(str(t.bid)), "ask": Decimal(str(t.ask)), "time": t.time}

    def spread_points(self, symbol: str) -> Optional[int]:
        if not (self._available and self._initialized):
            return None
        info = _mt5.symbol_info(symbol)
        return int(getattr(info, "spread", 0)) if info is not None else None

    # -- order routing (real order send — gated) --------------------------------------
    def create_order(
        self,
        symbol: str,
        side: str,
        volume_lots: Decimal | float | str,
        stop_loss: Optional[Decimal] = None,
        take_profit: Optional[Decimal] = None,
    ) -> dict:
        if not self.config.allow_live:
            return {"ok": False, "reason": "LIVE_GATE_OPEN_SET_ALLOW_LIVE_FIRST", "simulated": True}

        if not (self._available and self._initialized):
            return {"ok": False, "reason": "MT5_NOT_INITIALIZED", "simulated": True}

        side = side.upper()
        order_type = _mt5.ORDER_TYPE_BUY if side == "BUY" else _mt5.ORDER_TYPE_SELL
        quote = self.tick(symbol)
        if quote is None:
            return {"ok": False, "reason": "NO_QUOTE", "simulated": True}

        price = quote["ask"] if side == "BUY" else quote["bid"]
        request = {
            "action": _mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(Decimal(str(volume_lots))),
            "type": order_type,
            "price": float(price),
            "sl": float(stop_loss) if stop_loss is not None else 0.0,
            "tp": float(take_profit) if take_profit is not None else 0.0,
            "deviation": self.config.deviation_points,
            "magic": self.config.magic,
            "comment": self.config.comment,
            "type_time": _mt5.ORDER_TIME_GTC,
            "type_filling": _mt5.ORDER_FILLING_IOC,
        }
        result = _mt5.order_send(request)
        ok = result is not None and result.retcode == _mt5.TRADE_RETCODE_DONE
        return {
            "ok": ok,
            "retcode": getattr(result, "retcode", None),
            "order": getattr(result, "order", None),
            "simulated": False,
        }
