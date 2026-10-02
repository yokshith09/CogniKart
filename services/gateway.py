"""cognikart-gateway -- the storefront edge and the only public service.

What this service exists to demonstrate: SYMPTOM VERSUS CAUSE. All external
traffic lands here, so 5xx rate and p95 latency spike *here* when the real
fault is two hops away in payments. That gap is what makes "which service is
causing the problem?" a real question, and answering it is what the trace
correlation is for.

It also owns:
  * authentication -- sign-up, sign-in, sign-out, and the session cookie.
    Every auth outcome is logged with a role and a hashed user id, never an
    email, so sign-in activity is visible in OpsMind without leaking PII.
  * authorization -- staff-only routes refuse customers, and every refusal is
    logged as `authz.denied`, which is a genuinely useful security signal.
  * chaos orchestration -- the Scenario Lab, so a presenter triggers one
    named, coherent failure instead of configuring three services by hand.
"""
import asyncio
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Body, Query, Request, Response

from ..common import auth, chaos, downstream, events, logging as klog
from ..common.config import settings

router = APIRouter(tags=["gateway"])


# --- session plumbing ------------------------------------------------------
def _secure_cookie(request: Request) -> bool:
    """Cloud Run terminates TLS upstream, so the app sees http. Trust the
    forwarded header; fall back to the request scheme locally."""
    proto = request.headers.get("x-forwarded-proto", "")
    return (proto or request.url.scheme) == "https"


def _set_session(request: Request, response: Response, user: Dict[str, Any]) -> None:
    response.set_cookie(
        auth.SESSION_COOKIE, auth.issue_session(user),
        max_age=auth.SESSION_TTL_S, httponly=True, samesite="lax",
        secure=_secure_cookie(request), path="/",
    )


def current_user(request: Request) -> Optional[Dict[str, Any]]:
    return auth.read_session(request.cookies.get(auth.SESSION_COOKIE))


def _unauthenticated(request: Request, response: Response, what: str) -> Dict[str, Any]:
    request.state.error_code = events.ErrorCode.UNAUTHENTICATED
    request.state.error_class = "Unauthenticated"
    response.status_code = 401
    klog.warning("unauthenticated request to %s" % what, events.AUTHZ_DENIED,
                 route=what, reason="no valid session",
                 chaos_scenario=chaos.current_scenario())
    return {"error": events.ErrorCode.UNAUTHENTICATED,
            "message": "please sign in to continue"}


def _forbidden(request: Request, response: Response, session: Dict[str, Any],
               what: str) -> Dict[str, Any]:
    request.state.error_code = events.ErrorCode.FORBIDDEN
    request.state.error_class = "Forbidden"
    response.status_code = 403
    klog.warning("role %s denied access to %s" % (session.get("role"), what),
                 events.AUTHZ_DENIED, route=what, actorRole=session.get("role"),
                 userIdHash=auth.user_hash(session.get("uid", "")),
                 reason="staff role required",
                 chaos_scenario=chaos.current_scenario())
    return {"error": events.ErrorCode.FORBIDDEN,
            "message": "this area is for fulfilment staff"}


def _passthrough(request: Request, response: Response, status: int, body: Any) -> Any:
    response.status_code = status
    if isinstance(body, dict) and body.get("error"):
        request.state.error_code = body["error"]
        request.state.error_class = "".join(w.title() for w in str(body["error"]).split("_"))
    return body


