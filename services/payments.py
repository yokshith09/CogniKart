"""cognikart-payments -- simulated payment authorization.

What this service exists to demonstrate: ROOT CAUSE. It is the injection point
for the flagship `payment-timeout-cascade` scenario, and it runs at the lowest
concurrency of the four services so injected latency forces visible horizontal
scaling rather than being absorbed by a single instance.

Two independent ways to make it fail, and the difference matters:

  requested outcome  -- the shopper picks "rejected" or "slow" at checkout.
                        Explicit, per-order, one log line. Good for showing
                        what a failure looks like.
  chaos scenario     -- payments degrades for everyone, sustained. This is
                        what actually trips a threshold and raises an incident
                        in OpsMind, because an incident needs a rate, not a
                        single event.

Both are recorded on the log line so the demo is auditable, and OpsMind must
read neither for detection.
"""
import random
import time
import uuid
from typing import Any, Dict

from fastapi import APIRouter, Body, Request, Response

from ..common import chaos, events, logging as klog

router = APIRouter(tags=["payments"])

NATURAL_DECLINE_RATE = 0.03   # a real gateway declines some cards; so do we
PROVIDER = "mock-pay"
SLOW_OUTCOME_DELAY_S = 7.0    # exceeds the caller's timeout, producing a real one


@router.post("/payments/authorize")
def authorize(request: Request, response: Response,
              payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    injected_ms = chaos.inject_latency()

    order_id = payload.get("orderId") or "unknown"
    amount = float(payload.get("amountInr") or 0)
    outcome = (payload.get("outcome") or "approved").lower()
    if outcome not in events.PAYMENT_OUTCOMES:
        outcome = "approved"

    request.state.order_id = order_id
    request.state.cart_value_inr = amount

    if amount <= 0:
        request.state.error_code = events.ErrorCode.VALIDATION_ERROR
        request.state.error_class = "ValidationError"
        response.status_code = 400
        return {"error": events.ErrorCode.VALIDATION_ERROR,
                "message": "amountInr must be greater than zero"}

    # --- shopper-requested slow provider --------------------------------
    if outcome == "slow":
        klog.warning(
            "payment provider is responding slowly for %s (requested)" % order_id,
            events.PAYMENT_UPSTREAM_ERROR, orderId=order_id, cartValueInr=amount,
            paymentProvider=PROVIDER, paymentOutcomeRequested=outcome,
            chaos_scenario=chaos.current_scenario())
        time.sleep(SLOW_OUTCOME_DELAY_S)

    # --- shopper-requested rejection ------------------------------------
    if outcome == "rejected":
        klog.error(
            "payment provider rejected authorization for %s (requested)" % order_id,
            events.PAYMENT_UPSTREAM_ERROR, orderId=order_id, cartValueInr=amount,
            paymentProvider=PROVIDER, paymentOutcomeRequested=outcome,
            chaos_scenario=chaos.current_scenario())
        request.state.error_code = events.ErrorCode.PAYMENT_PROVIDER_ERROR
        request.state.error_class = "PaymentProviderError"
        response.status_code = 502
        return {"error": events.ErrorCode.PAYMENT_PROVIDER_ERROR,
                "message": "payment provider rejected the transaction",
                "retryable": True}

    # --- injected provider failure (chaos, affects everyone) -------------
    if chaos.should_fail():
        klog.error(
            "payment provider returned an error for %s" % order_id,
            events.PAYMENT_UPSTREAM_ERROR, orderId=order_id, cartValueInr=amount,
            paymentProvider=PROVIDER, injectedLatencyMs=injected_ms,
            paymentOutcomeRequested=outcome,
            chaos_scenario=chaos.current_scenario())
        request.state.error_code = events.ErrorCode.PAYMENT_PROVIDER_ERROR
        request.state.error_class = "PaymentProviderError"
        response.status_code = 503
        return {"error": events.ErrorCode.PAYMENT_PROVIDER_ERROR,
                "message": "payment provider unavailable", "retryable": True}

    # --- natural decline: a client outcome, not a service fault ----------
    if random.random() < NATURAL_DECLINE_RATE:
        klog.warning("card declined for %s" % order_id, events.PAYMENT_DECLINED,
                     orderId=order_id, cartValueInr=amount, paymentProvider=PROVIDER,
                     paymentOutcomeRequested=outcome,
                     chaos_scenario=chaos.current_scenario())
        request.state.error_code = events.ErrorCode.PAYMENT_DECLINED
        request.state.error_class = "PaymentDeclined"
        response.status_code = 402
        return {"error": events.ErrorCode.PAYMENT_DECLINED,
                "message": "card declined", "retryable": False}

    txn = "txn_" + uuid.uuid4().hex[:12]
    klog.info("authorized %.2f for %s" % (amount, order_id), events.PAYMENT_AUTHORIZED,
              orderId=order_id, cartValueInr=amount, transactionId=txn,
              paymentProvider=PROVIDER, paymentOutcomeRequested=outcome,
              chaos_scenario=chaos.current_scenario())
    return {"authorized": True, "transactionId": txn, "amountInr": amount,
            "provider": PROVIDER}


@router.post("/payments/refund")
def refund(request: Request, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    chaos.inject_latency()
    order_id = payload.get("orderId") or "unknown"
    amount = float(payload.get("amountInr") or 0)
    request.state.order_id = order_id
    klog.info("refunded %.2f for %s" % (amount, order_id), events.PAYMENT_REFUNDED,
              orderId=order_id, cartValueInr=amount, paymentProvider=PROVIDER,
              chaos_scenario=chaos.current_scenario())
    return {"refunded": True, "orderId": order_id, "amountInr": amount}


def startup() -> None:
    klog.info("payments ready (provider=%s, natural decline %.0f%%)"
              % (PROVIDER, NATURAL_DECLINE_RATE * 100),
              events.SERVICE_STARTED, paymentProvider=PROVIDER)
