"""CogniKart traffic generator.

Produces realistic shopper behaviour against the CogniKart storefront so the
OpsMind portal has genuine telemetry to display. Not a load test: the point is
a believable traffic SHAPE, not maximum throughput.

Behaviour model (weighted journeys, see docs/DEMO_SCRIPT.md):
    60%  browser       list -> 2-4 product views -> leave
    20%  abandoner     list -> view -> add to cart -> leave
    15%  buyer         list -> view -> cart -> CHECKOUT -> paid
     5%  bulk buyer    multi-SKU cart -> CHECKOUT

Plus a ~3% deliberate client-error floor (bad product ids, malformed carts,
empty-cart checkouts). Without it the 4xx panel is a flat zero line and the
portal cannot demonstrate that it separates client mistakes from server faults.

SAFETY: --duration and --max-requests are both enforced, and the generator
refuses to run unbounded. Runaway traffic is the single most likely cause of
an unexpected Cloud Logging bill, so the ceilings are not optional.
"""
import argparse
import asyncio
import hashlib
import random
import sys
import time
import uuid
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

import httpx

PROFILES = {
    "trickle": {"rps": 0.3, "description": "background baseline; safe to leave running"},
    "normal": {"rps": 4.0, "description": "typical shopping traffic"},
    "spike": {"rps": 12.0, "description": "3x surge; drives instance scale-up and cost"},
    "browse-only": {"rps": 6.0, "description": "catalog reads only; no checkouts"},
    "checkout-heavy": {"rps": 3.0, "description": "high checkout mix; maximises incident signal"},
}

JOURNEY_WEIGHTS = [
    ("browser", 60),
    ("abandoner", 20),
    ("buyer", 15),
    ("bulk", 5),
]
CLIENT_ERROR_RATE = 0.03


