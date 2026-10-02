# CogniKart

A small fashion storefront built to produce realistic application telemetry on
Google Cloud Run — structured logs, traces that span services, authentication
events, an order lifecycle and controllable failure.

It is the application being *monitored*. The monitoring platform that reads
these logs back out of Cloud Logging is a separate deployment.

## The services

Four Cloud Run services, all built from **one** container image. `SERVICE_NAME`
selects the role at startup, so one build produces all four.

| Service | Role |
|---|---|
| `cognikart-gateway` | Public. Serves the storefront UI and proxies the APIs |
| `cognikart-catalog` | The collection and stock levels |
| `cognikart-orders` | Order lifecycle: place → pay → fulfil / cancel |
| `cognikart-payments` | Simulated payment authorization |

## Deploy

See **[DEPLOY.md](DEPLOY.md)** — step by step, written to explain rather than
to be pasted.

## Run locally

The code uses relative imports, so it must be importable as a package named
`cognikart` — lowercase. Clone into a matching directory name:

```bash
git clone https://github.com/yokshith09/CogniKart.git cognikart
cd cognikart && python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
```

Then from the directory *above* `cognikart`, start the four services. Order
matters: a service must exist before another is pointed at it.

```bash
SERVICE_NAME=catalog  PORT=8081 cognikart/.venv/bin/python -m uvicorn cognikart.main:app --port 8081 &
SERVICE_NAME=payments PORT=8083 cognikart/.venv/bin/python -m uvicorn cognikart.main:app --port 8083 &
SERVICE_NAME=orders   PORT=8082 CATALOG_URL=http://127.0.0.1:8081 PAYMENTS_URL=http://127.0.0.1:8083 cognikart/.venv/bin/python -m uvicorn cognikart.main:app --port 8082 &
SERVICE_NAME=gateway  PORT=8080 CATALOG_URL=http://127.0.0.1:8081 ORDERS_URL=http://127.0.0.1:8082 PAYMENTS_URL=http://127.0.0.1:8083 cognikart/.venv/bin/python -m uvicorn cognikart.main:app --port 8080 &
```

Storefront: <http://127.0.0.1:8080>

The directory name only matters locally. In the container the Dockerfile copies
the build context to `/app/cognikart/`, so deployment works whatever the folder
is called.

## Demo accounts

| Role | Email | Password |
|---|---|---|
| Customer | `customer@cognikart.demo` | `ShopPass2026!` |
| Staff | `staff@cognikart.demo` | `DeskPass2026!` |

These are seeded at startup and shown on the sign-in page. This is a
demonstration application with no real data behind it.

## Generating traffic

```bash
python3 loadgen/loadgen.py --url https://YOUR-GATEWAY-URL --profile normal --duration 180
```

`--duration` and `--max-requests` are hard ceilings and cannot be disabled.
Runaway traffic is the one thing here that could actually cost money, through
Cloud Logging ingestion.

## Environment variables

| Variable | Used by | Purpose |
|---|---|---|
| `SERVICE_NAME` | all | `gateway` / `catalog` / `orders` / `payments` |
| `PORT` | all | Injected by Cloud Run |
| `CATALOG_URL` | gateway, orders | Where to reach catalog |
| `ORDERS_URL` | gateway | Where to reach orders |
| `PAYMENTS_URL` | gateway, orders | Where to reach payments |
| `SESSION_SECRET` | gateway | Signs login cookies; set it so sessions survive a redeploy |
| `GOOGLE_CLOUD_PROJECT` | all | Used to build the Cloud Logging trace field |
| `LOG_LEVEL` | all | `INFO` by default; `DEBUG` multiplies log volume |
| `ENVIRONMENT` | all | Tags every log line |
