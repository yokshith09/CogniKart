# Deploying CogniKart to Cloud Run

CogniKart only. OpsMind comes later — see `DEPLOY.md` in the main project when
you are ready for it.

This guide assumes CogniKart lives in its **own repository**, with the package
at the repository root:

<https://github.com/yokshith09/CogniKart>

If you are working from the combined project folder instead, run
`./scripts/export_cognikart.sh` first — it builds that standalone repo for you.

You run every command. Nothing here touches your cloud by itself.

---

## Part 1 — The mental model

### What you are deploying

Four services, built from **one** container image:

```
                    ┌──────────────────┐
   your browser ───▶│ cognikart-gateway│   public. serves the storefront UI
                    └────┬────────┬────┘   and proxies the APIs
                         │        │
                browse   │        │  checkout
                         ▼        ▼
          ┌────────────────┐   ┌──────────────────┐
          │cognikart-catalog│◀──│ cognikart-orders │
          │ stock + pieces │   │ order lifecycle  │
          └────────────────┘   └────────┬─────────┘
                                        │ authorise
                                        ▼
                               ┌──────────────────┐
                               │cognikart-payments│
                               └──────────────────┘
```

All four run the **same image**. An environment variable, `SERVICE_NAME`,
tells each container which role to play at startup. That means **one build,
four deploys** instead of four builds — and the shared logging code needs no
packaging tricks.

### Why they talk over HTTPS and not in-process

They could have been one program. They are four because the whole point of the
project is to answer *"which service caused the problem?"* — and that question
only exists when there are several services with separate metrics, separate
logs and separate scaling.

### Where the logs go

You will not configure logging anywhere. Each service writes JSON to **stdout**,
and Cloud Run captures stdout into **Cloud Logging** automatically — no agent,
no sidecar, no IAM permission. This is worth understanding, because "capture
application logs" and "send logs to a cloud service" are two of the four steps
in the use case, and both are solved by *where you run the code*, not by code.

---

## Part 2 — What files a deployment needs

A Cloud Run deployment needs three things: **source code**, a **Dockerfile**
telling Google how to turn that source into a container, and a **.dockerignore**
keeping junk out of it.

### `Dockerfile` — the build recipe

```dockerfile
FROM python:3.11-slim
```
The base image. `slim` is Debian with Python and little else — a few hundred MB
instead of ~1 GB. Smaller image, faster build, faster cold start.

```dockerfile
ENV PYTHONUNBUFFERED=1
```
**The single most important line for this project.** Python buffers stdout by
default, so logs would sit in memory instead of reaching Cloud Logging. With
buffering off, every log line is written immediately. Without this your
dashboard would lag or look empty.

```dockerfile
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY . /app/cognikart/
```
Requirements are copied and installed **before** the source. Docker caches each
layer, so when you change a `.py` file only the last line re-runs — the pip
install is reused. Change `requirements.txt` and it reinstalls. This ordering
turns a 2-minute rebuild into a 20-second one.

The source lands at `/app/cognikart/` because the code imports itself as
`cognikart.main`, `cognikart.common.logging` and so on. The directory name must
match the package name.

```dockerfile
ENV PORT=8080
CMD exec uvicorn cognikart.main:app --host 0.0.0.0 --port ${PORT} --workers 1
```
Three things matter here:

- **`$PORT`** — Cloud Run *injects* this and your container must listen on it.
  Hard-coding 8080 happens to work today but is not the contract.
- **`0.0.0.0`** — not `127.0.0.1`. Listening only on localhost means Cloud Run
  cannot reach your container and every request fails. This is the most common
  first-deploy mistake.
- **`--workers 1`** — one process. Cloud Run scales by adding *instances*, not
  threads, and these services are I/O-bound anyway.

### `.dockerignore` — what not to ship

```
__pycache__/  *.pyc  .venv/  .pytest_cache/  .DS_Store  .git/  *.md  loadgen/
```
Everything not listed here is uploaded to Cloud Build and baked into the image.
Without this, your 99 MB virtualenv would ship — slower builds, bigger images,
and a `.venv` built for macOS is useless inside a Linux container anyway.

