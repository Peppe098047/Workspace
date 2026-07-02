"""
set_stops_now.py — piazza uno stop loss di protezione su ogni posizione aperta.

Da usare una tantum per proteggere posizioni aperte senza stop (es. dopo il bug
in cui gli stop non venivano registrati sul broker).

Stop calcolato come EMA50 − ATR14 (coerente con le strategie high-vol), con un
tetto di sicurezza: mai sopra il 3% sotto il prezzo corrente (evita scatto immediato).

Uso:
    venv/bin/python scripts/set_stops_now.py            # esegue
    venv/bin/python scripts/set_stops_now.py --dry-run  # mostra soltanto
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

import logging
logging.disable(logging.WARNING)

from broker.alpaca_client import AlpacaClient
from broker.order_executor import OrderExecutor
from data.market_data import MarketDataFeed
from core.regime_strategies import _atr, _ema


def main() -> None:
    dry = "--dry-run" in sys.argv

    client   = AlpacaClient.from_env()
    feed     = MarketDataFeed(client, "1Day")
    executor = OrderExecutor(client)

    positions = client.get_positions()
    if not positions:
        print("Nessuna posizione aperta.")
        return

    # Stop già presenti, per non duplicare
    existing_stops = {
        o["symbol"] for o in client.get_open_orders()
        if o["type"] in ("stop", "stop_limit")
    }

    symbols = [p["symbol"] for p in positions]
    bars = feed.load_history(symbols, lookback_bars=120)

    print(f"\n{'SIMBOLO':<8} {'PREZZO':>10} {'STOP':>10} {'DISTANZA':>10}  AZIONE")
    print("─" * 60)

    for pos in positions:
        sym   = pos["symbol"]
        qty   = int(float(pos["qty"]))
        price = float(pos["current_price"])

        if sym in existing_stops:
            print(f"{sym:<8} {price:>10.2f} {'—':>10} {'—':>10}  già protetto")
            continue

        df = bars.get(sym)
        if df is None or len(df) < 60:
            # Fallback: stop a -8% dal prezzo corrente
            stop = round(price * 0.92, 2)
        else:
            ema  = _ema(df["close"])
            atr  = _atr(df)
            stop = ema - 1.0 * atr
            # Tetto di sicurezza: lo stop deve stare sotto il prezzo corrente
            stop = min(stop, price * 0.97)
            stop = round(stop, 2)

        dist_pct = (price - stop) / price
        action = "DRY-RUN" if dry else "piazzato"

        if not dry:
            try:
                executor.place_protective_stop(symbol=sym, shares=qty, stop_price=stop)
            except Exception as exc:
                action = f"ERRORE: {exc}"

        print(f"{sym:<8} {price:>10.2f} {stop:>10.2f} {dist_pct:>9.1%}  {action}")

    print("─" * 60)
    if dry:
        print("DRY-RUN: nessuno stop piazzato. Rilancia senza --dry-run per eseguire.")
    else:
        print("✅ Stop di protezione piazzati. Verifica su Alpaca o con la dashboard.")


if __name__ == "__main__":
    main()
