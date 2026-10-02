/* CogniKart storefront.

   A thin shell over the APIs the gateway exposes. Its job is not to be a shop
   -- it is to make the telemetry pipeline visible. Every action here produces
   structured logs that reach Cloud Logging, and the trace id in the ribbon at
   the foot of the page can be pasted into OpsMind to see that same action fan
   out across four services.

   Garments are drawn as inline SVG so there are no image files to host, no
   external requests and nothing that can fail to load. */
(() => {
"use strict";

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (n) => "$" + Number(n || 0).toLocaleString("en-US",
  { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const when = (ts) => new Date(ts * 1000).toLocaleString();

const state = {
  view: "shop", user: null, products: [], categories: [], category: "all",
  q: "", cart: [], pendingOrder: null, session: null,
  lastTrace: null, lastReq: null, authTab: "signin",
};

/* ---------- garment illustrations ---------- */
const INK = "#1c3329";
const ART = {
  coat: (c) => `<path d="M34 30 L50 22 L64 30 L64 92 L36 92 Z" fill="${c}" stroke="${INK}" stroke-width="2"/>
    <path d="M34 30 L22 36 L18 86 L30 88 L36 46" fill="${c}" stroke="${INK}" stroke-width="2"/>
    <path d="M66 30 L78 36 L82 86 L70 88 L64 46" fill="${c}" stroke="${INK}" stroke-width="2"/>
    <path d="M50 22 L50 92" stroke="${INK}" stroke-width="1.6"/>
    <path d="M50 22 L41 38 M50 22 L59 38" stroke="${INK}" stroke-width="1.6" fill="none"/>
    <circle cx="54" cy="46" r="1.8" fill="${INK}"/><circle cx="54" cy="60" r="1.8" fill="${INK}"/>`,
  trench: (c) => `<path d="M33 30 L50 22 L67 30 L67 90 L33 90 Z" fill="${c}" stroke="${INK}" stroke-width="2"/>
    <path d="M33 30 L21 37 L17 80 L29 83" fill="${c}" stroke="${INK}" stroke-width="2"/>
    <path d="M67 30 L79 37 L83 80 L71 83" fill="${c}" stroke="${INK}" stroke-width="2"/>
    <rect x="30" y="58" width="40" height="5" fill="${INK}" opacity=".75"/>
    <path d="M50 22 L50 58 M44 30 L44 56 M56 30 L56 56" stroke="${INK}" stroke-width="1.5" fill="none"/>`,
  knit: (c) => `<path d="M32 34 L50 26 L68 34 L68 86 L32 86 Z" fill="${c}" stroke="${INK}" stroke-width="2"/>
    <path d="M32 34 L18 42 L24 70 L34 66" fill="${c}" stroke="${INK}" stroke-width="2"/>
    <path d="M68 34 L82 42 L76 70 L66 66" fill="${c}" stroke="${INK}" stroke-width="2"/>
    <path d="M41 28 Q50 36 59 28" fill="none" stroke="${INK}" stroke-width="2"/>
    <path d="M40 46 v30 M50 44 v32 M60 46 v30" stroke="${INK}" stroke-width="1.1" opacity=".45" fill="none"/>
    <rect x="32" y="80" width="36" height="6" fill="${INK}" opacity=".18"/>`,
  shirt: (c) => `<path d="M34 32 L50 26 L66 32 L66 86 L34 86 Z" fill="${c}" stroke="${INK}" stroke-width="2"/>
    <path d="M34 32 L20 40 L26 62 L34 58" fill="${c}" stroke="${INK}" stroke-width="2"/>
    <path d="M66 32 L80 40 L74 62 L66 58" fill="${c}" stroke="${INK}" stroke-width="2"/>
    <path d="M43 26 L50 38 L57 26" fill="${c}" stroke="${INK}" stroke-width="2"/>
    <path d="M50 38 L50 86" stroke="${INK}" stroke-width="1.4"/>
    <circle cx="50" cy="50" r="1.7" fill="${INK}"/><circle cx="50" cy="62" r="1.7" fill="${INK}"/>
    <circle cx="50" cy="74" r="1.7" fill="${INK}"/>`,
  trouser: (c) => `<path d="M36 24 h28 v12 l-3 54 h-9 l-2-40 -2 40 h-9 l-3-54 Z" fill="${c}" stroke="${INK}" stroke-width="2"/>
    <rect x="36" y="24" width="28" height="7" fill="${INK}" opacity=".25"/>
    <path d="M50 36 v50" stroke="${INK}" stroke-width="1.1" opacity=".5"/>
    <rect x="56" y="26" width="5" height="3.5" fill="${INK}" opacity=".6"/>`,
  denim: (c) => `<path d="M35 24 h30 v11 l-2 55 h-10 l-3-39 -3 39 h-10 l-2-55 Z" fill="${c}" stroke="${INK}" stroke-width="2"/>
    <rect x="35" y="24" width="30" height="8" fill="${INK}" opacity=".22"/>
    <path d="M39 38 q4 5 8 0 M53 38 q4 5 8 0" fill="none" stroke="#e8e2d4" stroke-width="1.5"/>
    <path d="M37 64 h26" stroke="#b5791d" stroke-width="1" opacity=".55"/>`,
  dress: (c) => `<path d="M38 28 L50 24 L62 28 L70 88 L30 88 Z" fill="${c}" stroke="${INK}" stroke-width="2"/>
    <path d="M38 28 L28 36 L33 50" fill="${c}" stroke="${INK}" stroke-width="2"/>
    <path d="M62 28 L72 36 L67 50" fill="${c}" stroke="${INK}" stroke-width="2"/>
    <path d="M42 26 Q50 32 58 26" fill="none" stroke="${INK}" stroke-width="2"/>
    <path d="M34 60 h32" stroke="${INK}" stroke-width="1" opacity=".3"/>`,
  scarf: (c) => `<path d="M30 24 q20 12 40 0 l6 12 q-26 14 -52 0 Z" fill="${c}" stroke="${INK}" stroke-width="2"/>
    <path d="M36 38 l4 44 h14 l4 -44" fill="${c}" stroke="${INK}" stroke-width="2"/>
    <path d="M38 82 v6 M43 82 v7 M48 82 v6 M53 82 v7" stroke="${INK}" stroke-width="1.5"/>`,
  tote: (c) => `<path d="M30 40 h40 l-4 48 h-32 Z" fill="${c}" stroke="${INK}" stroke-width="2"/>
    <path d="M40 40 v-8 a10 10 0 0 1 20 0 v8" fill="none" stroke="${INK}" stroke-width="3"/>
    <rect x="44" y="60" width="12" height="9" fill="${INK}" opacity=".55"/>`,
  boot: (c) => `<path d="M38 26 h18 v34 l12 10 v12 H34 V60 Z" fill="${c}" stroke="${INK}" stroke-width="2"/>
    <rect x="32" y="78" width="38" height="6" fill="${INK}" opacity=".8"/>
    <path d="M56 32 v24" stroke="${INK}" stroke-width="1.2" opacity=".5"/>
    <ellipse cx="60" cy="40" rx="3" ry="8" fill="${INK}" opacity=".3"/>`,
};
const TONE = {
  coat: "#3f4a3a", trench: "#b08a52", knit: "#4f7238", shirt: "#dbe4ef",
  trouser: "#8d8f79", denim: "#4a6076", dress: "#8d5c6d", scarf: "#a4552f",
  tote: "#b9794a", boot: "#2f3b36",
};
function art(kind) {
  const draw = ART[kind] || ART.knit;
  return `<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">${draw(TONE[kind] || "#4f7238")}</svg>`;
}

/* ---------- telemetry ribbon ---------- */
function sessionId() {
  let s = null;
  try { s = sessionStorage.getItem("ck_sid"); } catch (e) { /* private mode */ }
  if (!s) {
    s = "sess_" + Math.random().toString(36).slice(2, 10);
    try { sessionStorage.setItem("ck_sid", s); } catch (e) { /* ignore */ }
  }
  return s;
}
function setObs(event, kind) {
  $("#obsSession").textContent = state.session;
  const t = $("#obsTrace");
  t.textContent = state.lastTrace ? state.lastTrace.slice(0, 18) + "…" : "—";
  t.dataset.full = state.lastTrace || "";
  $("#obsReq").textContent = state.lastReq || "—";
  const ev = $("#obsEvent");
  ev.textContent = event;
  ev.className = "ev" + (kind ? " " + kind : "");
}
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg; t.classList.add("on");
  clearTimeout(t._t); t._t = setTimeout(() => t.classList.remove("on"), 2000);
}

/* Every response carries X-Trace-Id and X-Request-Id from the shared
   middleware. Surfacing them is what lets a click here be found there. */
async function call(method, path, body) {
  const res = await fetch(path, {
    method,
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-Session-Id": state.session },
    body: body ? JSON.stringify(body) : undefined,
  });
  state.lastTrace = res.headers.get("X-Trace-Id") || state.lastTrace;
  state.lastReq = res.headers.get("X-Request-Id") || state.lastReq;
  let data = null;
  try { data = await res.json(); } catch (e) { /* non-JSON */ }
  return { ok: res.ok, status: res.status, data: data || {} };
}

/* ---------- navigation ---------- */
function show(view) {
  state.view = view;
  $$(".view").forEach(v => v.classList.remove("on"));
  $("#v-" + view).classList.add("on");
  $$("#nav button").forEach(b => b.classList.toggle("on", b.dataset.view === view));
  window.scrollTo({ top: 0, behavior: "instant" });
  if (view === "orders") loadOrders();
  if (view === "desk") loadDesk();
  if (view === "lab") loadScenarios();
  if (view === "cart") paintCart();
}
$$("#nav button").forEach(b => b.addEventListener("click", () => show(b.dataset.view)));

/* ---------- auth ---------- */
function paintUser() {
  const u = state.user;
  $("#who").hidden = !u;
  $("#navDesk").hidden = !(u && u.role === "staff");
  if (u) {
    $("#who").textContent = `${u.name} · ${u.role}`;
    $("#authBtn").textContent = "Sign out";
  } else {
    $("#authBtn").textContent = "Sign in";
  }
}
$("#authBtn").addEventListener("click", async () => {
  if (state.user) {
    await call("POST", "/api/auth/signout");
    state.user = null; state.cart = []; state.pendingOrder = null;
    paintUser(); paintCartPip(); setObs("auth.signout", "ok");
    toast("Signed out"); show("shop");
  } else { show("auth"); }
});
$$(".tabs button").forEach(b => b.addEventListener("click", () => {
  state.authTab = b.dataset.tab;
  $$(".tabs button").forEach(x => x.classList.toggle("on", x === b));
  $("#nameField").hidden = state.authTab !== "signup";
  $("#authSubmit").textContent = state.authTab === "signup" ? "Create account" : "Sign in";
  $("#authErr").innerHTML = "";
}));
$("#authSubmit").addEventListener("click", submitAuth);
$("#aPass").addEventListener("keydown", e => { if (e.key === "Enter") submitAuth(); });

async function submitAuth() {
  const btn = $("#authSubmit");
  const payload = { email: $("#aEmail").value.trim(), password: $("#aPass").value };
  if (state.authTab === "signup") payload.name = $("#aName").value.trim();
  btn.disabled = true; btn.innerHTML = '<span class="spin"></span>';
  const r = await call("POST", state.authTab === "signup"
    ? "/api/auth/signup" : "/api/auth/signin", payload);
  btn.disabled = false;
  btn.textContent = state.authTab === "signup" ? "Create account" : "Sign in";

  if (!r.ok) {
    $("#authErr").innerHTML = `<div class="notice bad">${esc(r.data.message || "Could not sign you in.")}</div>`;
    setObs(state.authTab === "signup" ? "auth.signup.failed" : "auth.signin.failed", "err");
    return;
  }
  state.user = r.data.user;
  $("#authErr").innerHTML = ""; $("#aPass").value = "";
  paintUser();
  setObs(state.authTab === "signup" ? "auth.signup.ok" : "auth.signin.ok", "ok");
  toast(`Signed in as ${state.user.name}`);
  show(state.cart.length ? "cart" : "shop");
}

async function loadDemoAccounts() {
  const r = await call("GET", "/api/auth/demo-accounts");
  const accts = r.data.accounts || [];
  $("#kdfNote").textContent = `Password hashing: ${r.data.kdf || "—"} · sessions signed with HMAC-SHA256`;
  $("#acctRows").innerHTML =
    `<tr><th class="label">Role</th><th class="label">Email</th><th class="label">Password</th><th></th></tr>` +
    accts.map((a, i) => `<tr>
      <td style="text-transform:capitalize">${esc(a.role)}</td>
      <td class="m">${esc(a.email)}</td>
      <td class="m">${esc(a.password)}</td>
      <td><button class="btn-line" data-use="${i}">Use</button></td>
    </tr>`).join("");
  $$("#acctRows [data-use]").forEach(b => b.addEventListener("click", () => {
    const a = accts[+b.dataset.use];
    state.authTab = "signin";
    $$(".tabs button").forEach(x => x.classList.toggle("on", x.dataset.tab === "signin"));
    $("#nameField").hidden = true;
    $("#authSubmit").textContent = "Sign in";
    $("#aEmail").value = a.email; $("#aPass").value = a.password;
  }));
}

/* ---------- shop ---------- */
async function loadCollection() {
  const r = await call("GET", "/api/collection");
  state.categories = r.data.categories || [];
  $("#heroStat").textContent =
    `${r.data.productCount || 0} pieces. Honest stock counts.`;
  $("#chips").innerHTML =
    `<button class="chip on" data-cat="all">All</button>` +
    state.categories.map(c => `<button class="chip" data-cat="${esc(c)}">${esc(c)}</button>`).join("");
  $$("#chips .chip").forEach(b => b.addEventListener("click", () => {
    state.category = b.dataset.cat;
    $$("#chips .chip").forEach(x => x.classList.toggle("on", x === b));
    loadProducts();
  }));
}

let qTimer;
$("#q").addEventListener("input", () => {
  clearTimeout(qTimer);
  qTimer = setTimeout(() => { state.q = $("#q").value.trim(); loadProducts(); }, 320);
});

async function loadProducts() {
  const p = new URLSearchParams();
  if (state.category && state.category !== "all") p.set("category", state.category);
  if (state.q) p.set("q", state.q);
  const r = await call("GET", "/api/products" + (p.toString() ? "?" + p : ""));
  if (!r.ok) {
    $("#grid").innerHTML = "";
    $("#shopEmpty").hidden = false;
    $("#shopEmpty").textContent = "The collection is unavailable right now — check OpsMind.";
    setObs("catalog unavailable", "err");
    return;
  }
  state.products = r.data.products || [];
  setObs(`catalog.product.list · ${state.products.length}`, "ok");
  paintGrid();
}

function paintGrid() {
  $("#shopEmpty").hidden = state.products.length > 0;
  $("#grid").innerHTML = state.products.map(p => {
    const cls = p.stock === 0 ? "out" : p.stock < 10 ? "low" : "";
    const txt = p.stock === 0 ? "Out of stock"
      : p.stock < 10 ? `Only ${p.stock} left` : `${p.stock} in stock`;
    return `<article class="piece">
      <div class="art">${art(p.art)}</div>
      <div class="info">
        <div class="meta">${esc(p.category)} · ${esc(p.id)}</div>
        <h3>${esc(p.name)}</h3>
        <div class="blurb">${esc(p.blurb)}</div>
        <div class="pricing">
          <span class="price">${money(p.priceInr)}</span>
          <span class="stock-tag ${cls}">${txt}</span>
        </div>
        <button class="btn-solid" data-add="${esc(p.id)}" ${p.stock === 0 ? "disabled" : ""}>
          ${p.stock === 0 ? "Unavailable" : "Add to order"}</button>
      </div>
    </article>`;
  }).join("");
  $$("#grid [data-add]").forEach(b => b.addEventListener("click", () => addToCart(b.dataset.add, b)));
}

async function addToCart(id, btn) {
  const p = state.products.find(x => x.id === id);
  if (!p) return;
  const old = btn.textContent;
  btn.disabled = true; btn.innerHTML = '<span class="spin"></span>';
  // Also fetches the detail, so one click is a realistic two-call sequence.
  await call("GET", `/api/products/${encodeURIComponent(id)}`);
  btn.disabled = false; btn.textContent = old;

  const line = state.cart.find(l => l.id === id);
  if (line) line.qty += 1;
  else state.cart.push({ id: p.id, name: p.name, art: p.art, priceInr: p.priceInr,
                         qty: 1, stock: p.stock, category: p.category });
  // A new basket invalidates any order already awaiting payment.
  state.pendingOrder = null;
  paintCartPip(); setObs("cart.item.added", "ok"); toast(`${p.name} added`);
}

function paintCartPip() {
  const n = state.cart.reduce((s, l) => s + l.qty, 0);
  const pip = $("#cartPip");
  pip.hidden = n === 0; pip.textContent = n;
}

/* ---------- cart & payment ---------- */
function cartTotal() { return state.cart.reduce((s, l) => s + l.priceInr * l.qty, 0); }

function paintCart() {
  const rows = $("#cartRows"), sum = $("#cartSummary");
  if (!state.cart.length) {
    rows.innerHTML = `<div class="empty">Your order is empty.</div>`;
    sum.innerHTML = `<div class="label">Summary</div><div class="empty" style="padding:30px 0">Nothing to total.</div>`;
    return;
  }
  rows.innerHTML = state.cart.map(l => `
    <div class="row-item">
      <div class="thumb-sm">${art(l.art)}</div>
      <div class="row-main">
        <div class="meta label">${esc(l.category)} · ${esc(l.id)}</div>
        <h3>${esc(l.name)}</h3>
        <div class="label" style="margin-top:4px">${money(l.priceInr)} each · ${l.stock} available</div>
      </div>
      <div class="qty">
        <button data-dec="${esc(l.id)}">−</button><span>${l.qty}</span>
        <button data-inc="${esc(l.id)}">+</button>
      </div>
      <strong style="min-width:92px;text-align:right">${money(l.priceInr * l.qty)}</strong>
      <button class="btn-line danger" data-rm="${esc(l.id)}">Remove</button>
    </div>`).join("");

  $$("#cartRows [data-inc]").forEach(b => b.addEventListener("click", () => {
    const l = state.cart.find(x => x.id === b.dataset.inc);
    l.qty++; state.pendingOrder = null; paintCart(); paintCartPip();
  }));
  $$("#cartRows [data-dec]").forEach(b => b.addEventListener("click", () => {
    const l = state.cart.find(x => x.id === b.dataset.dec);
    l.qty--; if (l.qty <= 0) state.cart = state.cart.filter(x => x.id !== l.id);
    state.pendingOrder = null; paintCart(); paintCartPip();
  }));
  $$("#cartRows [data-rm]").forEach(b => b.addEventListener("click", () => {
    state.cart = state.cart.filter(x => x.id !== b.dataset.rm);
    state.pendingOrder = null; paintCart(); paintCartPip();
  }));

  const po = state.pendingOrder;
  sum.innerHTML = `<div class="label">Summary</div>
    ${state.cart.map(l => `<div class="sum-row"><span>${esc(l.name)} × ${l.qty}</span>
       <span>${money(l.priceInr * l.qty)}</span></div>`).join("")}
    <div class="sum-total"><span style="font-size:20px;font-weight:700">Total</span>
      <span class="n">${money(cartTotal())}</span></div>
    ${po ? `
      <div class="label" style="margin-bottom:4px">Order ${esc(po.orderId)} is pending payment</div>
      <div class="label" style="margin-top:16px">Simulated payment outcome</div>
      <select class="outcome" id="outcome">
        <option value="approved">Approved</option>
        <option value="rejected">Provider rejects payment (502)</option>
        <option value="slow">Slow provider response (latency)</option>
      </select>
      <button class="btn-solid" style="margin-top:14px" id="payBtn">Pay ${money(po.totalInr)}</button>
      <button class="btn-line danger" style="width:100%;margin-top:10px" id="cancelBtn">Cancel this order</button>
      <div class="label" style="margin-top:14px;line-height:1.6">
        A rejected or slow payment here produces one failed order. For an
        <em>incident</em> in OpsMind, use the Scenario Lab.</div>`
    : `<button class="btn-solid dark" id="placeBtn">Place order &amp; reserve stock</button>
       <div class="label" style="margin-top:12px;line-height:1.6">
         Stock is reserved when the order is placed. Payment is a separate step.</div>`}`;

  if (po) {
    $("#payBtn").addEventListener("click", payOrder);
    $("#cancelBtn").addEventListener("click", cancelPending);
  } else {
    $("#placeBtn").addEventListener("click", placeOrder);
  }
}

function notice(html, kind) {
  $("#cartNotice").innerHTML = html
    ? `<div class="notice ${kind || ""}">${html}</div>` : "";
}

async function placeOrder() {
  if (!state.user) { toast("Please sign in first"); show("auth"); return; }
  const btn = $("#placeBtn");
  btn.disabled = true; btn.innerHTML = '<span class="spin"></span> Reserving stock…';
  setObs("order.placed");
  const items = state.cart.map(l => ({ id: l.id, qty: l.qty, priceInr: l.priceInr }));
  const r = await call("POST", "/api/orders", { items });

  if (!r.ok) {
    const code = r.data.error || `HTTP_${r.status}`;
    setObs(`order rejected: ${code}`, "err");
    notice(`<strong>${esc(r.data.message || "We could not place that order.")}</strong>
      <div class="label" style="margin-top:7px">${esc(code)} — nothing was reserved or charged.</div>`, "bad");
    paintCart();
    return;
  }
  state.pendingOrder = r.data;
  setObs("order.stock.reserved", "ok");
  notice("Order created and stock reserved. Choose a payment outcome to continue.");
  paintCart();
}

async function payOrder() {
  const outcome = $("#outcome").value;
  const btn = $("#payBtn");
  btn.disabled = true;
  btn.innerHTML = outcome === "slow"
    ? '<span class="spin"></span> Waiting on the provider…'
    : '<span class="spin"></span> Taking payment…';
  setObs(`order.payment.requested · ${outcome}`);

  const started = Date.now();
  const r = await call("POST", `/api/orders/${state.pendingOrder.orderId}/pay`, { outcome });
  const took = Date.now() - started;

  if (r.ok && r.data.status === "PAID") {
    setObs("order.paid", "ok");
    notice(`<strong>Payment approved.</strong> Order ${esc(r.data.orderId)} is paid —
      ${money(r.data.totalInr)}, transaction ${esc(r.data.transactionId || "—")}, ${took} ms.
      <div class="label" style="margin-top:8px">Find it in OpsMind by order id or by the trace below.</div>`);
    state.cart = []; state.pendingOrder = null;
    paintCartPip(); paintCart(); loadProducts();
    return;
  }

  const code = r.data.error || `HTTP_${r.status}`;
  setObs(`payment failed: ${code}`, "err");
  notice(`<strong>We could not take payment.</strong>
    <div style="margin-top:6px">${esc(r.data.message || "The provider did not complete the transaction.")}</div>
    <div class="label" style="margin-top:8px">${esc(code)} · ${took} ms · nothing was charged.
      Your stock is still reserved — try again or cancel.</div>
    <div class="label" style="margin-top:6px">A shopper sees only this. The retry count, the failing
      dependency and the cost impact are what OpsMind shows.</div>`, "bad");
  btn.disabled = false; btn.textContent = `Pay ${money(state.pendingOrder.totalInr)}`;
}

async function cancelPending() {
  const r = await call("POST", `/api/orders/${state.pendingOrder.orderId}/cancel`);
  if (r.ok) {
    setObs("order.cancelled", "ok");
    notice("Order cancelled and stock returned to the shelf.", "warn");
    state.pendingOrder = null; paintCart(); loadProducts();
  } else { toast(r.data.message || "Could not cancel"); }
}

/* ---------- orders ---------- */
$("#ordersRefresh").addEventListener("click", loadOrders);

async function loadOrders() {
  if (!state.user) {
    $("#orderRows").innerHTML = `<div class="empty">Sign in to see your orders.</div>`;
    return;
  }
  const r = await call("GET", "/api/orders");
  const orders = r.data.orders || [];
  if (!orders.length) {
    $("#orderRows").innerHTML = `<div class="empty">No orders yet.</div>`;
    return;
  }
  $("#orderRows").innerHTML = orders.map(o => `
    <div class="row-item">
      <div class="row-main">
        <div class="label">${when(o.createdAt)} · ${o.units} unit${o.units === 1 ? "" : "s"}</div>
        <h3>Order ${esc(o.orderId)}</h3>
        <div class="label" style="margin-top:4px">${o.items.map(i =>
          esc((state.products.find(p => p.id === i.id) || {}).name || i.id) + " × " + i.qty).join(" · ")}</div>
      </div>
      <span class="badge ${esc(o.status)}">${esc(o.status)}</span>
      <strong style="min-width:96px;text-align:right;font-size:17px">${money(o.totalInr)}</strong>
      ${o.status === "PENDING"
        ? `<button class="btn-solid" style="width:auto;padding:9px 18px;margin:0" data-pay="${esc(o.orderId)}">Pay</button>` : ""}
      ${(o.status === "PENDING" || o.status === "PAID")
        ? `<button class="btn-line danger" data-cancel="${esc(o.orderId)}">Cancel</button>` : ""}
    </div>`).join("");

  $$("#orderRows [data-pay]").forEach(b => b.addEventListener("click", () => {
    const o = orders.find(x => x.orderId === b.dataset.pay);
    state.pendingOrder = o;
    state.cart = o.items.map(i => {
      const p = state.products.find(x => x.id === i.id) || {};
      return { id: i.id, name: p.name || i.id, art: p.art || "knit", category: p.category || "",
               priceInr: i.priceInr, qty: i.qty, stock: p.stock || 0 };
    });
    paintCartPip(); notice(`Order ${esc(o.orderId)} is awaiting payment.`); show("cart");
  }));
  $$("#orderRows [data-cancel]").forEach(b => b.addEventListener("click", async () => {
    b.disabled = true;
    const r2 = await call("POST", `/api/orders/${b.dataset.cancel}/cancel`);
    setObs(r2.ok ? "order.cancelled" : "cancel failed", r2.ok ? "ok" : "err");
    toast(r2.ok ? "Order cancelled" : (r2.data.message || "Could not cancel"));
    loadOrders(); loadProducts();
  }));
}

/* ---------- fulfilment desk ---------- */
async function loadDesk() {
  if (!state.user || state.user.role !== "staff") {
    $("#deskRows").innerHTML = `<div class="empty">The fulfilment desk is for staff accounts.</div>`;
    $("#stockRows").innerHTML = "";
    return;
  }
  const [o, s] = await Promise.all([
    call("GET", "/api/staff/orders"), call("GET", "/api/staff/stock")]);

  const orders = o.data.orders || [];
  $("#deskRows").innerHTML = orders.length ? orders.map(x => `
    <div class="row-item">
      <div class="row-main">
        <div class="label">${when(x.createdAt)} · ${x.units} unit${x.units === 1 ? "" : "s"} · ${esc(x.userHash || "—")}</div>
        <h3>Order ${esc(x.orderId)}</h3>
      </div>
      <span class="badge ${esc(x.status)}">${esc(x.status)}</span>
      <strong style="min-width:96px;text-align:right;font-size:17px">${money(x.totalInr)}</strong>
      ${x.status === "PAID"
        ? `<button class="btn-solid" style="width:auto;padding:9px 18px;margin:0" data-ful="${esc(x.orderId)}">Mark fulfilled</button>` : ""}
      ${(x.status === "PENDING" || x.status === "PAID")
        ? `<button class="btn-line danger" data-dcancel="${esc(x.orderId)}">Cancel</button>` : ""}
    </div>`).join("") : `<div class="empty">No orders in the queue.</div>`;

  $$("#deskRows [data-ful]").forEach(b => b.addEventListener("click", async () => {
    b.disabled = true;
    const r = await call("POST", `/api/staff/orders/${b.dataset.ful}/fulfil`);
    setObs(r.ok ? "order.fulfilled" : "fulfil failed", r.ok ? "ok" : "err");
    toast(r.ok ? "Marked fulfilled" : (r.data.message || "Could not fulfil"));
    loadDesk();
  }));
  $$("#deskRows [data-dcancel]").forEach(b => b.addEventListener("click", async () => {
    b.disabled = true;
    const r = await call("POST", `/api/staff/orders/${b.dataset.dcancel}/cancel`);
    setObs(r.ok ? "order.cancelled" : "cancel failed", r.ok ? "ok" : "err");
    toast(r.ok ? "Order cancelled" : (r.data.message || "Could not cancel"));
    loadDesk();
  }));

  const stock = s.data.products || [];
  $("#stockRows").innerHTML = stock.map(p => `
    <div class="row-item">
      <div class="thumb-sm">${art(p.art)}</div>
      <div class="row-main">
        <div class="label">${esc(p.category)} · ${esc(p.id)}</div>
        <h3>${esc(p.name)}</h3>
        <div class="label" style="margin-top:4px">${money(p.priceInr)} per unit</div>
      </div>
      <span class="badge ${p.inStock ? "PAID" : "CANCELLED"}">${p.inStock ? "Active" : "Off sale"}</span>
      <div>
        <div class="label" style="margin-bottom:5px">Stock</div>
        <input type="number" min="0" value="${p.stock}" data-stock="${esc(p.id)}"
               style="width:96px;padding:9px 11px;border:1px solid var(--line);background:var(--paper)">
      </div>
      <button class="btn-line" data-avail="${esc(p.id)}" data-to="${p.available ? "0" : "1"}">
        ${p.available ? "Mark unavailable" : "Mark available"}</button>
    </div>`).join("");

  $$("#stockRows [data-stock]").forEach(i => i.addEventListener("change", async () => {
    const r = await call("POST", "/api/staff/stock/level",
      { productId: i.dataset.stock, stock: parseInt(i.value, 10) });
    setObs(r.ok ? "stock.level.set" : "stock update failed", r.ok ? "ok" : "err");
    toast(r.ok ? `${i.dataset.stock} set to ${i.value}` : (r.data.message || "Rejected"));
    loadDesk(); loadProducts();
  }));
  $$("#stockRows [data-avail]").forEach(b => b.addEventListener("click", async () => {
    b.disabled = true;
    const r = await call("POST", "/api/staff/stock/availability",
      { productId: b.dataset.avail, available: b.dataset.to === "1" });
    setObs(r.ok ? "stock.availability.changed" : "update failed", r.ok ? "ok" : "err");
    loadDesk(); loadProducts();
  }));
}

/* ---------- scenario lab ---------- */
async function loadScenarios() {
  const r = await call("GET", "/api/admin/chaos/scenarios");
  const list = (r.data.scenarios || []).filter(s => s.name !== "recover-all");
  $("#scenarios").innerHTML = list.map(s => `
    <div class="scen">
      <h3>${esc(s.name)}<span class="prio ${esc(s.priority)}">${esc(s.priority)}</span></h3>
      <p>${esc(s.description)}</p>
      <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap">
        <button class="btn-line" data-scen="${esc(s.name)}">Inject for ${s.defaultDurationS}s</button>
        <span class="label">affects: ${esc(s.affects.join(", "))}</span>
      </div>
    </div>`).join("");
  $$("#scenarios [data-scen]").forEach(b => b.addEventListener("click", async () => {
    b.disabled = true; const old = b.textContent; b.innerHTML = '<span class="spin"></span>';
    const res = await call("POST", "/api/admin/chaos/scenario", { name: b.dataset.scen });
    b.disabled = false; b.textContent = old;
    setObs(`chaos.applied · ${b.dataset.scen}`, "err");
    toast(res.ok ? `${b.dataset.scen} injected` : "Could not inject");
  }));
}
$("#recoverBtn").addEventListener("click", async () => {
  const b = $("#recoverBtn"); b.disabled = true;
  await call("POST", "/api/admin/chaos/scenario", { name: "recover-all" });
  b.disabled = false; setObs("chaos.cleared", "ok"); toast("All services back to baseline");
});

/* ---------- init ---------- */
$$(".obs code").forEach(el => el.addEventListener("click", async () => {
  const v = el.dataset.full || el.textContent;
  if (!v || v === "—") return;
  try { await navigator.clipboard.writeText(v); }
  catch (e) {
    const t = document.createElement("textarea");
    t.value = v; document.body.appendChild(t); t.select();
    try { document.execCommand("copy"); } catch (e2) { /* ignore */ }
    document.body.removeChild(t);
  }
  el.classList.add("copied");
  setTimeout(() => el.classList.remove("copied"), 900);
  toast("Copied — paste into OpsMind log search");
}));

(async function init() {
  state.session = sessionId();
  setObs("ready");
  const me = await call("GET", "/api/auth/me");
  state.user = me.data.user || null;
  paintUser();
  await loadDemoAccounts();
  await loadCollection();
  await loadProducts();
  paintCartPip();
})();
})();
