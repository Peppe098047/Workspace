"""Classificazione degli errori Alpaca per decidere se ritentare."""
from __future__ import annotations

from alpaca.common.exceptions import APIError


_NON_RETRYABLE_CODES = {
    40310000,  # potential wash trade / opposite side order
    42210000,  # validation error
    42210001,
    40010001,  # client_order_id must be unique — ritentare lo stesso id è futile;
               # il recupero idempotente è in AlpacaClient._submit_idempotent
}

_NON_RETRYABLE_TEXT = (
    "potential wash trade",
    "opposite side market/stop order exists",
    "stop price must be",
    "validation",
)


def is_non_retryable_api_error(exc: APIError) -> bool:
    """True per errori di validazione/ordine che un retry identico non risolve."""
    # exc.code è una PROPERTY di alpaca-py che fa error["code"] e solleva
    # KeyError se il JSON d'errore non ha quel campo (getattr NON protegge
    # dalle property che esplodono): mai mascherare l'errore API originale.
    try:
        code = exc.code
    except Exception:
        code = None
    if code in _NON_RETRYABLE_CODES:
        return True

    message = str(exc).lower()
    return any(text in message for text in _NON_RETRYABLE_TEXT)
