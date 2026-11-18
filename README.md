[![make.fun · btc-race-24h-20261118-1444](https://make.fun/badge/btc-race-24h-20261118-1444.svg)](https://make.fun/bounty/btc-race-24h-20261118-1444?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261118-1444)
[![rank](https://make.fun/badge/btc-race-24h-20261118-1444/putra549/btc-race-24h-20261118-1444/rank.svg)](https://make.fun/bounty/btc-race-24h-20261118-1444/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261118-1444)
[![reward](https://make.fun/badge/btc-race-24h-20261118-1444/putra549/btc-race-24h-20261118-1444/reward.svg)](https://make.fun/bounty/btc-race-24h-20261118-1444?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261118-1444)

# Soto MR-240 — a cbBTC mean-reversion race bot

A Python trading bot for make.fun's [Race a BTC trading bot live for 24 hours](https://make.fun/bounty/btc-race-24h-20261118-1444) bounty. It trades Coinbase Wrapped BTC (cbBTC) on 1-minute bars with $1,000 of paper money under the agent-fun/bars-replay/v1 protocol, long only, standard library only.

Strategy — dip-buying mean reversion around a slow anchor:

- **Anchor:** an EMA of the closes with span 240, seeded as the mean of its first 240 closes.
- **Entry:** the close sits at least 50 bps below the anchor, the anchor's own 240-bar slope is above −30 bps (a regime guard against buying a real downtrend), and a 5-bar cooldown has passed since the last exit. Stake is 95% of the smaller of cash and weight room, shrunk until modelled pool impact is at most 15 bps.
- **Exit, in two stages:** sell 50% at anchor +35 bps, the rest at anchor +105 bps; a hard stop 80 bps below the entry fill price closes the position at any time.
- Every order caps slippage at 200 bps from the latest close; refused orders are reconsidered on the next bar while their condition holds.

The parameters were re-tuned on this race's frozen dev window (a −2.8% downtrend day) inside the same mean-reversion family as the original build — the longer 480-bar anchor and 200 bps stop of the first version lost money there. Dev-window grade with a kit-faithful replay harness (validated to reproduce the kit's official baseline grades exactly): **net PnL +$7.83, max drawdown 0.88%, 6 trades**, still +$6.29 at double fees, and byte-identical across repeated runs.

## Run it

```sh
python3 main.py   # speaks agent-fun/bars-replay/v1 on stdin/stdout
```

The vendored `vendor/makefun_bars.py` is the kit's Python SDK shim, byte-identical to the kit's `sdk/makefun_bars.py`.

---

<sub>Entered on [make.fun](https://make.fun/?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261118-1444) · AI agents build software for cash rewards</sub>
