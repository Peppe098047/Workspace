#!/usr/bin/env bash
#
# start.sh — avvio del regime-trader con controlli di pre-volo.
#
# Uso:
#   ./start.sh            menu interattivo
#   ./start.sh train      addestra l'HMM ed esce
#   ./start.sh dry        pipeline completa senza ordini (dry-run)
#   ./start.sh live       trading paper (attende l'apertura del mercato)
#   ./start.sh dashboard  dashboard live nel terminale
#   ./start.sh web        dashboard nel browser (http://localhost:8787)
#
set -euo pipefail

# Posizionati nella directory dello script (root del progetto)
cd "$(dirname "$0")"

PY="venv/bin/python"
MODEL="models/hmm_model.pkl"
MODEL_MAX_AGE_DAYS=7

# Colori
RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; CYAN=$'\033[36m'; RESET=$'\033[0m'

info()  { echo "${CYAN}▶${RESET} $*"; }
ok()    { echo "${GREEN}✅${RESET} $*"; }
warn()  { echo "${YELLOW}⚠️ ${RESET} $*"; }
err()   { echo "${RED}🚨${RESET} $*" >&2; }

# ──────────────────────────────────────────────────────────────────────────────
# CONTROLLI DI PRE-VOLO
# ──────────────────────────────────────────────────────────────────────────────

preflight() {
    # 1. Interprete venv
    if [[ ! -x "$PY" ]]; then
        err "Interprete venv non trovato in '$PY'. Crea il venv e installa le dipendenze."
        exit 1
    fi
    ok "venv presente"

    # 2. File .env con credenziali
    if [[ ! -f ".env" ]]; then
        err "File .env mancante. Serve per connettersi ad Alpaca."
        echo "   Crea .env con:"
        echo "     ALPACA_API_KEY=..."
        echo "     ALPACA_SECRET_KEY=..."
        echo "     ALPACA_PAPER=true"
        exit 1
    fi
    if ! grep -q "ALPACA_API_KEY" .env || ! grep -q "ALPACA_SECRET_KEY" .env; then
        err "Chiavi ALPACA mancanti nel file .env."
        exit 1
    fi
    ok ".env con credenziali presente"

    # 3. Avviso modalità live
    if grep -qiE "^ALPACA_PAPER\s*=\s*(false|0|no)" .env; then
        warn "ALPACA_PAPER è impostato su LIVE — denaro reale!"
    else
        ok "Modalità PAPER (denaro virtuale)"
    fi
}

# Verifica età del modello HMM; ritorna 0 se valido, 1 se manca/vecchio
model_is_fresh() {
    [[ -f "$MODEL" ]] || return 1
    # File modificato negli ultimi MODEL_MAX_AGE_DAYS giorni?
    if [[ -n "$(find "$MODEL" -mtime "-${MODEL_MAX_AGE_DAYS}" 2>/dev/null)" ]]; then
        return 0
    fi
    return 1
}

ensure_model() {
    if model_is_fresh; then
        local age
        age=$(( ( $(date +%s) - $(stat -c %Y "$MODEL") ) / 86400 ))
        ok "Modello HMM presente (età: ${age}g)"
    else
        if [[ -f "$MODEL" ]]; then
            warn "Modello HMM più vecchio di ${MODEL_MAX_AGE_DAYS}g — riaddestramento…"
        else
            info "Nessun modello HMM — addestramento iniziale…"
        fi
        "$PY" main.py trade --train-only
        ok "Modello HMM pronto"
    fi
}

# ──────────────────────────────────────────────────────────────────────────────
# AZIONI
# ──────────────────────────────────────────────────────────────────────────────

do_train() {
    preflight
    info "Addestramento HMM…"
    "$PY" main.py trade --train-only
}

do_dry() {
    preflight
    ensure_model
    info "Avvio DRY-RUN (nessun ordine inviato). Ctrl+C per fermare."
    "$PY" main.py trade --dry-run
}

do_live() {
    preflight
    ensure_model
    info "Avvio TRADING paper. Attende l'apertura del mercato se chiuso. Ctrl+C per fermare."
    "$PY" main.py trade --wait-for-open
}

do_dashboard() {
    "$PY" main.py dashboard
}

do_web() {
    info "Dashboard browser su http://localhost:8787 — Ctrl+C per fermare."
    "$PY" main.py dashboard --web
}

menu() {
    echo ""
    echo "${CYAN}═══════════════════════════════════════════${RESET}"
    echo "  REGIME TRADER — menu di avvio"
    echo "${CYAN}═══════════════════════════════════════════${RESET}"
    echo "  1) Addestra HMM          (train-only)"
    echo "  2) Dry-run               (pipeline senza ordini)"
    echo "  3) Trading paper         (--wait-for-open)"
    echo "  4) Dashboard terminale   (live nel terminale)"
    echo "  5) Dashboard browser     (http://localhost:8787)"
    echo "  0) Esci"
    echo ""
    read -rp "Scelta: " choice
    case "$choice" in
        1) do_train ;;
        2) do_dry ;;
        3) do_live ;;
        4) do_dashboard ;;
        5) do_web ;;
        0) exit 0 ;;
        *) err "Scelta non valida"; exit 1 ;;
    esac
}

# ──────────────────────────────────────────────────────────────────────────────
# DISPATCH
# ──────────────────────────────────────────────────────────────────────────────

case "${1:-menu}" in
    train)     do_train ;;
    dry)       do_dry ;;
    live)      do_live ;;
    dashboard) do_dashboard ;;
    web)       do_web ;;
    menu)      menu ;;
    *)
        err "Argomento sconosciuto: $1"
        echo "Uso: ./start.sh [train|dry|live|dashboard|web]"
        exit 1
        ;;
esac
