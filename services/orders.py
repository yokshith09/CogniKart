"""cognikart-orders -- the order lifecycle.

What this service exists to demonstrate: BLAST RADIUS AND AMPLIFICATION. It
calls catalog to reserve stock, then payments to authorise, and it retries
payments twice with no backoff. Under the cascade scenario that retry
behaviour multiplies request and log volume at precisely the moment the
dependency can least cope -- which is the amplification OpsMind prices and the
optimization engine recommends fixing. The missing backoff is a deliberate,
realistic antipattern, not an oversight.

The lifecycle is two-phase on purpose:

    place   ->  PENDING   stock is reserved here, so an order holds real units
    pay     ->  PAID      a separate call, so payment can succeed, fail or
                          hang without affecting whether stock was held
    fulfil  ->  FULFILLED staff action
    cancel  ->  CANCELLED releases the reserved stock

Two phases produce far richer telemetry than a single checkout call, and they
mirror how real commerce systems actually behave.
"""
import threading
import time
import uuid
from collections import deque
from typing import Any, Deque, Dict, List, Optional

from fastapi import APIRouter, Body, Query, Request, Response

from ..common import chaos, downstream, events, logging as klog
from ..common.config import settings

router = APIRouter(tags=["orders"])

PENDING, PAID, FULFILLED, CANCELLED = "PENDING", "PAID", "FULFILLED", "CANCELLED"

_lock = threading.RLock()
# Bounded ring. No database: Cloud Logging is the durable record, and this is
# a working set. See docs/architecture.md on why Cloud SQL was dropped.
_orders: Deque[Dict[str, Any]] = deque(maxlen=500)
_index: Dict[str, Dict[str, Any]] = {}


def _store(order: Dict[str, Any]) -> None:
    with _lock:
        if len(_orders) == _orders.maxlen and _orders[0]["orderId"] != order["orderId"]:
            _index.pop(_orders[0]["orderId"], None)
        if order["orderId"] not in _index:
            _orders.append(order)
        _index[order["orderId"]] = order


def _fail(request: Request, response: Response, code: str, status: int,
          message: str, **extra: Any) -> Dict[str, Any]:
    request.state.error_code = code
    request.state.error_class = "".join(w.title() for w in code.split("_"))
    response.status_code = status
    out = {"error": code, "message": message}
    out.update(extra)
    return out


def _public(order: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in order.items() if k != "_internal"}


