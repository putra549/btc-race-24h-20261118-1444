"""cbBTC mean-reversion bot for the agent-fun/bars-replay/v1 protocol.

Strategy
--------
The bot trades a single symbol, CBBTC, against a slow exponential
moving average (the "anchor") of bar closes.

* Anchor: an EMA with span 150.  The first 150 closes are collected
  and their mean seeds the anchor; from then on the anchor follows
  the usual EMA recurrence on every bar.  One anchor value per bar
  is kept in a history list once the anchor exists.

* Entry (mean reversion): while flat, the bot buys only when the
  close sits far enough *below* the anchor -- a deviation of at
  least 80 bps -- on the theory that an overshoot to the downside
  tends to snap back toward the anchor.  Two guards can veto an
  entry: a short cooldown after the previous exit, and a slope
  guard that refuses to buy into a persistently falling anchor
  (anchor down more than 30 bps versus its value 240 bars earlier
  in history terms).  Position size is the smaller of cash and the
  remaining weight room, trimmed to 95%, then shrunk geometrically
  until the modelled price impact is at most 15 bps.

* Exits (staged): while holding, exits are staged so profits are
  banked in pieces.  Once the deviation reaches +50 bps, half of
  the position is sold (first stage).  The remainder is sold in
  full once the deviation reaches +105 bps.  If the first stage
  has not happened yet, +50 bps already triggers a full exit.

* Hard stop: regardless of the deviation, the whole position is
  sold when the close falls 80 bps or more below the entry price,
  capping the loss on a reversion that never comes.

All decisions are deterministic: no clock, no environment, no
files and no randomness are used, so repeated runs are identical.
"""

import os
import sys

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"),
)

import makefun_bars

# --- Parameters -------------------------------------------------------

SYMBOL = "CBBTC"

SPAN = 150                # EMA span for the anchor, in bars
ENTRY_BPS = 80            # buy when deviation <= -ENTRY_BPS
STAGE_EXIT_BPS = 50       # first-stage / unstaged full-exit deviation
FULL_EXIT_BPS = 105       # full-exit deviation after the first stage
STAGE_PCT = 50            # percent of the position sold in stage one
STOP_BPS = 80             # hard stop below the entry price
SLOPE_LOOKBACK = 240      # bars of anchor history used for the slope
SLOPE_MIN_BPS = -30       # veto entries when slope < this
COOLDOWN_BARS = 5         # batches to wait after a full exit
IMPACT_CAP_BPS = 15       # maximum acceptable modelled price impact
SLIPPAGE_BPS = 200        # max_slippage_bps attached to every order

_ALPHA = 2.0 / (SPAN + 1)

# --- Mutable strategy state -------------------------------------------
# Kept at module level because on_batch is called once per batch for
# the whole run and there is exactly one traded symbol.

_seed_closes = []         # closes collected before the anchor exists
_anchor = None            # current EMA value, or None while seeding
_anchor_history = []      # one anchor value per bar, once seeded
_entry_price = None       # fill price of the current position
_entry_qty = 0            # original filled quantity of the position
_last_exit_seq = -(10 ** 9)


def _staged_done(held_qty):
    """True once at least the staged fraction has been sold.

    The staged sale removes STAGE_PCT percent of the original
    entry quantity, so the stage counts as done when what remains
    is no larger than the entry quantity minus that fraction.
    """
    return _entry_qty > 0 and held_qty <= _entry_qty - _entry_qty * STAGE_PCT // 100


def _sell_order(qty):
    return {
        "side": "sell",
        "symbol": SYMBOL,
        "qty": qty,
        "max_slippage_bps": SLIPPAGE_BPS,
    }


def _step_anchor(close):
    """Advance the anchor with one close and record its history."""
    global _anchor
    price = float(close)
    if _anchor is None:
        _seed_closes.append(price)
        if len(_seed_closes) >= SPAN:
            _anchor = sum(_seed_closes) / len(_seed_closes)
            _anchor_history.append(_anchor)
        return
    _anchor = _anchor + _ALPHA * (price - _anchor)
    _anchor_history.append(_anchor)


