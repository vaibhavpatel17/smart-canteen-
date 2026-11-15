# Smart Canteen: paperless ordering with demand forecasting

Students sign up, add their class timetable, pre-order on their phones, **pay inside the app like on Zomato or Swiggy**
(UPI apps, cards, netbanking through Razorpay) and get an **e-token** (QR + PIN). Orders show up on the kitchen screen with no paper
and nobody confirms a payment by hand. (No Razorpay account yet? A manual **UPI-to-the-owner** mode works until you have one.) The system suggests pickup slots from the
student's own free breaks and the kitchen's capacity, and forecasts demand per item from past orders. Everything runs on
**India time (IST, UTC+05:30)**, whatever timezone the phone is set to.

It is a working prototype: one small Flask + SQLite server with a real JSON API, two web screens that install on a phone like an app,
no build step. There is no fake in-browser backend any more: both screens always talk to the server's `/api` endpoints.

```
Student phones (installed app)  ──┐   https
                                  ├──►  Flask server + SQLite (hosted, public address)  ◄──  Kitchen tablet (installed app)
Student phones (installed app)  ──┘          │
                                             └─ Razorpay: takes the payment (UPI apps, cards, netbanking), calls back, refunds
```

* **Run it on your own computer first** (next section), then **host it** (`DEPLOY.md`) so any phone can reach it.
* **Payments** work like Zomato and Swiggy through Razorpay (section "Payments like Zomato and Swiggy").

## Run it (5 minutes)

You need Python 3.9 or newer.

```bash
cd smart-canteen-app
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python3 app.py
```

The terminal prints the addresses, the kitchen PIN and the payment mode:

```
Students:  http://192.168.1.23:5000/
Kitchen:   http://192.168.1.23:5000/kitchen   (PIN 482913)
Payments:  RAZORPAY in TEST mode, no real money moves, webhook off (set RAZORPAY_WEBHOOK_SECRET)
Mode:      live, India time (IST): Wed 21 Oct 12:50 PM
```

(With no Razorpay keys the line reads `real UPI to the owner...` instead. See the next two sections.)

* **Same computer:** open both in a browser.
* **Phone and tablet:** put them on the **same Wi-Fi** as the computer and type the printed address into each.
  Place an order on the phone and watch it appear on the tablet within about two seconds.
  On iPhone/iPad use Share → Add to Home Screen; on Android use ⋮ → Add to Home screen.
  If it will not load, the computer's firewall is the usual cause: allow Python on private networks.
* **A friend on mobile data (not on your Wi-Fi):** run a free tunnel next to the server, for example
  `cloudflared tunnel --url http://localhost:5000`, and send them the `https://….trycloudflare.com` address it prints.
  The kitchen screen works through the same address.

The kitchen PIN in live mode is a random 6 digit number made the first time and stored in the database, so it stays the
same after a restart. Set your own with `KITCHEN_PIN=7391 python3 app.py`.

## Install it on a phone (Add to Home Screen)

The student screen and the kitchen screen are installable web apps (manifest, icon, service worker). Open the server's public https address, then:

* **Android (Chrome):** menu ⋮ → **Install app** (or *Add to Home screen*). It opens full screen with the samosa icon.
* **iPhone / iPad (Safari):** Share → **Add to Home Screen**.
* **Kitchen tablet:** open `/kitchen` and install it too. It is a separate app with its own green icon.

Installing needs the page to come from **https** (or `localhost`). That is one more reason to host it (`DEPLOY.md`). The app still talks to the live server for everything:
the service worker only keeps the app shell, never orders or payments, so with no signal the screen opens but shows that it is offline.
This is a web app on the home screen, **not** a Play Store or App Store app.

## Payments like Zomato and Swiggy (Razorpay)

Food apps do not ask you to type a transaction ID. You tap **Pay**, a payment window opens on top of the app, you approve in your
UPI app or enter a card, and the order simply goes through. That is a **payment gateway**, and this project uses **Razorpay** for it.

**What the student sees:** tap *Pay ₹52* and Razorpay's window opens at once (UPI apps, cards, netbanking, wallets). Pay, and the
e-token appears. Declined? The reason is shown and *Try again* reopens the window. Closed the window? The slot stays held and *Pay* reopens it.

**UPI apps (GPay, PhonePe, Paytm) in the window.** On **Android** phones Razorpay's window opens the student's UPI app directly (UPI intent) and the app returns to the
canteen when they approve. Razorpay's documentation says this is usually available by default and works automatically once the intent flow is enabled on your account (if it is not, Razorpay Support can switch it on).
On **iPhone** the same documentation says UPI intent is **not supported on mobile web** ("We do not support UPI intent for iOS Mobile Web currently"), so an iPhone
student pays inside the window with a UPI ID or QR, a card or netbanking instead. Opening GPay/PhonePe straight from an iPhone needs a native iOS app with Razorpay's iOS SDK, which is **not** built here.

