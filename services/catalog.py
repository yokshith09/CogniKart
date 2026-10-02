"""cognikart-catalog -- the collection, stock levels and availability.

What this service exists to demonstrate: WASTE WITHOUT FAILURE. It carries
~90% of request volume, is deliberately provisioned at 1 GiB while using a
fraction of it, and is the service whose log level the
`debug-logging-left-on` scenario flips. It rarely breaks; it quietly costs
money. That is the whole Half-B story of the use case.

It also owns stock, which is the second natural source of real failure after
payments: ordering more than exists is rejected here, with no fault injection
involved at all.
"""
import json
import os
import threading
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Query, Request, Response

from ..common import chaos, events, logging as klog

router = APIRouter(tags=["catalog"])

_SEED_PATH = os.path.join(os.path.dirname(__file__), "..", "seed", "products.json")
_lock = threading.RLock()
_products: List[Dict[str, Any]] = []
_by_id: Dict[str, Dict[str, Any]] = {}
_meta: Dict[str, Any] = {}


def load_seed() -> int:
    global _products, _by_id, _meta
    with open(os.path.abspath(_SEED_PATH)) as fh:
        data = json.load(fh)
    with _lock:
        _products = [dict(p) for p in data["products"]]
        _by_id = {p["id"]: p for p in _products}
        _meta = {"collection": data.get("collection", ""),
                 "categories": data.get("categories", [])}
    return len(_products)