`loadgen/` is excluded too: it lives in the repo so you can run it from Cloud
Shell, but it is not part of the application and does not belong in the image.

### `requirements.txt` — pinned dependencies

```
fastapi==0.128.8
uvicorn[standard]==0.39.0
httpx==0.28.1
pydantic==2.13.5
psutil==7.2.2
```
Exact versions, not `>=`. The build must produce the same thing next week as it
does today — a dependency that silently upgrades the morning of a demo is a
genuine risk.

Note what is **not** here: no Google Cloud libraries. CogniKart writes to
stdout and that is all. Only OpsMind needs the GCP SDKs.

### Everything else in the repository

| Path | What it is |
|---|---|
| `main.py` | Entry point. Reads `SERVICE_NAME`, mounts that service's routes |
| `common/` | Shared: log schema, tracing, auth, chaos injection, middleware |
| `services/` | The four roles: gateway, catalog, orders, payments |
| `seed/products.json` | The 14-piece collection |
| `static/` | The storefront UI, served by the gateway only |
| `loadgen/` | Traffic generator. In the repo, not in the image |

Note the repository **root is the Python package**. The Dockerfile copies the
whole build context to `/app/cognikart/`, which is why the code can import
itself as `cognikart.main` without any path juggling.

---

## Part 3 — Before you deploy

### Step 0 — Set a budget (do this first)

You are on a $300 trial credit, so nothing can reach your card. But credit can
be spent silently, and a budget is how you find out.

1. <https://console.cloud.google.com/billing> → **Budgets & alerts** → **Create budget**
2. Scope: **this project only**
3. Amount: **$10** — far more than this needs
4. Thresholds: **10%, 25%, 50%, 90%, 100%**, and tick *forecasted spend*

**A budget alert does not stop anything.** It emails you. If Cloud Billing
offers **Spend caps** on your account, turn one on too — that actually pauses
services. Check for it.

### Step 1 — Open Cloud Shell

You have no `gcloud` and no Docker on your Mac, and you need neither.
**Cloud Shell** is a free Linux VM in your browser with both pre-installed and
already signed in.

<https://console.cloud.google.com> → the **`>_`** icon, top right.

> Every command from here on runs **in Cloud Shell**, not on your Mac.

### Step 2 — Clone the repository

```bash
git clone https://github.com/yokshith09/CogniKart.git && cd CogniKart
```

Check you are in the right place — the Dockerfile should be at the root:

```bash
ls Dockerfile main.py requirements.txt services/ static/
```

**Why a separate repo matters here.** `gcloud run deploy --source .` uploads
the *entire* directory to Cloud Build on every deploy. A repo whose root is
exactly the application means a ~230 KB upload and a fast build. Pointing it at
a folder that also held the monitoring platform, the tests and the docs would
ship all of that to the builder four times over.

Pulling later updates is then just:

```bash
git pull
```

### Step 3 — Point gcloud at your project

```bash
gcloud config set project YOUR_PROJECT_ID
```

```bash
export PROJECT_ID=$(gcloud config get-value project) && export REGION=asia-south1 && echo "$PROJECT_ID in $REGION"
```

**Why `asia-south1`** (Mumbai): closest to you, so the demo feels fast. Cloud
Run costs the same across standard regions, so region is about latency, not
money.

> If you close Cloud Shell, these `export` lines are lost. Re-run this step.

### Step 4 — Enable the APIs

```bash
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com logging.googleapis.com
```

**Enabling an API is always free.** You are billed for *usage*, never for
switching a service on. What each one is for:

| API | Why |
|---|---|
| `run` | Runs the containers |
| `cloudbuild` | Builds your image from source, in the cloud |
| `artifactregistry` | Stores the built image |
| `logging` | Receives stdout. **This is the use case's "send logs to cloud service"** |

Takes a minute or two the first time.

### Step 5 — Create a service account