**What the kitchen does:** nothing. The order reaches the cooking queue by itself. The **Payments** page only shows money received today.

**What happens behind the scenes (every payment):**

1. Tapping Pay creates the order and holds the slot and stock for 10 minutes. The server creates a matching Razorpay order for the exact total.
2. The student pays in Razorpay's window. Razorpay hands back a signed receipt.
3. The server checks the signature, then **asks Razorpay itself** what happened: right order, right amount (to the rupee), status captured
   (an *authorized* payment is captured by the server). Only then is the order released to the kitchen.
4. **Webhook:** Razorpay also calls the server by itself when a payment succeeds, so an order is confirmed even if the student paid and
   immediately closed the app or lost signal. Whichever of the two arrives first wins; the second changes nothing.
5. **Late money is returned:** if the payment lands after the 10 minute hold ended (the slot was released), it is **refunded automatically** and the student is told.
6. **Refunds are automatic:** cancelling a paid order (student before cooking, or kitchen) refunds the full amount to the student's own account.
   If Razorpay cannot send one, the order is kept and the kitchen is told (nothing is cancelled without a refund).

### Set it up (about 15 minutes, free, no KYC for the first part)

**Step A. Test mode: try it with fake money**

1. Create a Razorpay account at razorpay.com (email or phone, OTP). Razorpay gives every account a **Test Mode** with its own keys.
2. In the Dashboard switch to **Test Mode**, open *Account & Settings > API Keys > Generate Key*, and copy the Key Id (`rzp_test_...`) and the Key Secret.
3. In the project folder copy `.env.example` to `.env` and paste the two values (or `export RAZORPAY_KEY_ID=... RAZORPAY_KEY_SECRET=...`). Start `python3 app.py`.
   The terminal should say **RAZORPAY in TEST mode**, and the student pay screen shows a Razorpay test-mode box.
4. Order from a phone and pay in Razorpay's window with Razorpay's test details: UPI ID **`success@razorpay`** (or `failure@razorpay` to see a failure),
   or card **4111 1111 1111 1111** with any future expiry and any CVV. The e-token appears. No money moves.

**Step B. Webhook (so closing the app after paying is safe)** Razorpay can only call a public https address, so on your own computer run a tunnel
(`cloudflared tunnel --url http://localhost:5000` prints an `https://...trycloudflare.com` address). Then in the Razorpay Dashboard:
*Account & Settings > Webhooks > Add New Webhook*, URL `https://<that address>/api/razorpay/webhook`, tick **payment.authorized** and **payment.captured**,
set any secret you like, and put the same secret in `.env` as `RAZORPAY_WEBHOOK_SECRET`. Restart. The kitchen Settings page then says *Webhook: set up*.
(A free tunnel address changes every time it restarts, so you must update the webhook URL each time. A real server with a fixed address avoids that.)

**Step C. Live mode: real money** Razorpay must verify you before real payments work. You apply in the Dashboard: as an **individual (unregistered)** you give your
personal PAN, complete identity KYC (CKYC with your PAN and an OTP, otherwise Aadhaar/ID and a short video KYC), and add a bank account
(Razorpay deposits ₹1 to check it). A registered business adds its documents instead. Razorpay's page gives no fixed review time, so plan for a day or two.
After approval generate **Live** keys (they start with `rzp_live_`), put them in `.env`, and create a **new** webhook in Live mode.
Do a real ₹1 test: set one item's price to ₹1 on the Menu page and buy it from a friend's phone.

**Cost (as Razorpay states it in Oct 2026; check their pricing page before relying on it):** no setup fee and no annual fee. The standard rate is
**2% per domestic transaction plus 18% GST on that fee** (about 2.36%, so roughly ₹1.18 on a ₹50 order). New accounts currently get a 90 day / ₹5 lakh
zero-fee offer (some card types excluded). The money reaches your bank account a day or two after payment (Razorpay's settlement schedule).

**What was and was not tested.** Everything above is tested against a **fake Razorpay** that replies the way Razorpay's documentation says
(83 backend tests and a browser test of the whole flow). It has **not** been run against Razorpay's real servers, because they cannot be reached from where this was built,
and I cannot create your Razorpay account. The first thing to do is Step A. If Razorpay's window behaves differently from what is described here, that is
where it will show up, and it will be a small fix.

## Fallback: UPI straight to the owner (no Razorpay account)

Used only when **no** Razorpay keys are set. This is the manual mode: it works today with just your UPI ID, but you confirm each payment yourself.

1. Start the server and open the kitchen screen (`/kitchen`) with the PIN.
2. Go to **Settings** and type **your UPI ID** (for example `vaibhav@okhdfcbank`) and the name students should see.
   Until this is set, students can look at the menu but cannot order. You can also set `UPI_ID` and `UPI_NAME`
   as environment variables instead.
3. Test it yourself first: set one item's price to **₹1** on the Menu page, order it from a second phone (your friend's)
   and pay that ₹1.