def _public(p: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(p)
    out["inStock"] = p["stock"] > 0 and p.get("available", True)
    return out


def _fail(request: Request, response: Response, code: str, status: int,
          message: str) -> Dict[str, Any]:
    request.state.error_code = code
    request.state.error_class = "".join(w.title() for w in code.split("_"))
    response.status_code = status
    return {"error": code, "message": message,
            "requestId": getattr(request.state, "request_id", "")}


# --- browsing --------------------------------------------------------------
@router.get("/categories")
def categories() -> Dict[str, Any]:
    chaos.inject_latency()
    with _lock:
        cats = sorted({p["category"] for p in _products})
        total = len(_products)
    klog.info("served %d categories" % len(cats), events.CATALOG_LIST,
              resultCount=len(cats), chaos_scenario=chaos.current_scenario())
    return {"categories": cats, "productCount": total,
            "collection": _meta.get("collection", "")}


@router.get("/products")
def list_products(
    request: Request,
    response: Response,
    category: Optional[str] = None,
    q: Optional[str] = None,
    includeUnavailable: bool = Query(False),
) -> Dict[str, Any]:
    chaos.inject_latency()
    chaos.leak_memory()
    if chaos.should_fail():
        return _fail(request, response, events.ErrorCode.UPSTREAM_UNAVAILABLE,
                     503, "catalog temporarily unavailable")

    with _lock:
        items = list(_products)
    if category and category.lower() != "all":
        items = [p for p in items if p["category"].lower() == category.lower()]
    if q:
        needle = q.strip().lower()
        items = [p for p in items
                 if needle in p["name"].lower() or needle in p["id"].lower()
                 or needle in p["blurb"].lower() or needle in p["category"].lower()]
    if not includeUnavailable:
        items = [p for p in items if p.get("available", True)]

    # DEBUG lines exist so `debug-logging-left-on` has something voluminous to
    # emit with no errors at all. Suppressed at INFO, which is the default.
    klog.debug(
        "catalog scan: category=%s q=%s matched=%d" % (category, q, len(items)),
        events.CATALOG_CACHE_DECISION,
        cacheHit=False, scanned=len(_products), matched=len(items),
        category=category, chaos_scenario=chaos.current_scenario(),
    )
    klog.info("listed %d pieces (category=%s)" % (len(items), category or "all"),
              events.CATALOG_LIST, resultCount=len(items), category=category,
              searchTerm=q, chaos_scenario=chaos.current_scenario())
    return {"total": len(items), "collection": _meta.get("collection", ""),
            "products": [_public(p) for p in items]}


@router.get("/products/{product_id}")
def get_product(request: Request, response: Response, product_id: str) -> Dict[str, Any]:
    chaos.inject_latency()
    chaos.leak_memory()
    if chaos.should_fail():
        return _fail(request, response, events.ErrorCode.UPSTREAM_UNAVAILABLE,
                     503, "catalog temporarily unavailable")
    with _lock:
        product = _by_id.get(product_id)
    if not product:
        klog.warning("piece %s not found" % product_id, events.CATALOG_NOT_FOUND,
                     productId=product_id, chaos_scenario=chaos.current_scenario())
        return _fail(request, response, events.ErrorCode.PRODUCT_NOT_FOUND,
                     404, "no such piece")
    klog.info("viewed %s" % product_id, events.CATALOG_VIEW, productId=product_id,
              category=product["category"], priceInr=product["priceInr"],
              chaos_scenario=chaos.current_scenario())
    return _public(product)


# --- stock reservation (called by orders) ----------------------------------
@router.post("/internal/stock/reserve")
def reserve_stock(request: Request, response: Response,
                  payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Reserve stock for an order, all-or-nothing.

    This is where "you cannot buy more than exists" is enforced. It is a real
    business rule, not injected chaos: ordering 20 of a piece with 7 left fails
    here every time, which gives the platform a second class of genuine error
    to group and alert on.
    """
    chaos.inject_latency()
    chaos.leak_memory()
    items = payload.get("items") or []
    order_id = payload.get("orderId")
    if not items:
        return _fail(request, response, events.ErrorCode.VALIDATION_ERROR,
                     400, "items required")

    if chaos.should_stock_conflict():
        sku = items[0].get("id", "unknown")
        klog.warning("stock contention on %s (injected)" % sku,
                     events.CATALOG_STOCK_CONFLICT, productId=sku, injected=True,
                     orderId=order_id, chaos_scenario=chaos.current_scenario())
        request.state.stock_shortfall = True
        return _fail(request, response, events.ErrorCode.STOCK_CONFLICT,
                     409, "could not reserve %s, please retry" % sku)

    with _lock:
        for i in items:
            p = _by_id.get(i.get("id"))
            if p is None:
                return _fail(request, response, events.ErrorCode.PRODUCT_NOT_FOUND,
                             404, "no such piece: %s" % i.get("id"))
            if not p.get("available", True):
                klog.warning("%s is not available for sale" % p["id"],
                             events.CATALOG_STOCK_CONFLICT, productId=p["id"],
                             orderId=order_id, chaos_scenario=chaos.current_scenario())
                return _fail(request, response, events.ErrorCode.PRODUCT_UNAVAILABLE,
                             409, "%s is currently unavailable" % p["name"])
            want = int(i.get("qty", 1))
            if want < 1:
                return _fail(request, response, events.ErrorCode.VALIDATION_ERROR,
                             400, "quantity must be at least 1")
            if p["stock"] < want:
                request.state.stock_shortfall = True
                klog.warning(
                    "insufficient stock for %s: wanted %d, have %d"
                    % (p["id"], want, p["stock"]),
                    events.CATALOG_STOCK_CONFLICT, productId=p["id"],
                    requestedQty=want, availableQty=p["stock"], orderId=order_id,
                    chaos_scenario=chaos.current_scenario())
                return _fail(request, response, events.ErrorCode.INSUFFICIENT_STOCK,
                             409, "only %d of %s left" % (p["stock"], p["name"]))
        # All checks passed -- commit the reservation.
        for i in items:
            _by_id[i["id"]]["stock"] -= int(i.get("qty", 1))

    units = sum(int(i.get("qty", 1)) for i in items)
    klog.info("reserved %d unit(s) across %d line(s)" % (units, len(items)),
              events.CATALOG_STOCK_RESERVED, skuCount=len(items), units=units,
              orderId=order_id, chaos_scenario=chaos.current_scenario())
    return {"reserved": True, "items": items, "units": units}


@router.post("/internal/stock/release")
def release_stock(request: Request, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Return reserved stock to the shelf when an order is cancelled."""
    items = payload.get("items") or []
    order_id = payload.get("orderId")
    with _lock:
        for i in items:
            p = _by_id.get(i.get("id"))
            if p:
                p["stock"] += int(i.get("qty", 1))
    units = sum(int(i.get("qty", 1)) for i in items)
    klog.info("released %d unit(s) back to stock" % units,
              events.ORDER_STOCK_RELEASED, skuCount=len(items), units=units,
              orderId=order_id, chaos_scenario=chaos.current_scenario())
    return {"released": True, "units": units}


# --- stock administration (staff, via the gateway) -------------------------
@router.post("/internal/stock/level")
def set_stock(request: Request, response: Response,
              payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    product_id = payload.get("productId")
    try:
        level = int(payload.get("stock"))
    except (TypeError, ValueError):
        return _fail(request, response, events.ErrorCode.VALIDATION_ERROR,
                     400, "stock must be a whole number")
    if level < 0 or level > 100000:
        return _fail(request, response, events.ErrorCode.VALIDATION_ERROR,
                     400, "stock must be between 0 and 100000")
    with _lock:
        p = _by_id.get(product_id)
        if not p:
            return _fail(request, response, events.ErrorCode.PRODUCT_NOT_FOUND,
                         404, "no such piece")
        before = p["stock"]
        p["stock"] = level
        # Setting stock to zero takes a piece off sale; restocking puts it back.
        if level == 0:
            p["available"] = False
        elif before == 0:
            p["available"] = True
        snapshot = _public(p)
    klog.info("stock for %s set to %d (was %d)" % (product_id, level, before),
              events.STOCK_LEVEL_SET, productId=product_id, stockBefore=before,
              stockAfter=level, actorRole=payload.get("actorRole"),
              userIdHash=payload.get("actorHash"),
              chaos_scenario=chaos.current_scenario())
    return {"updated": True, "product": snapshot}


@router.post("/internal/stock/availability")
def set_availability(request: Request, response: Response,
                     payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    product_id = payload.get("productId")
    available = bool(payload.get("available"))
    with _lock:
        p = _by_id.get(product_id)
        if not p:
            return _fail(request, response, events.ErrorCode.PRODUCT_NOT_FOUND,
                         404, "no such piece")
        p["available"] = available
        snapshot = _public(p)
    klog.info("%s marked %s" % (product_id, "available" if available else "unavailable"),
              events.STOCK_AVAILABILITY_CHANGED, productId=product_id,
              available=available, actorRole=payload.get("actorRole"),
              userIdHash=payload.get("actorHash"),
              chaos_scenario=chaos.current_scenario())
    return {"updated": True, "product": snapshot}


@router.get("/internal/stock")
def stock_overview() -> Dict[str, Any]:
    with _lock:
        return {"products": [_public(p) for p in _products],
                "totalUnits": sum(p["stock"] for p in _products)}


def startup() -> None:
    n = load_seed()
    klog.info("catalogue loaded: %d pieces across %d categories"
              % (n, len(_meta.get("categories", []))),
              events.SERVICE_STARTED, productCount=n)
