# Cissy Build Server

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

Not yet: uploaded icons are stored but not resized into the Android mipmaps and
the iOS appiconset, so a generated app still shows the default Flutter icon. Old
build artifacts are never deleted.

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
docs/PLAN.md     what is built and what is next
```

## Deploying

```bash
git clone https://github.com/<you>/cissy-server.git /opt/cissy
cd /opt/cissy && pip install -r requirements.txt
```

Updates are `git pull` and a restart.

## A note on signing keys

Keystore passwords are never written to `config.json` and never committed. They
are entered per build, used to write `android/key.properties`, and that file is
deleted afterwards and excluded from any download. A leaked upload key cannot be
revoked — Google will not reset it, and you lose the ability to update the app.
