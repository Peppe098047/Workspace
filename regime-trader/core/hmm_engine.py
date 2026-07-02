"""
HMM Engine — classificatore di volatilità di mercato tramite Gaussian HMM.

FILOSOFIA: il modello classifica l'ambiente di volatilità (calmo / moderato /
turbolento). Non predice la direzione del prezzo. Il layer strategico usa
questa classificazione per calibrare l'allocazione del portafoglio.

PUNTO CRITICO — forward algorithm:
  model.predict() usa Viterbi (smoothing bidirezionale) → look-ahead bias.
  Qui si usa SOLO il forward algorithm (inferenza filtrata): ogni stima al
  tempo T dipende esclusivamente da obs_0:T.
"""
from __future__ import annotations

import logging
import pickle
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from hmmlearn import hmm
from scipy.special import logsumexp
from scipy.stats import multivariate_normal

from data.feature_engineering import FEATURE_COLUMNS

logger = logging.getLogger("regime-trader")


# ──────────────────────────────────────────────────────────────────────────────
# COSTANTI — ETICHETTE REGIME
# ──────────────────────────────────────────────────────────────────────────────

# Etichette ordinate per rendimento medio crescente (BEAR → BULL).
# Lo STRATEGY layer ordina per volatilità indipendentemente — le etichette
# sono solo per leggibilità umana.
REGIME_LABELS: dict[int, list[str]] = {
    3: ["BEAR", "NEUTRAL", "BULL"],
    4: ["CRASH", "BEAR", "BULL", "EUPHORIA"],
    5: ["CRASH", "BEAR", "NEUTRAL", "BULL", "EUPHORIA"],
    6: ["CRASH", "STRONG_BEAR", "WEAK_BEAR", "WEAK_BULL", "STRONG_BULL", "EUPHORIA"],
    7: ["CRASH", "STRONG_BEAR", "WEAK_BEAR", "NEUTRAL", "WEAK_BULL", "STRONG_BULL", "EUPHORIA"],
}

# Defaults di rischio per etichetta: (strategy_type, max_leverage, max_pos_pct, min_confidence)
_LABEL_DEFAULTS: dict[str, tuple[str, float, float, float]] = {
    "CRASH":        ("defensive",  0.00, 0.05, 0.80),
    "STRONG_BEAR":  ("defensive",  0.50, 0.08, 0.70),
    "BEAR":         ("defensive",  0.75, 0.10, 0.65),
    "WEAK_BEAR":    ("balanced",   0.90, 0.12, 0.60),
    "NEUTRAL":      ("balanced",   1.00, 0.15, 0.55),
    "WEAK_BULL":    ("growth",     1.10, 0.18, 0.55),
    "BULL":         ("growth",     1.15, 0.20, 0.55),
    "STRONG_BULL":  ("aggressive", 1.20, 0.22, 0.55),
    "EUPHORIA":     ("aggressive", 1.25, 0.25, 0.55),
}


# ──────────────────────────────────────────────────────────────────────────────
# DATACLASSES
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class RegimeInfo:
    """Metadati statici di uno stato HMM — impostati dopo il training."""
    regime_id: int
    regime_name: str
    expected_return: float            # Media di log_ret_1 per questo stato
    expected_volatility: float        # Std di log_ret_1 per questo stato
    recommended_strategy_type: str    # "defensive"|"balanced"|"growth"|"aggressive"
    max_leverage_allowed: float
    max_position_size_pct: float
    min_confidence_to_act: float


@dataclass
class RegimeState:
    """Stato dinamico — predizione del regime corrente con filtro di stabilità."""
    label: str
    state_id: int
    probability: float                # P(stato effettivo | obs_0:t)
    state_probabilities: list[float]  # Posterior completo su tutti gli stati
    timestamp: Optional[datetime]
    is_confirmed: bool                # False durante la finestra di transizione N-barre
    consecutive_bars: int             # Barre nel regime confermato corrente
    size_multiplier: float = 1.0      # 0.75 durante transizione non confermata


