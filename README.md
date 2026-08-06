# Cissyweb2app

Turns a website into an Android app from a browser. Runs on a Linux box that has
Flutter, the Android SDK and a JDK; you reach it from anywhere.

Fill in a URL, an app name and an icon, press Build, watch the log, download a
signed APK or AAB. For iOS it hands back the complete Flutter project as a `.zip`
to open on a Mac, since Apple's toolchain is macOS-only.

This is a separate product from the `webview_builder` desktop app. They share
ideas, not code.

## What works

Create an app, configure it, upload an icon and a keystore, press Build, watch
the log stream, download a signed APK or AAB — and the whole Flutter project as
a `.zip` for the iOS half.

Mobile-money subscriptions through Collecto, running against a simulator until
there is an account — see below.

Not yet: there are no user accounts, so the subscription is server-wide rather
than per-customer. Old build artifacts are never deleted.

The splash image is applied everywhere it can be: it is the Android launch
window itself, so the app opens straight into it, and on Android 12+ the
system splash gets a transparent icon so the launcher icon no longer flashes
before it.

## Requirements

On the server:

- Flutter (stable) on `PATH`
- Android SDK with platform + build-tools, licences accepted
- JDK 17
- Python 3.10+

Verify with `flutter doctor` before expecting a build to work.

## Running it

```bash
python3 server.py
```

No venv, no `pip install` — it uses only the standard library. Listens on
`127.0.0.1:8080`.

| Variable | Default | |
|---|---|---|
| `CISSY_PASSWORD` | none | Shared password. Without it the server refuses to bind to anything but localhost. |
| `CISSY_HOST` | `127.0.0.1` | |
| `CISSY_PORT` | `8080` | |
| `CISSY_ROOT` | this directory | Where `projects/` lives. |
| `CISSY_COLLECTO_USERNAME` | none | Collecto account. Payments stay in demo mode until this and the key are both set. |
| `CISSY_COLLECTO_KEY` | none | `x-api-key`, issued against **this machine's IP**. |
| `CISSY_COLLECTO_REFERER` | `https://web2app.cissytech.com` | Sent on every call; their WAF rejects requests without one. |

To reach a localhost-only server from your machine:

```bash
ssh -L 8080:127.0.0.1:8080 you@your-server
```

Tests:

```bash
python3 -m unittest discover -s tests -t . -q
```

## Layout

```
server.py        the backend — runs commands, streams logs
web/             the UI (plain HTML/CSS/JS, no build step)
projects/        runtime data, gitignored
  <app-id>/
    config.json  the app's settings
    generated/   the Flutter project (kept warm between builds)
    builds/<n>/  artifacts and log for one build
payments/        one JSON file per payment, gitignored
  _demo/         simulated handset prompts, demo mode only
subscription.json  the current plan (server-wide until accounts exist)
docs/PLAN.md     what is built and what is next
```

## Deploying

```bash
git clone https://github.com/<you>/cissy-server.git /opt/cissy
cd /opt/cissy && pip install -r requirements.txt
```

Updates are `git pull` and a restart.

## Payments

Collecto is not shaped like Stripe. There is no checkout page, no redirect and
**no webhook**. You call `requestToPay`, the customer's handset gets a PIN
prompt, and you find out what happened by calling `requestToPayStatus` with the
same reference until it answers.

That shapes the code more than anything else here:

- The payment record is written to disk **before** the gateway is called. If the
  process dies in between, the reference is the only thread back to the money.
- A background sweeper chases every open payment, so a prompt approved after the
  browser closed still activates the plan. Close the tab, restart the server —
  it still finishes.
- A 2xx is not a payment. `data.requestToPay: true` only means the call was
  accepted; only `data.status == SUCCESSFUL` is money.
- Timeouts, dropped connections and non-JSON bodies mean *unknown*, so they keep
  the payment open. Reading them as failure would keep someone's money and give
  them nothing.
- `requestToPay` is never retried automatically — a repeat could mean two prompts
  and two debits. Status lookups are reads, so they retry freely.

### Demo mode

With no `CISSY_COLLECTO_USERNAME` and `CISSY_COLLECTO_KEY`, payments run against
an in-process simulator and the Billing screen grows a **demo handset** — a panel
that stands in for the customer tapping their PIN. It can also be told to
decline, to never answer, to drop a connection, or to return something that is
not JSON, because those are the paths worth watching before real money is
involved.

Going live is two environment variables and a restart. Note that the API key is
issued against a **username and an IP**: move the box or put a proxy with a
different outbound address in front of it and payments stop while everything
else keeps working.

## A note on signing keys

Keystore passwords are never written to `config.json` and never committed. They
are entered per build, used to write `android/key.properties`, and that file is
deleted afterwards and excluded from any download. A leaked upload key cannot be
revoked — Google will not reset it, and you lose the ability to update the app.