```bash
gcloud iam service-accounts create sa-cognikart --display-name "CogniKart runtime"
```

A **service account** is the identity your container runs as. By default Cloud
Run uses the Compute Engine default account, which has **Editor** on your whole
project — far too much for a shop that only needs to print to stdout.

```bash
export SA="sa-cognikart@${PROJECT_ID}.iam.gserviceaccount.com" && echo $SA
```

**We grant it no roles at all.** Writing to stdout requires zero IAM
permission; Cloud Run collects it regardless. An application that needs no
permissions should be given none — say this out loud in your presentation, it
is a real security point.

---

## Part 4 — Deploy, one service at a time

### Order matters

`orders` needs to know where `catalog` and `payments` live. `gateway` needs all
three. A service's URL only exists once it is deployed, so:

```
catalog → payments → orders → gateway
```

You are already in the build context — the repository root *is* the application,
so `--source .` is correct from here.

### Step 6 — catalog

```bash
gcloud run deploy cognikart-catalog --source . --region $REGION --service-account $SA --allow-unauthenticated --memory 1Gi --cpu 1 --concurrency 80 --min-instances 0 --max-instances 1 --set-env-vars "SERVICE_NAME=catalog,ENVIRONMENT=demo,GOOGLE_CLOUD_PROJECT=$PROJECT_ID"
```

First run asks to create an Artifact Registry repo — answer **Y**. This build
takes 2–4 minutes; later ones are faster.

**What `--source .` actually does**, because this is the part worth
understanding. It is three operations behind one command:

1. Uploads your directory (minus `.dockerignore` entries) to Cloud Build
2. Cloud Build runs your Dockerfile and produces an image
3. The image is pushed to Artifact Registry, then deployed to Cloud Run

Every flag:

| Flag | Meaning |
|---|---|
| `--source .` | Build from this directory (where the Dockerfile is) |
| `--service-account` | Run as our zero-permission identity |
| `--allow-unauthenticated` | Anyone with the URL can call it. See the note below |
| `--memory 1Gi` | **Deliberately too much.** OpsMind will spot the waste and recommend cutting it — that is the cost-optimization demo |
| `--concurrency 80` | Requests one instance handles at once |
| `--min-instances 0` | **Scales to zero.** Costs nothing when idle. Non-negotiable |
| `--max-instances 1` | Stock lives in memory. Two instances = two different stock counts = "only 7 left" stops meaning anything |
| `--set-env-vars` | `SERVICE_NAME=catalog` is what makes this image behave as the catalog |

Capture the URL:

```bash
export CATALOG_URL=$(gcloud run services describe cognikart-catalog --region $REGION --format 'value(status.url)') && echo $CATALOG_URL
```

Check it:

```bash
curl -s $CATALOG_URL/healthz && echo && curl -s "$CATALOG_URL/categories"
```

You should see `{"status":"ok","service":"cognikart-catalog"}` and six
categories.

### Step 7 — payments

```bash
gcloud run deploy cognikart-payments --source . --region $REGION --service-account $SA --allow-unauthenticated --memory 512Mi --cpu 1 --concurrency 10 --min-instances 0 --max-instances 3 --set-env-vars "SERVICE_NAME=payments,ENVIRONMENT=demo,GOOGLE_CLOUD_PROJECT=$PROJECT_ID"
```

This one builds in seconds — Cloud Build caches the layers from step 6 and
only the `SERVICE_NAME` differs.

**Why `--concurrency 10`** when catalog gets 80: payments is the service you
will deliberately make slow. With low concurrency, injected latency forces
Cloud Run to add instances, so instance count visibly climbs on the dashboard.
At concurrency 80 one instance would absorb it and you would see nothing.

```bash
export PAYMENTS_URL=$(gcloud run services describe cognikart-payments --region $REGION --format 'value(status.url)') && echo $PAYMENTS_URL
```

### Step 8 — orders

Now the wiring begins. This service gets two extra env vars:

