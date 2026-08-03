# Build plan

Six phases. Each one ends somewhere usable, so the thing is never half-broken
waiting on the next step.

## Target environment

The KVM, as configured:

| | |
|---|---|
| RAM | 4 GB + 4 GB swap |
| Disk | 80 GB |
| Flutter | 3.44.8 stable |
| Android SDK | platform 36, build-tools 36.0.0 |
| JDK | OpenJDK 17 |
| Users | one |
| Frequency | occasional, not daily |

Single user and occasional use remove two things that would otherwise dominate
the design: no job queue (a lock is enough) and no accounts (one password).

---

## Phase 1 — Backend skeleton

Serve `web/` and answer `GET /api/health` with the toolchain versions the server
actually reports, by running `flutter --version` and `sdkmanager --list`.

Health is first because every later failure is either "the toolchain is missing"
or "the build genuinely broke", and the two are indistinguishable without it.

**Done when:** the browser shows the real Flutter version from the server.

## Phase 2 — Projects

`config.json` per app under `projects/<app-id>/`. Create, read, update, list,
duplicate. No build yet.

The config schema is lifted from the desktop app's manifest — app name, package
ID, URL, allowed domains, features, version. It is the contract between UI and
builder, so it is worth settling before anything depends on it.

**Done when:** apps can be created and edited via the API and survive a restart.

## Phase 3 — Generate

`flutter create --platforms=android,ios`, then overwrite `lib/main.dart`,
`pubspec.yaml`, `AndroidManifest.xml` and `Info.plist` from the config.

The generated `main.dart` is the actual product — WebView, offline and error
screen, deep links, pull to refresh. Port it from the desktop app's
`GeneratedAppTemplate` rather than rewriting it; that file is already correct and
tested.

Keep the scaffold between builds. Regenerating it every time costs minutes.

**Done when:** a generated project builds by hand with `flutter build apk` over SSH.

## Phase 4 — Build and stream

`POST /api/apps/<id>/build` starts a build, returns an id.
`GET /api/builds/<id>/events` streams stdout over SSE.

One build at a time, enforced by a lock — a second request is refused with a
clear message rather than queued.

Set `org.gradle.daemon=false` in the generated project. The daemon holds ~1.5 GB
idle for hours, which is pointless when builds are days apart.

Classify the common failures — out of memory, bad URL, missing keystore, licence
not accepted — into a plain sentence above the raw log. Unclassified failures
show the log unchanged rather than a wrong guess.

**Done when:** pressing Build in a browser produces an APK, with live logs.

## Phase 5 — Signing and artifacts

Keystore upload, `key.properties` written before the build and deleted after,
release signing config patched into `build.gradle.kts`.

Downloads: APK, AAB, and the project `.zip`.

The zip excludes `build/`, `.dart_tool/`, `.gradle/`, `key.properties` and any
keystore. Without the exclusions it is ~300 MB instead of ~2 MB, and the first
two would put signing passwords in a Downloads folder in plaintext.

**Done when:** a signed AAB uploads to Play, and the zip opens and builds on a Mac.

## Phase 6 — UI

The six screens from the mockup: app list, configure, building, download,
overview, rebuild. Plain HTML/CSS/JS, no build step. Sidebar navigates, page
scrolls.

Two behaviours that matter more than they look:

- **Drift warning** — say when the config has changed since the last build, so a
  stale artifact is never mistaken for a current one.
- **Auto-bump version code** — Play rejects a reused one, and the rejection
  arrives after both the build and the upload have been waited through.

**Done when:** the whole flow works without touching a terminal.

---

## Deferred

- **Auth.** One shared password before it faces the public internet. Until then,
  bind to localhost and reach it over SSH tunnel or Tailscale.
- **Artifact cleanup.** 7-day retention. 80 GB makes this tidiness rather than
  survival — roughly 15 GB of toolchain plus ~1 GB per app.
- **iOS builds.** Not possible on Linux at any point; the `.zip` is the answer.

## Open

- Backend framework: FastAPI (SSE and uploads are cleaner) versus stdlib
  `http.server` (zero dependencies). FastAPI unless the dependency is unwelcome.
- Whether Preview, Manifest and Diagnostics from the desktop app belong in the
  sidebar here.