# ──────────────────────────────────────────────────────────────────────────────
# HMM ENGINE
# ──────────────────────────────────────────────────────────────────────────────

class HMMEngine:
    """
    Volatility classifier using Gaussian HMM with forward-only inference.

    Public API:
      fit(features)                        → seleziona n_stati via BIC e addestra
      predict_regime_filtered(features)    → stati per ogni barra (no stability filter)
      predict_regime_proba(features)       → posterior per ogni barra
      update(row, timestamp)               → aggiornamento incrementale (live)
      predict_current_regime(features)     → stato corrente con stability filter
      get_regime_stability()               → barre consecutive nel regime confermato
      get_transition_matrix()              → matrice di transizione appresa
      detect_regime_change()               → True se l'ultima barra ha confermato un cambio
      get_regime_flicker_rate()            → cambi di stato nella finestra flicker
      is_flickering()                      → True se il flicker supera la soglia
      save(path) / load(path)              → persistenza con pickle
    """

    MIN_TRAIN_BARS = 504  # 2 anni di dati giornalieri

    def __init__(self, config: dict) -> None:
        self.config = config
        self.model: Optional[hmm.GaussianHMM] = None
        self.n_states: Optional[int] = None
        self._best_bic: float = float("inf")
        self._regime_info: dict[int, RegimeInfo] = {}

        # Stability filter state
        self._confirmed_state: Optional[int] = None
        self._pending_state: Optional[int] = None
        self._pending_bars: int = 0
        self._consecutive_in_confirmed: int = 0
        self._last_change_confirmed: bool = False

        # Flicker tracking con deque a dimensione fissa
        flicker_window = config.get("flicker_window", 20)
        self._raw_state_history: deque[int] = deque(maxlen=flicker_window)

        # Cache forward algorithm per aggiornamenti incrementali (streaming)
        self._log_alpha_cache: Optional[np.ndarray] = None

    # ──────────────────────────────────────────────────────────────────────
    # TRAINING
    # ──────────────────────────────────────────────────────────────────────

    def fit(self, features: pd.DataFrame) -> None:
        """
        Seleziona n_components via BIC, addestra il modello finale, etichetta i regimi.

        Testa tutti i candidati in config['n_candidates'] con config['n_init']
        inizializzazioni random ciascuno. Seleziona il BIC minimo.

        Args:
            features: DataFrame con FEATURE_COLUMNS, almeno MIN_TRAIN_BARS righe.
        """
        X = features.values.astype(float)

        if len(X) < self.MIN_TRAIN_BARS:
            raise ValueError(
                f"Servono almeno {self.MIN_TRAIN_BARS} barre per il training, "
                f"ricevute {len(X)}."
            )

        candidates = self.config.get("n_candidates", [3, 4, 5, 6, 7])
        n_init     = self.config.get("n_init", 10)
        cov_type   = self.config.get("covariance_type", "full")

        logger.info(
            f"HMM model selection: candidati={candidates}, "
            f"n_init={n_init}, cov={cov_type}"
        )

        best_model: Optional[hmm.GaussianHMM] = None
        best_bic   = float("inf")
        best_k     = None

        for k in candidates:
            model_k, bic_k = self._train_candidate(X, k, n_init, cov_type)
            logger.info(f"  n_states={k}: BIC={bic_k:.2f}")
            if bic_k < best_bic:
                best_bic   = bic_k
                best_model = model_k
                best_k     = k

        logger.info(f"Modello selezionato: n_states={best_k} (BIC={best_bic:.2f})")

        self.model    = best_model
        self.n_states = best_k
        self._best_bic = best_bic

        self._label_regimes(X)
        self._reset_filter()

    def _train_candidate(
        self,
        X: np.ndarray,
        k: int,
        n_init: int,
        cov_type: str,
    ) -> tuple[hmm.GaussianHMM, float]:
        """Addestra con k stati e n_init seed, restituisce il miglior modello."""
        best_model = None
        best_bic   = float("inf")

        for seed in range(n_init):
            try:
                m = hmm.GaussianHMM(
                    n_components=k,
                    covariance_type=cov_type,
                    n_iter=200,
                    random_state=seed,
                    verbose=False,
                )
                m.fit(X)

                bic    = self._compute_bic(m, X)
                log_lik = m.score(X)

                logger.debug(
                    f"    k={k}, seed={seed:02d}: "
                    f"log_lik={log_lik:.1f}, BIC={bic:.1f}, "
                    f"converged={m.monitor_.converged}, "
                    f"iters={m.monitor_.iter}"
                )

                if bic < best_bic:
                    best_bic   = bic
                    best_model = m

            except Exception as exc:
                logger.debug(f"    k={k}, seed={seed}: training fallito — {exc}")

        if best_model is None:
            raise RuntimeError(
                f"Tutte le {n_init} inizializzazioni fallite per n_states={k}"
            )

        return best_model, best_bic

    def _compute_bic(self, model: hmm.GaussianHMM, X: np.ndarray) -> float:
        """
        BIC = -2 * log_likelihood + n_params * log(n_samples).

        Parametri per GaussianHMM full:
          - Matrice transizione: K*(K-1)  (ogni riga ha K-1 gradi di libertà)
          - Start probs: K-1
          - Medie: K*D
          - Covarianze full: K * D*(D+1)/2
        """
        K = model.n_components
        D = X.shape[1]
        n = len(X)

        n_params = (
            K * (K - 1)              # transmat
            + (K - 1)               # startprob
            + K * D                 # means
            + K * D * (D + 1) // 2  # full covariance
        )

        return -2.0 * model.score(X) + n_params * np.log(n)

    def _label_regimes(self, X: np.ndarray) -> None:
        """
        Assegna etichette agli stati ordinandoli per rendimento medio crescente.

        Usa la feature log_ret_1 (indice 0 in FEATURE_COLUMNS) come proxy
        del rendimento atteso di ogni stato.
        """
        K      = self.n_states
        labels = REGIME_LABELS[K]

        mean_returns  = self.model.means_[:, 0]   # log_ret_1 per ogni stato
        sorted_states = np.argsort(mean_returns)   # dal più basso al più alto

        self._regime_info = {}
        for rank, state_idx in enumerate(sorted_states):
            label = labels[rank]
            strat, max_lev, max_pos, min_conf = _LABEL_DEFAULTS[label]

            exp_ret = float(mean_returns[state_idx])
            # Volatilità attesa = std del log_ret_1 → sqrt della varianza (covars[j][0,0])
            exp_vol = float(np.sqrt(max(self.model.covars_[state_idx][0, 0], 0.0)))

            self._regime_info[int(state_idx)] = RegimeInfo(
                regime_id=int(state_idx),
                regime_name=label,
                expected_return=exp_ret,
                expected_volatility=exp_vol,
                recommended_strategy_type=strat,
                max_leverage_allowed=max_lev,
                max_position_size_pct=max_pos,
                min_confidence_to_act=min_conf,
            )

        summary = {
            int(idx): f"{self._regime_info[int(idx)].regime_name}"
                      f"(ret={mean_returns[idx]:.4f})"
            for idx in sorted_states
        }
        logger.info(f"Regime labels: {summary}")

    def _reset_filter(self) -> None:
        """Resetta stability filter e cache dopo un retrain."""
        self._confirmed_state       = None
        self._pending_state         = None
        self._pending_bars          = 0
        self._consecutive_in_confirmed = 0
        self._last_change_confirmed = False
        self._raw_state_history.clear()
        self._log_alpha_cache = None

    # ──────────────────────────────────────────────────────────────────────
    # FORWARD ALGORITHM — NO LOOK-AHEAD BIAS
    # ──────────────────────────────────────────────────────────────────────

    def _log_emission_proba(self, X: np.ndarray) -> np.ndarray:
        """
        Log P(obs_t | stato_j) per tutti i t e j.

        Usa scipy.stats.multivariate_normal per stabilità numerica.
        Restituisce array (T, K).
        """
        T, K   = len(X), self.model.n_components
        log_em = np.empty((T, K))

        for j in range(K):
            rv = multivariate_normal(
                mean=self.model.means_[j],
                cov=self.model.covars_[j],
                allow_singular=True,
            )
            log_em[:, j] = rv.logpdf(X)

        return log_em

    def _forward_log_alpha(self, X: np.ndarray) -> np.ndarray:
        """
        Forward algorithm in log space — inferenza FILTRATA (no smoothing).

        alpha[t, j] = P(stato_j | obs_0:t)

        Matematica:
          log α₀(j) = log π(j) + log b(j, x₀)
          log αₜ(j) = logsumexp_i[ log αₜ₋₁(i) + log aᵢⱼ ] + log b(j, xₜ)

        Restituisce log_alpha (T, K) non normalizzato.
        """
        T, K = len(X), self.model.n_components

        log_transmat  = np.log(self.model.transmat_  + 1e-300)
        log_startprob = np.log(self.model.startprob_ + 1e-300)
        log_em        = self._log_emission_proba(X)

        log_alpha    = np.empty((T, K))
        log_alpha[0] = log_startprob + log_em[0]

        for t in range(1, T):
            # Shape: log_alpha[t-1, :, None] (K,1) + log_transmat (K,K) → (K,K)
            # logsumexp su axis=0 → somma su stati precedenti → (K,)
            log_alpha[t] = (
                logsumexp(log_alpha[t - 1, :, np.newaxis] + log_transmat, axis=0)
                + log_em[t]
            )

        return log_alpha

    def _update_forward_cache(self, x_new: np.ndarray) -> np.ndarray:
        """
        Aggiornamento incrementale del forward alpha per una singola nuova osservazione.
        O(K²) per barra — efficiente per trading live.

        Args:
            x_new: vettore (D,) della barra corrente

        Returns:
            log_alpha per il timestep corrente, shape (K,)
        """
        log_transmat = np.log(self.model.transmat_  + 1e-300)
        log_em_new   = self._log_emission_proba(x_new.reshape(1, -1))[0]

        if self._log_alpha_cache is None:
            log_startprob         = np.log(self.model.startprob_ + 1e-300)
            self._log_alpha_cache = log_startprob + log_em_new
        else:
            self._log_alpha_cache = (
                logsumexp(
                    self._log_alpha_cache[:, np.newaxis] + log_transmat,
                    axis=0,
                )
                + log_em_new
            )

        return self._log_alpha_cache.copy()

    # ──────────────────────────────────────────────────────────────────────
    # PREDIZIONE — API PUBBLICA
    # ──────────────────────────────────────────────────────────────────────

    def predict_regime_filtered(self, features: pd.DataFrame) -> np.ndarray:
        """
        Sequenza di stati filtrati via forward algorithm — nessun look-ahead.

        argmax P(stato_t | obs_0:t) per ogni t.
        NON applica il stability filter — usa per backtest batch.

        Returns:
            Array di interi (T,) con gli ID di stato per ogni barra.
        """
        self._assert_fitted()
        log_alpha = self._forward_log_alpha(features.values.astype(float))
        return np.argmax(log_alpha, axis=1)

    def predict_regime_proba(self, features: pd.DataFrame) -> np.ndarray:
        """
        Distribuzione posteriore filtrata P(stato_t | obs_0:t) per ogni t.

        Returns:
            Array (T, K), ogni riga somma a 1.
        """
        self._assert_fitted()
        log_alpha = self._forward_log_alpha(features.values.astype(float))
        log_norm  = logsumexp(log_alpha, axis=1, keepdims=True)
        return np.exp(log_alpha - log_norm)

    def update(
        self,
        current_row: pd.Series,
        timestamp: Optional[datetime] = None,
    ) -> RegimeState:
        """
        Aggiornamento incrementale per una singola nuova barra (modalità live).

        Deve essere chiamato in ordine sequenziale — mantiene la cache interna.
        O(K²) per chiamata.

        Args:
            current_row: Serie con FEATURE_COLUMNS per la barra corrente
            timestamp:   Timestamp della barra

        Returns:
            RegimeState con stability filter applicato
        """
        self._assert_fitted()
        x         = current_row.values.astype(float)
        log_alpha = self._update_forward_cache(x)
        raw_state = int(np.argmax(log_alpha))
        return self._apply_stability_filter(raw_state, log_alpha, timestamp)

    def predict_current_regime(
        self,
        features: pd.DataFrame,
        timestamp: Optional[datetime] = None,
    ) -> RegimeState:
        """
        Predice il regime corrente con forward pass completo e stability filter.

        Più lento di update() ma non richiede chiamate sequenziali.
        Adatto per ispezione occasionale o al bootstrap.

        Args:
            features: Tutta la storia disponibile fino alla barra corrente
            timestamp: Timestamp della barra corrente

        Returns:
            RegimeState della barra più recente
        """
        self._assert_fitted()
        X         = features.values.astype(float)
        log_alpha = self._forward_log_alpha(X)[-1]
        raw_state = int(np.argmax(log_alpha))
        return self._apply_stability_filter(raw_state, log_alpha, timestamp)

    # ──────────────────────────────────────────────────────────────────────
    # STABILITY FILTER
    # ──────────────────────────────────────────────────────────────────────

    def _apply_stability_filter(
        self,
        raw_state: int,
        log_alpha: np.ndarray,
        timestamp: Optional[datetime],
    ) -> RegimeState:
        """
        Filtro N-barre: un cambio di regime viene confermato solo dopo
        `stability_bars` barre consecutive nel nuovo stato.

        Durante la transizione: riporta il regime precedente (confermato)
        con size_multiplier=0.75.
        """
        stability_bars = self.config.get("stability_bars", 3)
        self._last_change_confirmed = False

        # Aggiorna storia per flicker detection
        self._raw_state_history.append(raw_state)

        # Prima chiamata: inizializza senza richiedere N barre
        if self._confirmed_state is None:
            self._confirmed_state          = raw_state
            self._consecutive_in_confirmed = 1
            is_confirmed   = True
            effective_state = raw_state

        elif raw_state == self._confirmed_state:
            # Regime stabile — nessuna transizione in corso
            self._pending_state         = None
            self._pending_bars          = 0
            self._consecutive_in_confirmed += 1
            is_confirmed   = True
            effective_state = self._confirmed_state

        else:
            # Candidato di cambio regime
            if raw_state == self._pending_state:
                self._pending_bars += 1
            else:
                # Candidato diverso dal precedente → reset counter
                self._pending_state = raw_state
                self._pending_bars  = 1

            if self._pending_bars >= stability_bars:
                # Cambio confermato
                old = self._regime_info[self._confirmed_state].regime_name
                new = self._regime_info[self._pending_state].regime_name
                logger.warning(
                    f"Regime change confermato: {old} → {new} "
                    f"({self._pending_bars} barre)"
                )
                self._confirmed_state          = self._pending_state
                self._pending_state            = None
                self._pending_bars             = 0
                self._consecutive_in_confirmed = stability_bars
                self._last_change_confirmed    = True
                is_confirmed   = True
                effective_state = self._confirmed_state
            else:
                # Ancora in finestra di transizione → riporta vecchio regime
                is_confirmed   = False
                effective_state = self._confirmed_state
                pending_name   = self._regime_info.get(
                    self._pending_state,
                    self._regime_info[self._confirmed_state]
                ).regime_name
                logger.debug(
                    f"Transizione in corso: "
                    f"{self._regime_info[self._confirmed_state].regime_name} → "
                    f"{pending_name} "
                    f"({self._pending_bars}/{stability_bars})"
                )

        # Normalizza log_alpha per ottenere probabilità
        probs = np.exp(log_alpha - logsumexp(log_alpha))
        info  = self._regime_info[effective_state]

        return RegimeState(
            label=info.regime_name,
            state_id=effective_state,
            probability=float(probs[effective_state]),
            state_probabilities=probs.tolist(),
            timestamp=timestamp,
            is_confirmed=is_confirmed,
            consecutive_bars=self._consecutive_in_confirmed,
            size_multiplier=1.0 if is_confirmed else 0.75,
        )

    # ──────────────────────────────────────────────────────────────────────
    # QUERY METADATI
    # ──────────────────────────────────────────────────────────────────────

    def get_regime_stability(self) -> int:
        """Barre consecutive nel regime attualmente confermato."""
        return self._consecutive_in_confirmed

    def get_transition_matrix(self) -> Optional[np.ndarray]:
        """Matrice di transizione appresa, shape (K, K). None se non addestrato."""
        return self.model.transmat_.copy() if self.model is not None else None

    def detect_regime_change(self) -> bool:
        """True solo se l'ultima chiamata a update/predict ha confermato un cambio."""
        return self._last_change_confirmed

    def get_regime_flicker_rate(self) -> int:
        """Numero di cambi di stato nella finestra flicker corrente."""
        h = list(self._raw_state_history)
        if len(h) < 2:
            return 0
        return sum(1 for i in range(1, len(h)) if h[i] != h[i - 1])

    def is_flickering(self) -> bool:
        """True se il flicker rate supera la soglia configurata."""
        threshold = self.config.get("flicker_threshold", 4)
        return self.get_regime_flicker_rate() > threshold

    def get_regime_info(self, state_id: Optional[int] = None) -> Optional[RegimeInfo]:
        """Restituisce i metadati del regime (corrente se state_id è None)."""
        sid = state_id if state_id is not None else self._confirmed_state
        return self._regime_info.get(sid) if sid is not None else None

    def is_fitted(self) -> bool:
        """True se il modello è stato addestrato."""
        return self.model is not None

    # ──────────────────────────────────────────────────────────────────────
    # PERSISTENZA
    # ──────────────────────────────────────────────────────────────────────

    def save(self, path: Path) -> None:
        """Salva il modello e i metadati su disco con pickle."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        payload = {
            "model":           self.model,
            "n_regimes":       self.n_states,
            "bic":             self._best_bic,
            "training_date":   datetime.now().isoformat(),
            "labels":          {k: v.regime_name for k, v in self._regime_info.items()},
            "regime_info":     self._regime_info,
            "feature_columns": FEATURE_COLUMNS,
            "config":          self.config,
        }

        with open(path, "wb") as f:
            pickle.dump(payload, f)

        logger.info(
            f"Modello salvato: {path} "
            f"(n_states={self.n_states}, BIC={self._best_bic:.2f})"
        )

    @classmethod
    def load(cls, path: Path) -> "HMMEngine":
        """Carica un modello salvato da disco."""
        with open(path, "rb") as f:
            payload = pickle.load(f)

        engine              = cls(config=payload["config"])
        engine.model        = payload["model"]
        engine.n_states     = payload["n_regimes"]
        engine._best_bic    = payload["bic"]
        engine._regime_info = payload["regime_info"]

        logger.info(
            f"Modello caricato: {path} "
            f"(n_states={engine.n_states}, "
            f"trained={payload['training_date']})"
        )
        return engine

    # ──────────────────────────────────────────────────────────────────────
    # UTILITY INTERNE
    # ──────────────────────────────────────────────────────────────────────

    def _assert_fitted(self) -> None:
        if self.model is None:
            raise RuntimeError(
                "HMMEngine non addestrato. Chiamare fit() prima di predict_*."
            )
