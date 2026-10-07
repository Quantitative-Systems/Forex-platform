"""
Phase Riding Strategy - Ensures asymmetry by enforcing minimum win rate and balanced trade outcomes.

This strategy focuses on capturing strong asymmetric moves by:
1. Requiring a minimum win rate (>= 4.0R) to filter asymmetric outcomes
2. Balancing entry/exit risks to avoid asymmetry leakage
3. Using HTF/MTF/LTF structural alignment for trade confirmation
4. Enforcing structural take-profit at HTF destinations
5. Pre-trade R:R gate: reject any trade with expected R:R < 4.0
6. Trailing stop engages only after +2.0R favorable excursion
7. Trade taken ONLY when MTF (1H) produces confirmed structural shift out of HTF (4H) Key Zone
8. Discount for Longs, Premium for Shorts
"""

from datetime import datetime
from decimal import Decimal
from typing import Optional

from forex_platform.core.domain import CurrencyPair, LotSize
from forex_platform.market_data.causal_aligner import CausalAligner, Timeframe
from forex_platform.market_model.structure.swings import detect_swings, SwingPoint, SwingType, SwingScope
from forex_platform.market_model.structure.breaks import detect_structure_breaks, StructureBreak, BreakType
from forex_platform.market_model.structure.strength import mark_protected_weak_swings
from forex_platform.market_model.phases import MarketPhaseClassifier
from forex_platform.market_model.zones import detect_order_blocks, liquidity_pools_to_zones, extract_levels, MarketZone, ZoneScope, ZoneType
from forex_platform.core.sessions import ForexSession
from forex_platform.core.domain import OrderSide


class TradeState(Enum):
    WAITING = "WAITING"
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"


class Position:
    model_config = ConfigDict(frozen=True)

    index: int
    timestamp: Optional[str]
    pair: CurrencyPair
    direction: OrderSide
    entry_price: Decimal
    lots: Decimal
    stop_loss: Decimal
    take_profit: Decimal
    trail_stop: Decimal
    max_drawdown: Decimal
    break_even_price: Decimal
    trade_id: str
    max_favorable_excursion: Decimal = Decimal("0")
