"""BTC race bot: dip-buying mean reversion around a slow anchor, for the
agent-fun/bars-replay/v1 protocol (make.fun btc-race-24h).

Why this shape: on 1-minute cbBTC bars a full-size round trip costs roughly
30 bps in fees, slippage and pool impact, and fast trend signals whipsaw --
the kit's own EMA-cross baseline loses heavily on the dev window. This bot
instead trades rarely, only when price is stretched far from a slow mean,
and lets an extended move pay for the trade:

  - Anchor: an EMA of the closes with span ANCHOR_SPAN (seeded as the mean of
    its first span of closes, then the usual 2/(span+1) recursion, float64 in
    a fixed operation order, so every run answers byte for byte the same).
  - Entry: the close sits at least ENTRY_BPS below the anchor, the anchor
    itself is not in a steep fall (its 240-bar slope is above SLOPE_MIN_BPS,
    a regime guard against buying a real downtrend), and COOLDOWN_BARS have
    passed since the last exit. Stake: 95% of the smaller of cash and the
    weight room, shrunk further until the modelled impact against the latest
    close's depth is at most IMPACT_CAP_BPS.
  - Exit, in two stages: at ANCHOR + EXIT1_BPS, sell STAGE_FRACTION of the
    position; the rest goes at ANCHOR + EXIT2_BPS. A hard stop at STOP_BPS
    below the entry fill price closes the position at any time.
  - Warm-up batches (state.warming_up) only feed the anchor.
  - Every order caps its slippage at SLIP_BPS from the latest close; a refused
    order is reconsidered on the next bar while its condition still holds
    (the staged-exit state is read back from the held quantity, so a refused
    staged sell is simply sent again).

Parameters were tuned on this race's frozen dev window inside the same
mean-reversion family as the original build (anchor EMA, dip entry, staged
exits, slope guard, hard stop): the window is a downtrend day, so the anchor
span came down to 240 and the stop tightened to 80 bps. Dev grade with the
kit-faithful replay harness: net +$7.83, max drawdown 0.88%, 6 trades.

Standard library only; no clock, no environment, no files, no randomness.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"

ANCHOR_SPAN = 240       # EMA span of the anchor (4 hours of 1-minute bars)
ENTRY_BPS = 50          # buy when the close is this far below the anchor
EXIT1_BPS = 35          # first staged exit this far above the anchor
EXIT2_BPS = 105         # final exit this far above the anchor
STAGE_FRACTION = 50     # percent of the position sold at the first stage
STOP_BPS = 80           # hard stop below the entry fill price
SLOPE_BARS = 240        # anchor slope lookback for the regime guard
SLOPE_MIN_BPS = -30     # skip entries while the anchor falls faster than this
COOLDOWN_BARS = 5       # bars to sit out after an exit
IMPACT_CAP_BPS = 15     # do not pay more impact than this on an entry
SLIP_BPS = 200          # max_slippage_bps on every order


class Bot:
    def __init__(self):
        self.warm = []
        self.anchor = None
        self.anchors = []       # the anchor at every bar since it seeded
        self.entry_price = None # raw price units of the entry fill (or first close held)
        self.entry_qty = 0      # quantity the entry fill brought in
        self.last_exit_seq = -10 ** 9

    def step_anchor(self, close):
        if self.anchor is not None:
            self.anchor += (2.0 / (ANCHOR_SPAN + 1)) * (close - self.anchor)
        else:
            self.warm.append(close)
            if len(self.warm) == ANCHOR_SPAN:
                self.anchor = sum(self.warm) / ANCHOR_SPAN
        if self.anchor is not None:
            self.anchors.append(self.anchor)

    def staged_done(self, held_qty):
        """Whether the first-stage sell has filled: the held quantity is down to the unstaged rest."""
        if self.entry_qty <= 0:
            return False
        return held_qty <= self.entry_qty - self.entry_qty * STAGE_FRACTION // 100


bot = Bot()


def on_batch(state, batch):
    bar = None
    for b in batch["bars"]:
        if b["symbol"] == SYMBOL:
            bar = b
    if bar is None:
        return []
    close = int(bar["c"])
    bot.step_anchor(float(close))

    # Learn the entry's exact fill (price and quantity) from the fills this
    # batch carries; the position itself first shows in the same batch.
    for f in batch["fills"]:
        if f["symbol"] == SYMBOL and f["side"] == "buy" and f["status"] == "filled":
            bot.entry_price = int(f["price"])
            bot.entry_qty = int(f["qty"])

    if state.warming_up:
        return []

    held = state.positions.get(SYMBOL)
    min_notional = int(state.hello["fees"]["min_notional_quote_units"])

    if held is not None:
        if bot.entry_price is None:
            bot.entry_price = close
            bot.entry_qty = held.qty
        dev = (float(close) / bot.anchor - 1.0) * 10000.0 if bot.anchor else 0.0
        # First stage: sell STAGE_FRACTION of the position at EXIT1_BPS.
        if not bot.staged_done(held.qty) and dev >= EXIT1_BPS:
            q = held.qty * STAGE_FRACTION // 100
            if q > 0 and state.value(SYMBOL, q) >= min_notional:
                return [{"side": "sell", "symbol": SYMBOL, "qty": q,
                         "max_slippage_bps": SLIP_BPS}]
        # Full exit: the rest at EXIT2_BPS (or everything at EXIT1_BPS when no
        # staged sell is possible), and the hard stop at any deviation.
        exit_at = EXIT2_BPS if bot.staged_done(held.qty) else EXIT1_BPS
        stop = bot.entry_price is not None and close * 10000 <= bot.entry_price * (10000 - STOP_BPS)
        if dev >= exit_at or stop:
            bot.last_exit_seq = state.seq
            bot.entry_price = None
            bot.entry_qty = 0
            return [{"side": "sell", "symbol": SYMBOL, "qty": held.qty,
                     "max_slippage_bps": SLIP_BPS}]
        return []

    # Flat: the position is gone; forget its bookkeeping and look for an entry.
    bot.entry_price = None
    bot.entry_qty = 0
    if bot.anchor is None:
        return []
    if state.seq - bot.last_exit_seq < COOLDOWN_BARS:
        return []
    dev = (float(close) / bot.anchor - 1.0) * 10000.0
    if dev > -ENTRY_BPS:
        return []
    if len(bot.anchors) > SLOPE_BARS + 1:
        slope = (bot.anchor / bot.anchors[-SLOPE_BARS - 1] - 1.0) * 10000.0
        if slope < SLOPE_MIN_BPS:
            return []
    room = state.weight_room(SYMBOL)
    stake = state.cash if state.cash < room else room
    stake = stake * 19 // 20
    while stake > 0:
        imp = state.impact_bps(SYMBOL, stake * 9996 // 10000)
        if imp is None or imp <= IMPACT_CAP_BPS:
            break
        stake = stake * 3 // 4
    if stake < min_notional:
        return []
    return [{"side": "buy", "symbol": SYMBOL, "notional_quote_units": stake,
             "max_slippage_bps": SLIP_BPS}]


makefun_bars.run(on_batch)