What the student sees: the UPI sheet shows your UPI ID, the exact amount and a note with the token number, with an
**Open my UPI app** button (Android lets them pick any app; on iPhone there is one button each for Google Pay, PhonePe and
Paytm), a **Copy** button for the UPI ID, and a QR for paying from another device. After paying, the student types the
**12 digit UPI transaction ID** (UTR) from the payment details in their app and taps *I have paid*.

What you do: the kitchen screen beeps and shows a banner. Open **Payments**, look at your UPI app for that amount and
transaction ID, then tap **Payment received**. Only then does the order join the cooking queue and the student's e-token
appear. If you cannot find the payment, tap **Not received** (twice, to be sure): the student sees why and can re-enter the ID.
A transaction ID can only be used for one order.

**Why you confirm by hand:** a personal UPI ID has no way of telling this server that money arrived (UPI apps do not
publish that to developers). Automatic confirmation needs a payment gateway account (Razorpay, Cashfree, PhonePe) with KYC.
Automatic confirmation needs a payment gateway account, which is exactly what the Razorpay section above sets up. Until then, the manual check is the safe, honest way to take real money.

**Refunds are manual too:** if a paid order is cancelled (by the student before cooking starts, or by the kitchen), it moves
to **Refunds to send** on the Payments page. Send the money back from your UPI app (the student's number is shown) and tap
**Mark refunded**. A student cannot cancel while you are still checking their payment.

If a UPI app refuses the one-tap link ("cannot pay this merchant"), copy the UPI ID and pay it by hand, which works in any app.

### Developer demo (not for students)

`python3 app.py --demo` starts a **demo** instead: a fast clock (1 minute every 10 seconds, starting at 12:50 pm), 42 days of
made-up sales history, a lunch rush in progress, the kitchen PIN `1234` and a **built-in test gateway** where no real money
moves (a top bar lets you switch between the Student and Kitchen screens and drive the clock). Use it to show the idea.

The mode is chosen when the database is first created. To switch, stop the server and delete `instance/canteen.db`
(or run `python3 app.py reset` / `python3 app.py reset --demo`; accounts are kept). The kitchen's Settings page has the same button in demo mode.

## Accounts and timetables

* **Sign up** (name, 10 digit mobile number, password of 6+ characters) is the first thing a new student sees. The password is
  stored as a salted PBKDF2 hash, never as typed, and repeated wrong passwords are throttled. You stay signed in for 60 days.
* **Timetable** opens straight after sign up. For each weekday the student adds their free breaks (for example Lunch 12:30 to
  1:15 pm). Slots are only offered inside those breaks, clipped to the canteen's opening hours (8:30 am to 5:30 pm). A day with no
  breaks, or a student who skips it, sees the canteen's own break times instead. The timetable can be edited any time from the account button.
* Orders belong to the account, so they follow the student to any phone and survive clearing the browser.

## How a student order works

1. **Pick food.** Items that are running low say so. Stock is checked again on the server, so two students cannot
   buy the last samosa.
2. **Choose a slot.** The server finds the student's next break, looks at how many items are already booked for each
   5-minute slot, and suggests the least busy one that leaves enough cooking time. Full slots are disabled.
3. **Pay.** Tapping **Pay** creates the order and *holds* the slot and stock for 10 minutes (5 in demo mode), then opens the payment (Razorpay's window in live mode, or the UPI sheet in the fallback mode). An unpaid order expires and releases everything. A student has one open checkout at a time.
4. **E-token.** As soon as the payment is confirmed (by Razorpay automatically, or by you in the UPI fallback mode) the student gets a token number, a 4-digit PIN and a QR code. The token turns green
   when the kitchen marks the order ready. While it is still "Received" the student can cancel for a refund.
5. **Pickup.** At the counter the kitchen types the token number and PIN (a wrong PIN is rejected, and repeated wrong
   guesses are throttled), checks the order and taps **Mark as Picked Up**.

## Kitchen screens

Modelled on a simple sidebar dashboard: **Live Orders** (New / Preparing / Ready / Delayed counters that double as
filters, a sortable table, an order detail panel with a status stepper, and a banner when payments need confirming),
**Payments** (money received today; in UPI fallback mode also the payments to confirm and refunds to send), **Pickup Counter** (ready cards, Announce Next,
token + PIN check, a full-screen queue display), **Order History**, **Menu Management**, **Demand Forecast**,
**Inventory** and **Settings** (payment status, kitchen capacity per slot, pause new orders, sound, light/dark/auto theme).
On a phone the sidebar becomes a bottom bar. Buttons are large touch targets so they are easy to hit with wet or floury hands.

## Payment methods

| Mode | How students pay | Who confirms |
|---|---|---|
| **Razorpay** (when `RAZORPAY_KEY_ID` and `RAZORPAY_KEY_SECRET` are set) | Razorpay's window inside the app: UPI apps, cards, netbanking, wallets | **automatic** (signature + Razorpay lookup + webhook); refunds automatic |
| **UPI to the owner** (live mode, no keys) | UPI app link, UPI ID or QR, then the 12 digit transaction ID | the owner, on the Payments page; refunds sent by the owner |
| **Built-in simulated gateway** (`--demo` only, for developers) | one tap on a UPI app, a UPI ID, a card or a bank; nothing is charged | automatic |

The simulated gateway is only for trying the screens on your own computer and for the automatic tests. It is **off** unless you start the server with `--demo`.
In it a card must pass the number check (for example `4111 1111 1111 1111`, any future expiry, any CVV; `4000 0000 0000 0002` is declined) and the UPI ID `fail@upi` is declined.
With Razorpay the card and UPI details are typed into Razorpay's window, so this server never sees them.

## Demand forecasting

`forecast.py` is deliberately simple and explainable. For each item and each hour:

* it takes the last 6 weeks of sales and removes the day-of-week effect (Fridays are busy, Saturdays are quiet),
* applies an exponential moving average (recent days count more),
* puts the day-of-week effect back for today.

From that it produces the **Order Volume Prediction** chart (actual vs predicted), **Top Predicted Items** (change against
the average of the last four same weekdays), **Expected Crowd**, **Recommended Preparation**, **Kitchen Capacity**, and a
suggested cooking quantity for the day (forecast + 12 % safety margin) that you can apply on the Inventory page.

In live mode, run `python3 app.py rollup` at the end of each day (or just leave the server running, which rolls
the day over automatically) so that today's real sales feed tomorrow's forecast. With less than a few weeks of real data
the numbers will be rough, and the page says "from past orders" rather than claiming accuracy.

## Folder map

```
app.py            Flask routes + command line          db.py        SQLite schema, IST clock helpers, transactions
logic.py          slots, orders, payment flow, kitchen actions     forecast.py   the forecasting model
accounts.py       sign up, sign in, timetables                      demo.py       sample orders for demo mode
payments.py       Razorpay, UPI links, test gateway          .env.example  copy to .env for your Razorpay keys
config.json       menu, timetable, banks, capacities (edit this!)   wsgi.py  production entry point
templates/        base.html, student.html, kitchen.html, sprite.html (SVG food stickers and icons)
static/js/        student.js (sign up, timetable, ordering, UPI checkout, e-token)   kitchen.js (sign-in, live orders, pickup counter)
                  kitchen-pages.js (history, menu, inventory, forecast, settings)   common.js   shell.js (demo bar)
                  api-remote.js (every call to the server's /api)   boot.js   sw.js (service worker)
static/css/       base, student, kitchen, shell, fonts       static/icons/  app icons
Dockerfile  Procfile  requirements-prod.txt  .env.example  DEPLOY.md     how to host it
tests/            test_backend.py (89 tests)   e2e_flow.py (full order flow, simulated gateway)   e2e_upi.py (UPI fallback flow)   e2e_pwa.py (installable app)
                  e2e_razorpay.py (Razorpay flow)   fake_razorpay.py + serve_razorpay.py (a fake Razorpay for testing)
                  serve_frozen.py (server with the clock frozen at 12:50 pm IST)
```

**Make it yours:** edit `config.json` for your own menu (name, price, prep minutes, daily quantity), canteen break times, banks and slot capacity. Restart with a fresh database (`rm instance/canteen.db`).

## Tests

```bash
python3 -m unittest discover -s tests          # 89 backend tests, no browser needed (includes starting app.py in each payment mode and through gunicorn)
```

Browser tests (optional): `pip install playwright && playwright install chromium`. They start the server with the clock
frozen at 12:50 pm IST so they pass at any hour:

```bash
python3 tests/serve_frozen.py 5101 --demo &       # kitchen PIN 4321
python3 tests/e2e_flow.py flask http://127.0.0.1:5101         # sign up, timetable, pay, e-token, cook, pickup, refunds

python3 tests/serve_frozen.py 5102 &              # live mode, real-UPI path (no money moves: the test plays both people)
python3 tests/e2e_upi.py http://127.0.0.1:5102

python3 tests/serve_razorpay.py 5103 &            # live mode with a FAKE Razorpay behind the real server (webhook secret whsec_test)
python3 tests/e2e_razorpay.py http://127.0.0.1:5103   # pay, retry, webhook-only, refund, late payment, Razorpay down

python3 tests/e2e_pwa.py http://127.0.0.1:5102    # manifest, icons, service worker, offline shell (run against the server from the line above)
```

## API (all JSON)

| Student (session cookie) | | Kitchen (needs the PIN login cookie) | |
|---|---|---|---|
| `GET /api/config`, `/api/menu`, `/api/rev` | settings, menu with stock, change counter + IST clock | `POST /api/kitchen/login`, `/logout` | PIN sign in |
| `POST /api/auth/signup`, `/login`, `/logout`, `GET /api/auth/me` | accounts | `GET /api/kitchen/snapshot` | live orders, payments to confirm, refunds |
| `PUT /api/timetable` | save the weekly free breaks | `PATCH /api/kitchen/orders/<id>` | move status forward or back |
| `POST /api/slots` | suggested slots for a basket | `POST /api/kitchen/orders/<id>/payment-received`, `/payment-missing`, `/refunded` | confirm UPI payments and refunds |
| `POST /api/orders` | create order and hold slot | `POST /api/kitchen/orders/<id>/cancel` | cancel (+ refund) |
| `POST /api/payments/<id>/utr` | send the UPI transaction ID | `POST /api/kitchen/verify` | token + PIN check |
| `POST /api/payments/<id>/pay`, `/razorpay` | test gateway; Razorpay return (signed receipt) | `GET /api/kitchen/history`, `/forecast`, `/inventory` | reports |
| `GET /api/orders/mine`, `POST /api/orders/<id>/cancel` | the student's own orders | `PATCH /api/kitchen/menu/<id>`, `/settings` | menu, capacity, UPI ID |

`POST /api/razorpay/webhook` is called by Razorpay itself (no cookie; the `X-Razorpay-Signature` header is checked against `RAZORPAY_WEBHOOK_SECRET`).

`GET /healthz` (is the server and its database up; use it as the host's health check), `GET /manifest.webmanifest`, `/kitchen.webmanifest`, `/sw.js` (the installable app).

Both screens poll `/api/rev` every two seconds and reload only when the number changes, which is cheap enough for
an old tablet on school Wi-Fi.

## Honest limits

* **Razorpay has been tested only against a fake Razorpay** (a stand-in that follows Razorpay's documentation), never against Razorpay's real servers or
  with real money. Do Step A (test keys) and then a real ₹1 order before you rely on it. Live mode needs your own Razorpay account and KYC, and a
  public https address for the webhook.
* **Hosting files are untested on a real host.** The server was run through gunicorn behind a simulated HTTPS proxy and the Dockerfile follows standard practice,
  but it has not been deployed anywhere. `DEPLOY.md` says what to check after the first deploy.
* **UPI-app buttons in Razorpay's window work on Android only** (see above). iPhone students pay with a UPI ID or QR, card or netbanking.
* **It is a web app on the home screen, not a Play Store / App Store app.** Real GPay/PhonePe app-switching on iPhone would need a native iOS app.
* **Fees:** Razorpay keeps about 2% plus GST of every online payment (after any offer period). The manual UPI fallback has no fee but needs a person to confirm each payment.
* **UPI fallback is confirmed by a person.** The owner checks the UPI app and taps *Payment received*; the server cannot see the bank. A student could type a
  transaction ID for a payment that never happened, which is why nothing is cooked until you confirm. Its refunds are manual too (you send them and mark them done).
  Tested in a browser with the money step simulated, **not on real Android/iOS UPI apps**.
* The QR code on the e-token is generated, but the kitchen **types** token + PIN. Camera scanning is not built.
* It has been tested in headless Chromium at phone, tablet and desktop sizes, **not** on real Android or iOS devices or Safari.
* One canteen, one server. A server on your own computer only works while the computer is on and the tunnel or Wi-Fi is up.
  For regular use add HTTPS, backups and a small always-on host.
* The sales history, student names and sample orders in demo mode are invented. In live mode the forecast starts from a
  generated history and gets better as real days are added (`python3 app.py rollup`, or leave the server running).
* All times are India time (IST). The server's own timezone does not matter.
