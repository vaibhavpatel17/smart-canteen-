# Putting Smart Canteen on a real server

Your laptop is fine for trying it. For real students you need a server with a **public https address**, so any phone can reach it,
the app can be installed on the home screen, and Razorpay can call the webhook. This file is the checklist.

> **Not tested on a real host.** The server was run through gunicorn behind a simulated HTTPS proxy, and the Dockerfile is standard, but nothing
> here has been deployed to Fly, Render or a VPS. Do the "After the first deploy" checks below and fix anything that differs.

## What the server needs (whichever host you pick)

1. **A persistent disk.** Everything (accounts, orders, sales history, your UPI ID) is **one SQLite file**. If the host wipes its disk on restart you lose all of it.
   Mount a volume and point `CANTEEN_DB` at it (the Dockerfile uses `/data/canteen.db`).
2. **One server process.** `wsgi.py` is meant for `gunicorn -w 1 --threads 8`. Do not raise the worker count and do not run two copies at once.
3. **HTTPS in front.** Every host below gives you https. Set `BEHIND_HTTPS_PROXY=1` so the login cookie is HTTPS-only and the kitchen PIN throttle sees each visitor's real address.
4. **Secrets as environment variables**, never in the code: `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET`, `KITCHEN_PIN`. (`.env.example` lists them.)

**Hosts that do not fit:** Render's *free* web service has an ephemeral filesystem ("SQLite databases are lost on redeploys, restarts, and spin-downs") and no
persistent disk, and it spins down after 15 minutes without traffic ([Render docs](https://render.com/docs/free)). A paid Render service can attach a disk.

## Option A: Fly.io (Docker + a volume)

Fly keeps SQLite data on a **volume**, and volumes are limited to one host, so run a single machine ([Fly docs](https://docs.fly.io/rails/advanced-guides/sqlite3)).

```bash
fly launch --no-deploy                      # uses the Dockerfile in this folder; say NO to a Postgres/Redis database
fly volumes create canteen_data --size 1    # pick the region closest to your canteen when asked (Mumbai if offered)
```
In the `fly.toml` that `fly launch` wrote, make sure it contains (names may differ slightly between Fly versions, check their docs):
```toml
[env]
  PORT = "8080"
[[mounts]]
  source = "canteen_data"
  destination = "/data"
[http_service]
  internal_port = 8080
  force_https = true
```
```bash
fly secrets set KITCHEN_PIN=482913 RAZORPAY_KEY_ID=rzp_test_xxx RAZORPAY_KEY_SECRET=xxx RAZORPAY_WEBHOOK_SECRET=xxx
fly deploy
fly status                                  # exactly ONE machine
```

## Option B: any small VPS with Docker (full control)
```bash
docker build -t canteen .
docker run -d --name canteen --restart unless-stopped -p 127.0.0.1:8080:8080 -v canteen-data:/data --env-file .env canteen
```
Then put an HTTPS proxy in front, for example Caddy with a domain pointed at the VPS: `caddy reverse-proxy --from canteen.yourdomain.in --to localhost:8080`
(Caddy gets the certificate by itself).

## Option C: Render, paid web service with a disk
Create a *Web Service* from the repo, runtime Docker (or Python with the `Procfile`), add a **Disk** mounted at `/data`, set `CANTEEN_DB=/data/canteen.db`,
`BEHIND_HTTPS_PROXY=1` and the secrets above.

## After the first deploy (do these in order)

1. Open `https://<your address>/healthz`. You should see `{"ok":true}`.
2. Open `https://<your address>/kitchen`. The PIN is the `KITCHEN_PIN` you set (otherwise a random 6 digits, printed once in the server log).
3. On a phone open `https://<your address>/`, sign up, add a timetable, and install it (Android: Install app. iPhone: Share, Add to Home Screen).
4. **Redeploy test:** restart the machine and check your account and the kitchen PIN still work (proves the disk is persistent).
5. Payments: follow "Payments like Zomato and Swiggy" in `README.md`. Start with Razorpay **test** keys. The webhook URL is `https://<your address>/api/razorpay/webhook`,
   events `payment.authorized` and `payment.captured`, and the same secret as `RAZORPAY_WEBHOOK_SECRET`. A real address means it no longer changes like a free tunnel does.
6. Then a real ₹1 order with live keys.

## Day to day
* **Backup** (do this before changes, and now and then): `python -c "import sqlite3; s=sqlite3.connect('/data/canteen.db'); d=sqlite3.connect('/data/backup.db'); s.backup(d)"`, then copy `backup.db` off the server.
* **Menu or prices:** edit `config.json` and redeploy. (Prices of existing menu items can also be changed on the kitchen Menu page.)
* **Logs:** `fly logs`, `docker logs canteen`, or your host's log page.