```bash
gcloud run deploy cognikart-orders --source . --region $REGION --service-account $SA --allow-unauthenticated --memory 512Mi --cpu 1 --concurrency 40 --min-instances 0 --max-instances 1 --set-env-vars "SERVICE_NAME=orders,ENVIRONMENT=demo,GOOGLE_CLOUD_PROJECT=$PROJECT_ID,CATALOG_URL=$CATALOG_URL,PAYMENTS_URL=$PAYMENTS_URL"
```

`CATALOG_URL` and `PAYMENTS_URL` are **how the services find each other**.
There is no service discovery and no DNS trickery — just URLs in environment
variables. `max-instances 1` again, because orders are held in memory.

```bash
export ORDERS_URL=$(gcloud run services describe cognikart-orders --region $REGION --format 'value(status.url)') && echo $ORDERS_URL
```

### Step 9 — gateway

The public one. It needs all three URLs plus a secret for signing login
cookies:

```bash
export SESSION_SECRET=$(openssl rand -hex 32)
```

```bash
gcloud run deploy cognikart-gateway --source . --region $REGION --service-account $SA --allow-unauthenticated --memory 512Mi --cpu 1 --concurrency 80 --min-instances 0 --max-instances 5 --set-env-vars "SERVICE_NAME=gateway,ENVIRONMENT=demo,GOOGLE_CLOUD_PROJECT=$PROJECT_ID,CATALOG_URL=$CATALOG_URL,ORDERS_URL=$ORDERS_URL,PAYMENTS_URL=$PAYMENTS_URL,SESSION_SECRET=$SESSION_SECRET"
```

`SESSION_SECRET` signs the login cookie. Setting it explicitly means sessions
survive a redeploy; leave it out and every deploy signs everyone out. The
gateway can scale to 5 because it holds no state — the signed cookie is
verified identically by any instance.

```bash
export GATEWAY_URL=$(gcloud run services describe cognikart-gateway --region $REGION --format 'value(status.url)') && echo "OPEN THIS: $GATEWAY_URL"
```

---

## Part 5 — Verify

### The storefront

Open `$GATEWAY_URL` in a browser. Sign in with:

| Role | Email | Password |
|---|---|---|
| Customer | `customer@cognikart.demo` | `ShopPass2026!` |
| Staff | `staff@cognikart.demo` | `DeskPass2026!` |

Place an order, choose a payment outcome, watch it succeed or fail.

### The logs — this is the step that proves the use case

```bash
gcloud logging read 'resource.type="cloud_run_revision" AND jsonPayload.event:*' --limit 10 --format 'value(jsonPayload.service, jsonPayload.event, severity)'
```

If rows come back, **implementation steps 1 and 2 of Use Case 2 are done** —
logs captured, logs in a cloud service. You wrote no logging configuration to
make that happen.

Try some filters, since this is exactly what OpsMind automates:

```bash
gcloud logging read 'jsonPayload.event="auth.signin.ok"' --limit 5 --format 'value(jsonPayload.actorRole, jsonPayload.userIdHash, timestamp)'
```

```bash
gcloud logging read 'severity>=ERROR AND resource.type="cloud_run_revision"' --limit 10 --format 'value(jsonPayload.service, jsonPayload.errorCode, jsonPayload.message)'
```

Notice the sign-in log has a **role and a hashed id, never an email**.

### A failure, end to end

Order more than exists — the Chelsea Boot has 7:

```bash
curl -s -X POST $GATEWAY_URL/api/auth/signin -H 'Content-Type: application/json' -d '{"email":"customer@cognikart.demo","password":"ShopPass2026!"}' -c /tmp/ck.txt > /dev/null && curl -s -b /tmp/ck.txt -X POST $GATEWAY_URL/api/orders -H 'Content-Type: application/json' -d '{"items":[{"id":"FW-ACCS-03","qty":99,"priceInr":312}]}'
```

You should get `INSUFFICIENT_STOCK: "only 7 of Chelsea Boot left"`. No fault
injection involved — a real business rule producing a real error log.

