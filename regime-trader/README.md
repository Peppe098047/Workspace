# regime-trader

Bot di trading algoritmico basato su Hidden Markov Model per il rilevamento del regime di mercato.

## Architettura

```
regime-trader/
├── core/          # HMM engine, strategie, risk manager, signal generator
├── broker/        # Integrazione Alpaca (ordini, posizioni, dati)
├── data/          # Market data feed e feature engineering
├── monitoring/    # Dashboard terminale, logging, alert
├── backtest/      # Walk-forward backtester, metriche, stress test
└── tests/         # Test suite
```

## Setup

```bash
# 1. Crea ambiente virtuale
python -m venv venv && source venv/bin/activate

# 2. Installa dipendenze
pip install -r requirements.txt

# 3. Configura le credenziali
cp .env.example .env
# Inserisci ALPACA_API_KEY e ALPACA_SECRET_KEY in .env

# 4. (Opzionale) Modifica i parametri
# Edita config/settings.yaml
```

## Utilizzo

```bash
# Paper trading (default)
python main.py trade

# Backtest walk-forward
python main.py backtest

# Stress test
python main.py stress
```

## Configurazione

Tutti i parametri si trovano in `config/settings.yaml`, organizzati in sezioni:
- `broker` — simboli, timeframe, paper/live
- `hmm` — iperparametri del modello
- `strategy` — allocazioni per regime
- `risk` — limiti di esposizione e drawdown
- `backtest` — parametri walk-forward
- `monitoring` — dashboard e alert