# --- place an order --------------------------------------------------------
@router.post("/orders")
async def place_order(request: Request, response: Response,
                      payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    chaos.inject_latency()
    items: List[Dict[str, Any]] = payload.get("items") or []
    user_id = payload.get("userId") or "anonymous"
    user_hash = payload.get("userHash")
    order_id = uuid.uuid4().hex[:8].upper()
    request.state.order_id = order_id

    if not items:
        klog.warning("order attempted with an empty basket", events.CART_INVALID,
                     orderId=order_id, userIdHash=user_hash,
                     chaos_scenario=chaos.current_scenario())
        return _fail(request, response, events.ErrorCode.EMPTY_CART, 400,
                     "your basket is empty")

    total = round(sum(float(i.get("priceInr", 0)) * int(i.get("qty", 1))
                      for i in items), 2)
    units = sum(int(i.get("qty", 1)) for i in items)
    request.state.cart_value_inr = total
    request.state.sku_count = len(items)

    klog.info("order %s placed: %d unit(s), %.2f" % (order_id, units, total),
              events.ORDER_PLACED, orderId=order_id, cartValueInr=total,
              skuCount=len(items), units=units, userIdHash=user_hash,
              chaos_scenario=chaos.current_scenario())

    # --- reserve stock ---------------------------------------------------
    try:
        status, body, ms = await downstream.call(
            request, "catalog", "POST", "/internal/stock/reserve",
            json_body={"items": items, "orderId": order_id})
        request.state.dependency = "cognikart-catalog"
        request.state.dependency_latency_ms = ms
    except downstream.DependencyError as exc:
        request.state.dependency = "cognikart-catalog"
        request.state.dependency_latency_ms = exc.latency_ms
        klog.error("stock service failed while placing %s: %s" % (order_id, exc.code),
                   events.CHECKOUT_FAILED, orderId=order_id, cartValueInr=total,
                   errorCode=exc.code, dependency="cognikart-catalog",
                   dependencyLatencyMs=exc.latency_ms, userIdHash=user_hash,
                   chaos_scenario=chaos.current_scenario())
        return _fail(request, response, exc.code, 502,
                     "could not reserve stock right now", orderId=order_id)

    # downstream.call raises only on 5xx / timeout, so a 409 arrives here as a
    # normal response and must be handled explicitly.
    if status != 200:
        code = (body or {}).get("error") if isinstance(body, dict) else None
        code = code or events.ErrorCode.INSUFFICIENT_STOCK
        message = (body or {}).get("message") if isinstance(body, dict) else None
        request.state.stock_shortfall = True
        klog.warning("stock reservation rejected for %s: %s" % (order_id, code),
                     events.CHECKOUT_FAILED, orderId=order_id, cartValueInr=total,
                     skuCount=len(items), errorCode=code, stockShortfall=True,
                     dependency="cognikart-catalog", userIdHash=user_hash,
                     chaos_scenario=chaos.current_scenario())
        return _fail(request, response, code, 409,
                     message or "not enough stock", orderId=order_id)

    order = {
        "orderId": order_id,
        "status": PENDING,
        "items": items,
        "units": units,
        "totalInr": total,
        "userId": user_id,
        "userHash": user_hash,
        "createdAt": time.time(),
        "paidAt": None,
        "fulfilledAt": None,
        "cancelledAt": None,
        "transactionId": None,
        "lastFailure": None,
        "paymentAttempts": 0,
        "traceId": getattr(request.state, "trace_id", None),
    }
    _store(order)

    klog.info("stock reserved for %s" % order_id, events.ORDER_STOCK_RESERVED,
              orderId=order_id, units=units, cartValueInr=total,
              userIdHash=user_hash, chaos_scenario=chaos.current_scenario())
    return _public(order)


# --- pay for an order ------------------------------------------------------
@router.post("/orders/{order_id}/pay")
async def pay_order(request: Request, response: Response, order_id: str,
                    payload: Dict[str, Any] = Body(default_factory=dict)) -> Dict[str, Any]:
    chaos.inject_latency()
    order = _index.get(order_id)
    request.state.order_id = order_id
    if not order:
        return _fail(request, response, events.ErrorCode.ORDER_NOT_FOUND, 404,
                     "no such order")
    if order["status"] != PENDING:
        klog.warning("payment attempted on %s which is %s"
                     % (order_id, order["status"]), events.ORDER_NOT_PAYABLE,
                     orderId=order_id, orderStatus=order["status"],
                     userIdHash=order.get("userHash"),
                     chaos_scenario=chaos.current_scenario())
        return _fail(request, response, events.ErrorCode.ORDER_NOT_PAYABLE, 409,
                     "this order is %s and cannot be paid" % order["status"].lower())

    outcome = (payload.get("outcome") or "approved").lower()
    if outcome not in events.PAYMENT_OUTCOMES:
        outcome = "approved"
    total = order["totalInr"]
    request.state.cart_value_inr = total

    klog.info("payment requested for %s (outcome=%s)" % (order_id, outcome),
              events.ORDER_PAYMENT_REQUESTED, orderId=order_id, cartValueInr=total,
              paymentOutcomeRequested=outcome, userIdHash=order.get("userHash"),
              chaos_scenario=chaos.current_scenario())

    last_error: Optional[downstream.DependencyError] = None
    attempts = 0
    for attempt in range(settings.payment_max_retries + 1):
        attempts = attempt + 1
        try:
            status, body, dep_ms = await downstream.call(
                request, "payments", "POST", "/payments/authorize",
                json_body={"orderId": order_id, "amountInr": total,
                           "outcome": outcome})
            request.state.dependency = "cognikart-payments"
            request.state.dependency_latency_ms = dep_ms
            request.state.retry_count = attempt

            if status == 200 and isinstance(body, dict) and body.get("authorized"):
                with _lock:
                    order["status"] = PAID
                    order["paidAt"] = time.time()
                    order["transactionId"] = body.get("transactionId")
                    order["paymentAttempts"] = attempts
                    order["lastFailure"] = None
                klog.info("order %s paid on attempt %d" % (order_id, attempts),
                          events.ORDER_PAID, orderId=order_id, cartValueInr=total,
                          skuCount=len(order["items"]), retryCount=attempt,
                          transactionId=order["transactionId"],
                          dependency="cognikart-payments", dependencyLatencyMs=dep_ms,
                          paymentOutcomeRequested=outcome,
                          userIdHash=order.get("userHash"),
                          chaos_scenario=chaos.current_scenario())
                return _public(order)

            if status == 402:
                with _lock:
                    order["lastFailure"] = events.ErrorCode.PAYMENT_DECLINED
                    order["paymentAttempts"] = attempts
                klog.warning("card declined for %s" % order_id,
                             events.ORDER_PAYMENT_REJECTED, orderId=order_id,
                             cartValueInr=total,
                             errorCode=events.ErrorCode.PAYMENT_DECLINED,
                             paymentOutcomeRequested=outcome,
                             userIdHash=order.get("userHash"),
                             chaos_scenario=chaos.current_scenario())
                return _fail(request, response, events.ErrorCode.PAYMENT_DECLINED,
                             402, "your card was declined", orderId=order_id,
                             status=PENDING)

        except downstream.DependencyError as exc:
            last_error = exc
            request.state.dependency = "cognikart-payments"
            request.state.dependency_latency_ms = exc.latency_ms
            request.state.retry_count = attempt
            if attempt < settings.payment_max_retries:
                # No backoff. Deliberate antipattern -- this is the
                # amplification OpsMind detects and recommends fixing.
                klog.warning(
                    "payment attempt %d/%d failed for %s (%s), retrying immediately"
                    % (attempts, settings.payment_max_retries + 1, order_id, exc.code),
                    events.ORDER_PAYMENT_AUTHORIZE_RETRY, orderId=order_id,
                    cartValueInr=total, retryCount=attempt, errorCode=exc.code,
                    dependency="cognikart-payments",
                    dependencyLatencyMs=exc.latency_ms,
                    paymentOutcomeRequested=outcome,
                    userIdHash=order.get("userHash"),
                    chaos_scenario=chaos.current_scenario())
                continue

    code = last_error.code if last_error else events.ErrorCode.PAYMENT_PROVIDER_ERROR
    dep_ms = last_error.latency_ms if last_error else 0
    with _lock:
        order["lastFailure"] = code
        order["paymentAttempts"] = attempts
    request.state.retry_count = attempts - 1
    klog.error("payment failed for %s after %d attempt(s): %s"
               % (order_id, attempts, code),
               events.ORDER_PAYMENT_REJECTED, orderId=order_id, cartValueInr=total,
               skuCount=len(order["items"]), retryCount=attempts - 1, errorCode=code,
               dependency="cognikart-payments", dependencyLatencyMs=dep_ms,
               paymentOutcomeRequested=outcome, userIdHash=order.get("userHash"),
               chaos_scenario=chaos.current_scenario())
    klog.error("checkout failed for %s (%.2f at risk)" % (order_id, total),
               events.CHECKOUT_FAILED, orderId=order_id, cartValueInr=total,
               skuCount=len(order["items"]), errorCode=code,
               userIdHash=order.get("userHash"),
               chaos_scenario=chaos.current_scenario())
    return _fail(request, response, code, 502,
                 "we could not take payment for this order", orderId=order_id,
                 attempts=attempts, status=PENDING)


# --- cancel / fulfil -------------------------------------------------------
@router.post("/orders/{order_id}/cancel")
async def cancel_order(request: Request, response: Response, order_id: str,
                       payload: Dict[str, Any] = Body(default_factory=dict)) -> Dict[str, Any]:
    chaos.inject_latency()
    order = _index.get(order_id)
    request.state.order_id = order_id
    if not order:
        return _fail(request, response, events.ErrorCode.ORDER_NOT_FOUND, 404,
                     "no such order")
    if order["status"] in (CANCELLED, FULFILLED):
        return _fail(request, response, events.ErrorCode.ORDER_NOT_PAYABLE, 409,
                     "this order is already %s" % order["status"].lower())

    was = order["status"]
    with _lock:
        order["status"] = CANCELLED
        order["cancelledAt"] = time.time()

    # Put the units back on the shelf.
    try:
        await downstream.call(request, "catalog", "POST", "/internal/stock/release",
                              json_body={"items": order["items"], "orderId": order_id})
    except downstream.DependencyError as exc:
        klog.error("could not release stock for cancelled order %s: %s"
                   % (order_id, exc.code), events.ORDER_STOCK_RELEASED,
                   orderId=order_id, errorCode=exc.code,
                   chaos_scenario=chaos.current_scenario())

    klog.info("order %s cancelled (was %s)" % (order_id, was), events.ORDER_CANCELLED,
              orderId=order_id, cartValueInr=order["totalInr"],
              previousStatus=was, actorRole=payload.get("actorRole"),
              userIdHash=payload.get("actorHash") or order.get("userHash"),
              chaos_scenario=chaos.current_scenario())
    return _public(order)


@router.post("/orders/{order_id}/fulfil")
def fulfil_order(request: Request, response: Response, order_id: str,
                 payload: Dict[str, Any] = Body(default_factory=dict)) -> Dict[str, Any]:
    chaos.inject_latency()
    order = _index.get(order_id)
    request.state.order_id = order_id
    if not order:
        return _fail(request, response, events.ErrorCode.ORDER_NOT_FOUND, 404,
                     "no such order")
    if order["status"] != PAID:
        return _fail(request, response, events.ErrorCode.ORDER_NOT_PAYABLE, 409,
                     "only paid orders can be fulfilled")
    with _lock:
        order["status"] = FULFILLED
        order["fulfilledAt"] = time.time()
    klog.info("order %s marked fulfilled" % order_id, events.ORDER_FULFILLED,
              orderId=order_id, cartValueInr=order["totalInr"],
              units=order["units"], actorRole=payload.get("actorRole"),
              userIdHash=payload.get("actorHash"),
              chaos_scenario=chaos.current_scenario())
    return _public(order)


# --- reads -----------------------------------------------------------------
@router.get("/orders/{order_id}")
def get_order(request: Request, response: Response, order_id: str) -> Dict[str, Any]:
    chaos.inject_latency()
    order = _index.get(order_id)
    if not order:
        klog.warning("order %s not found" % order_id, events.ORDER_NOT_FOUND,
                     orderId=order_id, chaos_scenario=chaos.current_scenario())
        return _fail(request, response, events.ErrorCode.ORDER_NOT_FOUND, 404,
                     "no such order")
    return _public(order)


@router.get("/orders")
def list_orders(userId: Optional[str] = None,
                limit: int = Query(60, ge=1, le=200)) -> Dict[str, Any]:
    chaos.inject_latency()
    with _lock:
        rows = list(_orders)
    if userId:
        rows = [o for o in rows if o.get("userId") == userId]
    rows.sort(key=lambda o: o["createdAt"], reverse=True)
    return {"orders": [_public(o) for o in rows[:limit]], "total": len(rows)}


@router.get("/internal/orders/summary")
def summary() -> Dict[str, Any]:
    with _lock:
        rows = list(_orders)
    by_status: Dict[str, int] = {}
    for o in rows:
        by_status[o["status"]] = by_status.get(o["status"], 0) + 1
    return {"count": len(rows), "byStatus": by_status,
            "valueInr": round(sum(o["totalInr"] for o in rows), 2)}


def startup() -> None:
    klog.info("orders ready (payment retries=%d, dependency timeout=%.1fs)"
              % (settings.payment_max_retries, settings.dependency_timeout_s),
              events.SERVICE_STARTED, paymentMaxRetries=settings.payment_max_retries)
