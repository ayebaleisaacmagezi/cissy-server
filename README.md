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
the log stream, download a signed APK or AAB - and the whole Flutter project as
a `.zip` for the iOS half.

Mobile-money subscriptions through Collecto, running against a simulator until
there is an account - see below.

Accounts, so the apps and the money belong to somebody. Sign-up is a phone
number and a code; each customer's apps live under their own directory, which
makes ownership structural rather than a check somebody has to remember.

Push notifications through the customer's own Firebase project. This server
holds the two client configuration files - which ship inside every app and are
not secrets - and never the service-account key that can actually send. The
generated app carries the registration, the permission prompt, categories, a
notification router and a local history; the Notifications page generates
working sending code for the customer's own backend. The status-bar icon is a
silhouette traced from the uploaded logo at build time, because Android keeps
only the alpha channel of a small icon and would show the logo itself as a
plain square.

Taking the website's own navigation down, so a native bottom bar is not simply
a second one, and lighting up whichever tab the page on screen belongs to.

Not yet: old build artifacts are never deleted.

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

No venv, no `pip install` - it uses only the standard library. Listens on
`127.0.0.1:8080`.

| Variable | Default | |
|---|---|---|
| `CISSY_PASSWORD` | none | Legacy shared password, kept so an existing single-user deployment does not lock itself out on upgrade. Customers sign in with a phone number. |
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
server.py        the backend - runs commands, streams logs
cissy/
  template/      the generated app's source, split by what it emits
  firebase.py    reading the uploaded Firebase configuration files
  pushdocs.py    the customer's own sending code, with their values in it
web/             the UI (plain HTML/CSS/JS, no build step)
  docs/          the documentation site, served at /docs
projects/        runtime data, gitignored
  <app-id>/
    config.json  the app's settings
    generated/   the Flutter project (kept warm between builds)
    builds/<n>/  artifacts and log for one build
payments/        one JSON file per payment, gitignored
  _demo/         simulated handset prompts, demo mode only
accounts.db      users, sessions and build allowances (SQLite)
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
  browser closed still activates the plan. Close the tab, restart the server -
  it still finishes.
- A 2xx is not a payment. `data.requestToPay: true` only means the call was
  accepted; only `data.status == SUCCESSFUL` is money.
- Timeouts, dropped connections and non-JSON bodies mean *unknown*, so they keep
  the payment open. Reading them as failure would keep someone's money and give
  them nothing.
- `requestToPay` is never retried automatically - a repeat could mean two prompts
  and two debits. Status lookups are reads, so they retry freely.

### Demo mode

With no `CISSY_COLLECTO_USERNAME` and `CISSY_COLLECTO_KEY`, payments run against
an in-process simulator and the Billing screen grows a **demo handset** - a panel
that stands in for the customer tapping their PIN. It can also be told to
decline, to never answer, to drop a connection, or to return something that is
not JSON, because those are the paths worth watching before real money is
involved.

Going live is two environment variables and a restart. Note that the API key is
issued against a **username and an IP**: move the box or put a proxy with a
different outbound address in front of it and payments stop while everything
else keeps working.

Which is why there is a command for it, to be run **on the server** after any
deploy or move:

```
python3 server.py --check-collecto
```

It reads the wallet and SMS balances, which charges nothing and sends no prompt,
and exits non-zero if the credentials are refused. It also warns when the SMS
balance is empty, because verification codes are sent over the same account and
a signup nobody can complete looks nothing like a billing problem.

## A note on signing keys

Keystore passwords are never written to `config.json` and never committed. They
are entered per build, used to write `android/key.properties`, and that file is
deleted afterwards and excluded from any download. A leaked upload key cannot be
revoked - Google will not reset it, and you lose the ability to update the app.