# --- authentication --------------------------------------------------------
@router.post("/api/auth/signup")
def signup(request: Request, response: Response,
           payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    email = (payload.get("email") or "").strip()
    name = (payload.get("name") or "").strip()
    user, err = auth.create_user(email, payload.get("password") or "", name)
    if err:
        # The password itself is never passed to the logger; the redaction
        # denylist would strip it anyway.
        klog.warning("sign-up rejected: %s" % err, events.AUTH_SIGNUP_FAILED,
                     errorCode=err, chaos_scenario=chaos.current_scenario())
        request.state.error_code = err
        request.state.error_class = "SignupRejected"
        response.status_code = 409 if err == "EMAIL_TAKEN" else 400
        return {"error": err, "message": {
            "EMAIL_TAKEN": "an account with that email already exists",
            "WEAK_PASSWORD": "please use at least 8 characters",
            "INVALID_EMAIL": "that does not look like an email address",
            "INVALID_NAME": "please tell us your name",
        }.get(err, "could not create that account")}

    _set_session(request, response, user)
    request.state.user_hash = auth.user_hash(user["id"])
    klog.info("account created (%s)" % user["role"], events.AUTH_SIGNUP_OK,
              actorRole=user["role"], userIdHash=auth.user_hash(user["id"]),
              chaos_scenario=chaos.current_scenario())
    return {"user": auth.public_user(user)}


@router.post("/api/auth/signin")
def signin(request: Request, response: Response,
           payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    email = (payload.get("email") or "").strip()
    user, err = auth.authenticate(email, payload.get("password") or "")
    if err:
        # Deliberately does not reveal whether the account exists. The log
        # records which it was, because an operator needs that distinction
        # even though the caller must not have it.
        klog.warning("sign-in failed (%s)" % err, events.AUTH_SIGNIN_FAILED,
                     errorCode=events.ErrorCode.BAD_CREDENTIALS, failureReason=err,
                     chaos_scenario=chaos.current_scenario())
        request.state.error_code = events.ErrorCode.BAD_CREDENTIALS
        request.state.error_class = "BadCredentials"
        response.status_code = 401
        return {"error": events.ErrorCode.BAD_CREDENTIALS,
                "message": "that email and password do not match"}

    _set_session(request, response, user)
    klog.info("signed in (%s)" % user["role"], events.AUTH_SIGNIN_OK,
              actorRole=user["role"], userIdHash=auth.user_hash(user["id"]),
              chaos_scenario=chaos.current_scenario())
    return {"user": auth.public_user(user)}


@router.post("/api/auth/signout")
def signout(request: Request, response: Response) -> Dict[str, Any]:
    session = current_user(request)
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    if session:
        klog.info("signed out (%s)" % session.get("role"), events.AUTH_SIGNOUT,
                  actorRole=session.get("role"),
                  userIdHash=auth.user_hash(session.get("uid", "")),
                  chaos_scenario=chaos.current_scenario())
    return {"signedOut": True}


@router.get("/api/auth/me")
def me(request: Request) -> Dict[str, Any]:
    session = current_user(request)
    if not session:
        return {"user": None}
    user = auth.get_user(session["uid"])
    return {"user": auth.public_user(user) if user else {
        "id": session["uid"], "name": session.get("name"),
        "role": session.get("role"), "userHash": auth.user_hash(session["uid"])}}


@router.get("/api/auth/demo-accounts")
def demo_accounts() -> Dict[str, Any]:
    """Seeded credentials, shown on the sign-in page so a reviewer can get in.
    Safe: this is a demonstration application with no real data behind it."""
    return {"accounts": auth.demo_account_hints(), "kdf": auth.kdf_scheme()}


# --- the collection --------------------------------------------------------
@router.get("/api/collection")
async def collection(request: Request, response: Response) -> Any:
    try:
        status, body, _ms = await downstream.call(request, "catalog", "GET", "/categories")
        return _passthrough(request, response, status, body)
    except downstream.DependencyError as exc:
        request.state.error_code = exc.code
        request.state.error_class = "CatalogUnavailable"
        response.status_code = 502
        return {"error": exc.code, "message": "collection unavailable"}


@router.get("/api/products")
async def products(request: Request, response: Response,
                   category: Optional[str] = None, q: Optional[str] = None) -> Any:
    path = "/products"
    params = []
    if category and category.lower() != "all":
        params.append("category=%s" % category)
    if q:
        params.append("q=%s" % q)
    if params:
        path += "?" + "&".join(params)
    try:
        status, body, _ms = await downstream.call(request, "catalog", "GET", path)
        return _passthrough(request, response, status, body)
    except downstream.DependencyError as exc:
        request.state.dependency = "cognikart-catalog"
        request.state.dependency_latency_ms = exc.latency_ms
        request.state.error_code = exc.code
        request.state.error_class = "CatalogUnavailable"
        response.status_code = 502
        return {"error": exc.code, "message": "the collection is unavailable"}


@router.get("/api/products/{product_id}")
async def product_detail(request: Request, response: Response, product_id: str) -> Any:
    try:
        status, body, _ms = await downstream.call(
            request, "catalog", "GET", "/products/%s" % product_id)
        return _passthrough(request, response, status, body)
    except downstream.DependencyError as exc:
        request.state.error_code = exc.code
        request.state.error_class = "CatalogUnavailable"
        response.status_code = 502
        return {"error": exc.code, "message": "the collection is unavailable"}


# --- orders (signed in) ----------------------------------------------------
@router.post("/api/orders")
async def place_order(request: Request, response: Response,
                      payload: Dict[str, Any] = Body(...)) -> Any:
    session = current_user(request)
    if not session:
        return _unauthenticated(request, response, "/api/orders")
    try:
        status, body, ms = await downstream.call(
            request, "orders", "POST", "/orders",
            json_body={"items": payload.get("items") or [],
                       "userId": session["uid"],
                       "userHash": auth.user_hash(session["uid"])},
            timeout_s=30.0)
        request.state.dependency = "cognikart-orders"
        request.state.dependency_latency_ms = ms
        if isinstance(body, dict):
            request.state.order_id = body.get("orderId")
            request.state.cart_value_inr = body.get("totalInr")
        return _passthrough(request, response, status, body)
    except downstream.DependencyError as exc:
        request.state.dependency = "cognikart-orders"
        request.state.dependency_latency_ms = exc.latency_ms
        request.state.error_code = exc.code
        request.state.error_class = "OrdersUnavailable"
        response.status_code = 502
        return {"error": exc.code, "message": "we could not place that order"}


@router.post("/api/orders/{order_id}/pay")
async def pay(request: Request, response: Response, order_id: str,
              payload: Dict[str, Any] = Body(default_factory=dict)) -> Any:
    session = current_user(request)
    if not session:
        return _unauthenticated(request, response, "/api/orders/{id}/pay")
    try:
        # Long timeout: the shopper may have chosen "slow provider", and orders
        # retries twice. The edge must outlast the work it delegated.
        status, body, ms = await downstream.call(
            request, "orders", "POST", "/orders/%s/pay" % order_id,
            json_body={"outcome": payload.get("outcome")}, timeout_s=60.0)
        request.state.dependency = "cognikart-orders"
        request.state.dependency_latency_ms = ms
        request.state.order_id = order_id
        return _passthrough(request, response, status, body)
    except downstream.DependencyError as exc:
        request.state.dependency = "cognikart-orders"
        request.state.dependency_latency_ms = exc.latency_ms
        request.state.error_code = exc.code
        request.state.error_class = "PaymentUnavailable"
        response.status_code = 502
        return {"error": exc.code, "message": "we could not take payment",
                "orderId": order_id}


@router.post("/api/orders/{order_id}/cancel")
async def cancel(request: Request, response: Response, order_id: str) -> Any:
    session = current_user(request)
    if not session:
        return _unauthenticated(request, response, "/api/orders/{id}/cancel")
    try:
        status, body, _ms = await downstream.call(
            request, "orders", "POST", "/orders/%s/cancel" % order_id,
            json_body={"actorRole": session.get("role"),
                       "actorHash": auth.user_hash(session["uid"])}, timeout_s=30.0)
        request.state.order_id = order_id
        return _passthrough(request, response, status, body)
    except downstream.DependencyError as exc:
        request.state.error_code = exc.code
        request.state.error_class = "OrdersUnavailable"
        response.status_code = 502
        return {"error": exc.code, "message": "could not cancel that order"}


@router.get("/api/orders")
async def my_orders(request: Request, response: Response) -> Any:
    session = current_user(request)
    if not session:
        return _unauthenticated(request, response, "/api/orders")
    try:
        status, body, _ms = await downstream.call(
            request, "orders", "GET", "/orders?userId=%s" % session["uid"])
        return _passthrough(request, response, status, body)
    except downstream.DependencyError as exc:
        request.state.error_code = exc.code
        request.state.error_class = "OrdersUnavailable"
        response.status_code = 502
        return {"error": exc.code, "message": "could not load your orders"}


# --- fulfilment desk (staff only) ------------------------------------------
def _require_staff(request: Request, response: Response, what: str):
    session = current_user(request)
    if not session:
        return None, _unauthenticated(request, response, what)
    if session.get("role") != auth.ROLE_STAFF:
        return None, _forbidden(request, response, session, what)
    return session, None


@router.get("/api/staff/orders")
async def staff_orders(request: Request, response: Response) -> Any:
    session, denied = _require_staff(request, response, "/api/staff/orders")
    if denied is not None:
        return denied
    try:
        status, body, _ms = await downstream.call(request, "orders", "GET", "/orders")
        return _passthrough(request, response, status, body)
    except downstream.DependencyError as exc:
        response.status_code = 502
        return {"error": exc.code, "message": "the fulfilment desk is unavailable"}


@router.post("/api/staff/orders/{order_id}/{action}")
async def staff_order_action(request: Request, response: Response,
                             order_id: str, action: str) -> Any:
    session, denied = _require_staff(
        request, response, "/api/staff/orders/{id}/%s" % action)
    if denied is not None:
        return denied
    if action not in ("fulfil", "cancel"):
        request.state.error_code = events.ErrorCode.VALIDATION_ERROR
        response.status_code = 400
        return {"error": events.ErrorCode.VALIDATION_ERROR, "message": "unknown action"}
    try:
        status, body, _ms = await downstream.call(
            request, "orders", "POST", "/orders/%s/%s" % (order_id, action),
            json_body={"actorRole": session.get("role"),
                       "actorHash": auth.user_hash(session["uid"])}, timeout_s=30.0)
        request.state.order_id = order_id
        return _passthrough(request, response, status, body)
    except downstream.DependencyError as exc:
        response.status_code = 502
        return {"error": exc.code, "message": "could not %s that order" % action}


@router.get("/api/staff/stock")
async def staff_stock(request: Request, response: Response) -> Any:
    session, denied = _require_staff(request, response, "/api/staff/stock")
    if denied is not None:
        return denied
    try:
        status, body, _ms = await downstream.call(
            request, "catalog", "GET", "/internal/stock")
        return _passthrough(request, response, status, body)
    except downstream.DependencyError as exc:
        response.status_code = 502
        return {"error": exc.code, "message": "stock is unavailable"}


@router.post("/api/staff/stock/{what}")
async def staff_stock_update(request: Request, response: Response, what: str,
                             payload: Dict[str, Any] = Body(...)) -> Any:
    session, denied = _require_staff(request, response, "/api/staff/stock/%s" % what)
    if denied is not None:
        return denied
    if what not in ("level", "availability"):
        request.state.error_code = events.ErrorCode.VALIDATION_ERROR
        response.status_code = 400
        return {"error": events.ErrorCode.VALIDATION_ERROR, "message": "unknown action"}
    body_out = dict(payload)
    body_out["actorRole"] = session.get("role")
    body_out["actorHash"] = auth.user_hash(session["uid"])
    try:
        status, body, _ms = await downstream.call(
            request, "catalog", "POST", "/internal/stock/%s" % what,
            json_body=body_out)
        return _passthrough(request, response, status, body)
    except downstream.DependencyError as exc:
        response.status_code = 502
        return {"error": exc.code, "message": "could not update stock"}


# --- Scenario Lab ----------------------------------------------------------
async def _post_admin(role: str, path: str,
                      body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    base = settings.downstream(role)
    if not base:
        return {"service": role, "ok": False, "error": "no URL configured"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(base.rstrip("/") + path, json=body or {})
        return {"service": role, "ok": resp.status_code < 400, "response": resp.json()}
    except Exception as exc:
        return {"service": role, "ok": False, "error": str(exc)}


async def _get_admin(role: str, path: str) -> Dict[str, Any]:
    base = settings.downstream(role)
    if not base:
        return {"service": role, "ok": False, "error": "no URL configured"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(base.rstrip("/") + path)
        return {"service": role, "ok": resp.status_code < 400, "response": resp.json()}
    except Exception as exc:
        return {"service": role, "ok": False, "error": str(exc)}


@router.get("/api/admin/chaos/scenarios")
def list_scenarios() -> Dict[str, Any]:
    return {"scenarios": chaos.scenario_catalog()}


@router.get("/api/admin/chaos/state")
async def chaos_state() -> Dict[str, Any]:
    results = await asyncio.gather(
        *[_get_admin(r, "/admin/chaos") for r in ("catalog", "orders", "payments")])
    return {"gateway": chaos.state().as_dict(), "services": results}


@router.post("/api/admin/chaos/scenario")
async def apply_scenario(payload: Dict[str, Any] = Body(default_factory=dict)) -> Dict[str, Any]:
    name = (payload.get("name") or "").strip()
    spec = chaos.SCENARIOS.get(name)
    if not spec:
        return {"error": "UNKNOWN_SCENARIO", "known": sorted(chaos.SCENARIOS.keys())}

    if name == "recover-all":
        results = await asyncio.gather(
            *[_post_admin(r, "/admin/chaos/reset") for r in ("catalog", "orders", "payments")])
        chaos.clear()
        klog.info("recover-all applied: every service back to baseline",
                  events.CHAOS_CLEARED)
        return {"scenario": name, "applied": True, "results": results}

    duration = int(payload.get("durationS") or spec["defaultDurationS"] or 600)
    tasks = []
    for role, knobs in spec["targets"].items():
        body = dict(knobs)
        body["scenario"] = name
        body["durationS"] = duration
        tasks.append(_post_admin(role, "/admin/chaos", body))
    results = await asyncio.gather(*tasks) if tasks else []
    chaos.apply({"scenario": name, "durationS": duration})
    klog.warning("scenario '%s' orchestrated for %ds" % (name, duration),
                 events.CHAOS_APPLIED, chaosScenario=name, chaosDurationS=duration)
    return {"scenario": name, "applied": True, "durationS": duration,
            "description": spec["description"],
            "loadgenProfile": spec.get("loadgenProfile"), "results": results}


def startup() -> None:
    n = auth.seed_demo_accounts()
    klog.info("gateway ready (seeded %d demo account(s), kdf=%s)"
              % (n, auth.kdf_scheme()), events.SERVICE_STARTED,
              seededAccounts=n, kdf=auth.kdf_scheme())