class Stats:
    def __init__(self) -> None:
        self.by_status: Counter = Counter()
        self.by_journey: Counter = Counter()
        self.errors: Counter = Counter()
        self.latencies: List[float] = []
        self.checkouts_ok = 0
        self.checkouts_failed = 0
        self.cancelled = 0
        self.outcomes: Counter = Counter()
        self.revenue_ok = 0.0
        self.revenue_lost = 0.0
        self.requests = 0
        self.started = time.time()

    def record(self, status: int, latency_s: float) -> None:
        self.requests += 1
        self.by_status[status] += 1
        self.latencies.append(latency_s * 1000)

    def summary(self) -> str:
        el = max(time.time() - self.started, 0.001)
        lat = sorted(self.latencies)
        p50 = lat[len(lat) // 2] if lat else 0
        p95 = lat[int(len(lat) * 0.95) - 1] if len(lat) > 1 else p50
        ok = sum(v for k, v in self.by_status.items() if 200 <= k < 400)
        c4 = sum(v for k, v in self.by_status.items() if 400 <= k < 500)
        c5 = sum(v for k, v in self.by_status.items() if k >= 500)
        settled = self.checkouts_ok + self.checkouts_failed
        lines = [
            "",
            "=" * 66,
            "  requests      %d in %.0fs  (%.2f req/s)" % (self.requests, el, self.requests / el),
            "  responses     2xx/3xx=%d  4xx=%d  5xx=%d" % (ok, c4, c5),
            "  latency       p50=%.0fms  p95=%.0fms" % (p50, p95),
            "  checkouts     confirmed=%d  failed=%d  cancelled=%d  success=%s" % (
                self.checkouts_ok, self.checkouts_failed, self.cancelled,
                ("%.1f%%" % (100.0 * self.checkouts_ok / settled)) if settled else "n/a"),
            "  revenue       captured=INR %.2f   at risk=INR %.2f" % (
                self.revenue_ok, self.revenue_lost),
        ]
        if self.outcomes:
            lines.append("  pay outcomes  " + "  ".join(
                "%s=%d" % (k, v) for k, v in self.outcomes.most_common()))
        if self.by_journey:
            lines.append("  journeys      " + "  ".join(
                "%s=%d" % (k, v) for k, v in self.by_journey.most_common()))
        if self.errors:
            lines.append("  error codes   " + "  ".join(
                "%s=%d" % (k, v) for k, v in self.errors.most_common(6)))
        lines.append("=" * 66)
        return "\n".join(lines)


def user_hash(session: str) -> str:
    # Salted hash: the generator never sends a real identifier, mirroring the
    # redaction policy enforced inside CogniKart itself.
    return hashlib.sha256(("cognikart-demo-salt|" + session).encode()).hexdigest()[:16]


def pick_journey() -> str:
    total = sum(w for _, w in JOURNEY_WEIGHTS)
    r = random.uniform(0, total)
    acc = 0
    for name, w in JOURNEY_WEIGHTS:
        acc += w
        if r <= acc:
            return name
    return "browser"


class Shopper:
    """One shopper session against the storefront.

    Sessions are real: the generator signs in through /api/auth/signin and
    carries the cookie, because orders require authentication. Earlier versions
    posted to /api/cart and /api/checkout, which stopped existing when accounts
    were added -- the result was traffic that browsed happily and never placed
    a single order. Keeping this aligned with the gateway's actual routes
    matters more than it looks: no orders means no payments, which means no
    incident to detect.
    """

    def __init__(self, base: str, client: httpx.AsyncClient, stats: Stats,
                 catalog_cache: List[Dict[str, Any]], cookies: Any) -> None:
        self.base = base.rstrip("/")
        self.client = client
        self.stats = stats
        self.catalog = catalog_cache
        self.cookies = cookies
        self.session = "sess_" + uuid.uuid4().hex[:10]
        self.headers = {
            "X-Session-Id": self.session,
            "User-Agent": "cognikart-loadgen/2.0",
        }

    async def _req(self, method: str, path: str,
                   json_body: Optional[Dict[str, Any]] = None,
                   timeout: float = 30.0,
                   authed: bool = True) -> Tuple[int, Optional[Dict[str, Any]]]:
        started = time.time()
        try:
            resp = await self.client.request(
                method, self.base + path, json=json_body, headers=self.headers,
                cookies=self.cookies if authed else None, timeout=timeout)
        except Exception:
            self.stats.record(0, time.time() - started)
            self.stats.errors["CLIENT_TIMEOUT"] += 1
            return 0, None
        self.stats.record(resp.status_code, time.time() - started)
        try:
            body = resp.json()
        except Exception:
            return resp.status_code, None
        if isinstance(body, dict) and body.get("error"):
            self.stats.errors[body["error"]] += 1
        return resp.status_code, body if isinstance(body, dict) else None

    async def browse(self) -> List[Dict[str, Any]]:
        category = random.choice([None] + CATEGORIES)
        path = "/api/products" + (f"?category={category}" if category else "")
        _s, body = await self._req("GET", path)
        products = (body or {}).get("products") or []
        if products:
            self.catalog[:] = [p for p in products if p.get("stock", 0) > 2][:40] or self.catalog
        return products

    async def view_some(self, products: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        viewed = []
        for p in random.sample(products, min(len(products), random.randint(2, 4))):
            await self._req("GET", f"/api/products/{p['id']}")
            viewed.append(p)
        return viewed

    async def place_and_pay(self, picks: List[Dict[str, Any]]) -> None:
        """The two-phase flow: place reserves stock, pay is a separate call."""
        items = [{"id": p["id"], "qty": random.randint(1, 2),
                  "priceInr": p["priceInr"]} for p in picks]
        value = sum(i["priceInr"] * i["qty"] for i in items)

        status, body = await self._req("POST", "/api/orders", {"items": items}, timeout=45.0)
        if status != 200 or not body or not body.get("orderId"):
            self.stats.checkouts_failed += 1
            self.stats.revenue_lost += value
            return
        order_id = body["orderId"]

        # Most shoppers pay normally. A minority hit a provider problem, which
        # gives the platform a natural error floor without any chaos injected.
        outcome = random.choices(
            ("approved", "rejected", "slow"), weights=(86, 10, 4))[0]
        self.stats.outcomes[outcome] += 1

        status, body = await self._req(
            "POST", f"/api/orders/{order_id}/pay", {"outcome": outcome}, timeout=60.0)
        if status == 200 and body and body.get("status") == "PAID":
            self.stats.checkouts_ok += 1
            self.stats.revenue_ok += body.get("totalInr") or value
            return

        self.stats.checkouts_failed += 1
        self.stats.revenue_lost += value
        # A shopper who cannot pay usually gives up and releases the stock.
        if random.random() < 0.5:
            await self._req("POST", f"/api/orders/{order_id}/cancel")
            self.stats.cancelled += 1

    async def client_error(self) -> None:
        """Deliberate client mistakes, so the 4xx series is never a flat zero."""
        which = random.choice(["bad-sku", "empty-order", "over-stock", "no-session"])
        if which == "bad-sku":
            await self._req("GET", f"/api/products/FW-NOPE-{random.randint(10,99)}")
        elif which == "empty-order":
            await self._req("POST", "/api/orders", {"items": []})
        elif which == "over-stock" and self.catalog:
            p = random.choice(self.catalog)
            # Slightly more than exists, not an absurd quantity. qty=9999 was
            # producing orders worth lakhs, which made revenue-at-risk
            # meaningless -- a figure is only useful if it is plausible.
            qty = int(p.get("stock", 5)) + random.randint(1, 4)
            await self._req("POST", "/api/orders",
                            {"items": [{"id": p["id"], "qty": qty,
                                        "priceInr": p["priceInr"]}]})
        else:
            # No cookie: exercises the 401 path and logs an authz event.
            await self._req("POST", "/api/orders",
                            {"items": [{"id": "FW-KNIT-01", "qty": 1, "priceInr": 138}]},
                            authed=False)

    async def run(self, journey: str, allow_checkout: bool) -> None:
        self.stats.by_journey[journey] += 1
        if random.random() < CLIENT_ERROR_RATE:
            await self.client_error()
            return

        products = await self.browse()
        if not products:
            return
        viewed = await self.view_some(products)
        if journey == "browser" or not viewed:
            return

        in_stock = [p for p in viewed if p.get("stock", 0) > 2] or viewed
        if journey == "abandoner" or not allow_checkout:
            return

        if journey == "bulk":
            pool = [p for p in (self.catalog or in_stock) if p.get("stock", 0) > 2]
            picks = random.sample(pool, min(len(pool), random.randint(2, 3))) or in_stock[:1]
        else:
            picks = [random.choice(in_stock)]
        await self.place_and_pay(picks)


CATEGORIES = ["Outerwear", "Knitwear", "Shirting", "Trousers", "Dresses", "Accessories"]

DEMO_ACCOUNT = {"email": "customer@cognikart.demo", "password": "ShopPass2026!"}


async def sign_in(base: str, client: httpx.AsyncClient) -> Optional[Any]:
    """Authenticate once and reuse the session cookie.

    Orders require a signed-in session, so without this the generator produces
    browsing traffic and a wall of 401s.
    """
    try:
        resp = await client.post(
            base.rstrip("/") + "/api/auth/signin", json=DEMO_ACCOUNT, timeout=30.0)
    except Exception as exc:
        print("  ! could not reach the storefront to sign in: %s" % exc)
        return None
    if resp.status_code != 200:
        print("  ! sign-in failed (HTTP %d). Orders will not be placed; "
              "browsing traffic only." % resp.status_code)
        return None
    user = (resp.json() or {}).get("user") or {}
    print("  signed in as %s (%s)" % (user.get("name", "?"), user.get("role", "?")))
    return resp.cookies


async def run_load(base: str, profile: str, duration_s: float,
                   max_requests: int, quiet: bool) -> Stats:
    spec = PROFILES[profile]
    rps = spec["rps"]
    stats = Stats()
    catalog_cache: List[Dict[str, Any]] = []
    allow_checkout = profile != "browse-only"
    deadline = time.time() + duration_s
    inflight: set = set()
    last_report = time.time()

    limits = httpx.Limits(max_connections=40, max_keepalive_connections=20)
    async with httpx.AsyncClient(limits=limits) as client:
        cookies = await sign_in(base, client)
        while time.time() < deadline and stats.requests < max_requests:
            journey = "buyer" if profile == "checkout-heavy" and random.random() < 0.6 \
                else pick_journey()
            shopper = Shopper(base, client, stats, catalog_cache, cookies)
            task = asyncio.ensure_future(shopper.run(journey, allow_checkout))
            inflight.add(task)
            task.add_done_callback(inflight.discard)

            await asyncio.sleep(max(0.01, random.expovariate(rps)))

            if not quiet and time.time() - last_report >= 10:
                el = time.time() - stats.started
                sys.stdout.write(
                    "  [%4.0fs] %5d requests  %4.1f req/s  checkouts ok=%d failed=%d\n"
                    % (el, stats.requests, stats.requests / max(el, 0.001),
                       stats.checkouts_ok, stats.checkouts_failed))
                sys.stdout.flush()
                last_report = time.time()

        if inflight:
            if not quiet:
                sys.stdout.write("  draining %d in-flight journeys...\n" % len(inflight))
            await asyncio.wait(list(inflight), timeout=60)
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(
        description="CogniKart traffic generator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="profiles:\n" + "\n".join(
            "  %-15s %-5s req/s  %s" % (k, v["rps"], v["description"])
            for k, v in PROFILES.items()),
    )
    ap.add_argument("--url", default="http://127.0.0.1:8080",
                    help="CogniKart gateway base URL")
    ap.add_argument("--profile", default="normal", choices=sorted(PROFILES))
    ap.add_argument("--duration", type=float, default=120,
                    help="seconds to run (hard ceiling, required)")
    ap.add_argument("--max-requests", type=int, default=20000,
                    help="hard request ceiling; protects the log budget")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if args.duration <= 0 or args.duration > 3600:
        ap.error("--duration must be between 1 and 3600 seconds")
    if args.max_requests <= 0 or args.max_requests > 200000:
        ap.error("--max-requests must be between 1 and 200000")

    spec = PROFILES[args.profile]
    print("CogniKart loadgen -> %s" % args.url)
    print("  profile  %s (%s, ~%.1f req/s)"
          % (args.profile, spec["description"], spec["rps"]))
    print("  ceilings %.0fs duration, %d requests" % (args.duration, args.max_requests))
    print()

    # asyncio.run rather than get_event_loop(): the latter is deprecated from
    # Python 3.10 and warns loudly on Cloud Shell's 3.12.
    stats = asyncio.run(
        run_load(args.url, args.profile, args.duration, args.max_requests, args.quiet))
    print(stats.summary())
    return 0


if __name__ == "__main__":
    sys.exit(main())