### Generate some traffic

From Cloud Shell:

```bash
python3 loadgen/loadgen.py --url $GATEWAY_URL --profile normal --duration 180
```

`--duration` and `--max-requests` are hard ceilings and cannot be disabled —
runaway traffic is the one thing that could actually cost you money.

---

## Part 6 — If something goes wrong

| Symptom | Cause | Fix |
|---|---|---|
| "Container failed to start and listen on PORT" | Not listening on `$PORT`, or bound to `127.0.0.1` | Check the `CMD` line uses `0.0.0.0` and `${PORT}` |
| Build fails on `pip install` | Typo in `requirements.txt` | Read the Cloud Build log — the URL is in the error |
| Storefront loads, products do not | `CATALOG_URL` wrong on gateway | `gcloud run services describe cognikart-gateway --region $REGION --format 'value(spec.template.spec.containers[0].env)'` |
| Checkout 502s | `PAYMENTS_URL` wrong on orders | Same check on `cognikart-orders` |
| Signed out on every click | `SESSION_SECRET` not set | Redeploy gateway with it |
| `gcloud logging read` empty | Wait 30s; confirm you made requests | Drop the filter to `resource.type="cloud_run_revision"` |
| Variables empty after reopening Cloud Shell | Shell restarted | Re-run steps 3 and the `export ..._URL` lines |

See a service's recent errors:

```bash
gcloud run services logs read cognikart-gateway --region $REGION --limit 30
```

Redeploying is always safe — it creates a new revision and shifts traffic. To
change one setting without rebuilding:

```bash
gcloud run services update cognikart-catalog --region $REGION --memory 512Mi
```

### Shipping a code change

Push from your Mac, then in Cloud Shell:

```bash
git pull && gcloud run deploy cognikart-gateway --source . --region $REGION
```

An existing service keeps its env vars, service account and scaling settings,
so a redeploy needs only the flags you are actually changing.

---

## Part 7 — What this costs

| Resource | Free allowance | This project |
|---|---|---|
| Cloud Run | 2M requests, 180k vCPU-s, 360k GiB-s per month | Far inside it |
| Cloud Logging | **50 GiB per project per month** | A few GiB at most |
| Cloud Build | Free daily allowance | 4 builds, mostly cached |
| Artifact Registry | 0.5 GB free | One ~200 MB image |

**Three things could actually cost money:**

1. **Leaving the load generator running.** Bounded by `--duration`.
2. **`--min-instances` above 0.** One always-on instance is ~2.6M
   instance-seconds a month against a 240k free allotment — roughly 11× over,
   for a service doing nothing. Every command above uses `0`.
3. **Forgetting to tear down.** The commonest cause of a surprise bill.

Expect **$0** for a hackathon's worth of use.

---

## Part 8 — Tear down when finished

```bash
for s in cognikart-gateway cognikart-orders cognikart-payments cognikart-catalog; do gcloud run services delete $s --region $REGION --quiet; done
```

```bash
gcloud iam service-accounts delete sa-cognikart@${PROJECT_ID}.iam.gserviceaccount.com --quiet
```

Logs already ingested stay for 30 days and cost nothing further.

---

## One honest note on `--allow-unauthenticated`

Every service above is publicly reachable, including `catalog`, `orders` and
`payments`, which should really only accept calls from the gateway.

Properly, the internal three would be `--no-allow-unauthenticated`, and the
gateway would fetch an identity token from the Cloud Run metadata server and
send it as a bearer token on every internal call. That is roughly fifteen lines
of code and a `roles/run.invoker` binding per service.

It is on the roadmap rather than done, and worth mentioning before a judge asks
— knowing the gap and naming the fix reads far better than not having noticed.

---

## Next

Once this works and you have clicked around the storefront, `DEPLOY.md` in the
main project deploys OpsMind — which reads these logs back out of Cloud Logging
and turns them into the dashboard that is the actual deliverable.

Keep the gateway URL to hand; OpsMind needs it for its Scenario Lab controls.