def _deviation_bps(close):
    """Deviation of the close from the anchor, in basis points."""
    if _anchor is None:
        return 0.0
    return (float(close) / _anchor - 1.0) * 10000.0


def on_batch(state, batch):
    global _entry_price, _entry_qty, _last_exit_seq

    # Locate this batch's bar for the traded symbol.
    bar = None
    for candidate in batch["bars"]:
        if candidate["symbol"] == SYMBOL:
            bar = candidate
            break
    if bar is None:
        return []

    close = int(bar["c"])

    # The anchor moves first, so every decision below uses the
    # anchor value that already includes this bar's close.
    _step_anchor(close)

    # Learn the actual entry from any filled buy reported here;
    # the last such fill in the batch wins.  Tolerate a batch
    # without a fills list so a malformed frame cannot crash the
    # bot mid-run; it simply teaches nothing about the entry.
    for fill in batch.get("fills", []):
        if (
            fill["symbol"] == SYMBOL
            and fill["side"] == "buy"
            and fill["status"] == "filled"
        ):
            _entry_price = int(fill["price"])
            _entry_qty = int(fill["qty"])

    if state.warming_up:
        return []

    min_notional = int(state.hello["fees"]["min_notional_quote_units"])
    held = state.positions.get(SYMBOL)

    # --- Holding: manage staged exits and the hard stop. -------------
    if held is not None:
        if _entry_price is None:
            # A position without a recorded fill (for example one
            # carried into the run): anchor the stop to this close.
            _entry_price = close
            _entry_qty = held.qty

        dev = _deviation_bps(close)

        # First stage: bank half once the snap-back has begun.
        if not _staged_done(held.qty) and dev >= STAGE_EXIT_BPS:
            q = held.qty * STAGE_PCT // 100
            if q > 0 and state.value(SYMBOL, q) >= min_notional:
                return [_sell_order(q)]

        # Full exit: a higher target once staged, otherwise the
        # same level as the first stage; the stop always applies.
        exit_at = FULL_EXIT_BPS if _staged_done(held.qty) else STAGE_EXIT_BPS
        stop = (
            _entry_price is not None
            and close * 10000 <= _entry_price * (10000 - STOP_BPS)
        )
        if dev >= exit_at or stop:
            _last_exit_seq = state.seq
            _entry_price = None
            _entry_qty = 0
            return [_sell_order(held.qty)]

        return []

    # --- Flat: forget any stale entry and look for a new one. --------
    _entry_price = None
    _entry_qty = 0

    if _anchor is None:
        return []

    if state.seq - _last_exit_seq < COOLDOWN_BARS:
        return []

    dev = _deviation_bps(close)
    if dev > -ENTRY_BPS:
        return []

    # Slope guard: do not buy while the anchor itself is falling
    # steeply -- that is a trend, not a temporary dislocation.
    # History index: the current anchor sits at [-1], so the value
    # SLOPE_LOOKBACK bars before it sits at -(SLOPE_LOOKBACK + 2)
    # counting the seeded entry, and it only exists once the
    # history holds more than SLOPE_LOOKBACK + 1 entries.
    if len(_anchor_history) > SLOPE_LOOKBACK + 1:
        slope = (_anchor / _anchor_history[-(SLOPE_LOOKBACK + 2)] - 1.0) * 10000.0
        if slope < SLOPE_MIN_BPS:
            return []

    # Sizing: respect both cash and the weight cap, keep a small
    # cash buffer, then shrink until the impact is acceptable.
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

    return [
        {
            "side": "buy",
            "symbol": SYMBOL,
            "notional_quote_units": stake,
            "max_slippage_bps": SLIPPAGE_BPS,
        }
    ]


makefun_bars.run(on_batch)
