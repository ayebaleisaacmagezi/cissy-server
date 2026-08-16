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

## Decided: no dependencies

Standard library only. `git clone` then `python3 server.py` - no venv, no pip,
nothing to go wrong on a box that is only touched every few weeks.

The one thing that argued for FastAPI was file uploads. Sidestepped by taking
raw `PUT` bodies instead of multipart forms, which costs a single line in the
browser (`fetch(url, {method: 'PUT', body: file})`) and removes the dependency
entirely. Streaming a build log is easier without a framework than with one.

---

## Phase 1 - Backend skeleton - **done**

Serve `web/` and answer `GET /api/health` with the toolchain versions the server
actually reports, by running `flutter --version` and `sdkmanager --list`.

Health is first because every later failure is either "the toolchain is missing"
or "the build genuinely broke", and the two are indistinguishable without it.

**Done when:** the browser shows the real Flutter version from the server.

Built. `GET /api/health` runs `flutter --version` and `java -version` and reads
`ANDROID_HOME`, and reports what it finds in the sidebar. The Android check
looks for the directories rather than running `sdkmanager --list`, which reaches
the network and takes tens of seconds - far too slow for something the UI polls.

## Phase 2 - Projects - **done**

`config.json` per app under `projects/<app-id>/`. Create, read, update, list,
duplicate. No build yet.

The config schema is lifted from the desktop app's manifest - app name, package
ID, URL, allowed domains, features, version. It is the contract between UI and
builder, so it is worth settling before anything depends on it.

**Done when:** apps can be created and edited via the API and survive a restart.

Built, plus the UI for it - app list, config editor, duplicate and delete.
Building is shown as an honest "not wired up yet" panel rather than a button
that does nothing.

Two things worth remembering:

- App ids come from the URL, so `store._safe_id` rejects anything that is not
  already a clean slug. `../secrets` would otherwise read outside `projects/`.
- `PUT /api/apps/<id>` drops `id`, `created_at` and `updated_at` from the
  request. Without that, a stale browser tab could move an app on top of
  another one.

## Phase 3 - Generate - **done**

`flutter create --platforms=android,ios`, then overwrite `lib/main.dart`,
`pubspec.yaml`, `AndroidManifest.xml` and `Info.plist` from the config.

The generated `main.dart` is the actual product - WebView, offline and error
screen, deep links, pull to refresh. Port it from the desktop app's
`GeneratedAppTemplate` rather than rewriting it; that file is already correct and
tested.

Keep the scaffold between builds. Regenerating it every time costs minutes.

**Done when:** a generated project builds by hand with `flutter build apk` over SSH.

Built. Verified the way that actually proves something: generate a real project
and run `flutter analyze` on it, for a fully-featured config and a bare one.
Both report **No issues found**.

That caught two things no string assertion would have. `flutter create` writes a
placeholder widget test referencing `MyApp`, which is a hard analyzer error in a
project whose app class is named something else - so a clean checkout would fail
`flutter analyze` and `flutter test` for whoever opened it on a Mac. And sharing
touched `context` after an await, which trips `use_build_context_synchronously`.

Feature flags emit or omit whole blocks, so anything referenced by an emitted
block must itself be emitted. `_load` is the example: it is only reachable from
deep links or the retry buttons, and emitting it otherwise leaves dead code that
fails the generated project's own lints.

## Phase 4 - Build and stream - **done**

`POST /api/apps/<id>/build` starts a build, returns an id.
`GET /api/builds/<id>/events` streams stdout over SSE.

One build at a time, enforced by a lock - a second request is refused with a
clear message rather than queued.

Set `org.gradle.daemon=false` in the generated project. The daemon holds ~1.5 GB
idle for hours, which is pointless when builds are days apart.

Classify the common failures - out of memory, bad URL, missing keystore, licence
not accepted - into a plain sentence above the raw log. Unclassified failures
show the log unchanged rather than a wrong guess.

**Done when:** pressing Build in a browser produces an APK, with live logs.

Built and confirmed with a real build: **40.9 MB APK in 448 seconds** on this
Windows machine.

Logs stream as server-sent events, read in the browser with `fetch` rather than
`EventSource` - `EventSource` cannot send headers, which would mean putting the
password in a query string where it lands in proxy logs and browser history.

Subscribers get the backlog and live lines with no gap and no duplicate, and a
browser that navigates away mid-build cannot take the build with it.

## Phase 5 - Signing and artifacts - **done**

Keystore upload, `key.properties` written before the build and deleted after,
release signing config patched into `build.gradle.kts`.

Downloads: APK, AAB, and the project `.zip`.

The zip excludes `build/`, `.dart_tool/`, `.gradle/`, `key.properties` and any
keystore. Without the exclusions it is ~300 MB instead of ~2 MB, and the first
two would put signing passwords in a Downloads folder in plaintext.

**Done when:** a signed AAB uploads to Play, and the zip opens and builds on a Mac.

Built. The Gradle patch is tolerant of template changes between Flutter
versions: it replaces the debug signing line if present, otherwise adds a
signing config to the release build type directly, and raises if it recognises
neither - quietly producing a debug-signed artifact that looks like a release
one is the worst available outcome.

The archive was verified by downloading and opening it: 80 entries, a complete
`ios/` folder carrying the right bundle id and display name, and no
`key.properties`, keystore, `build/` or `.dart_tool/`.

## Phase 6 - UI - **done**

The six screens from the mockup: app list, configure, building, download,
overview, rebuild. Plain HTML/CSS/JS, no build step. Sidebar navigates, page
scrolls.

Two behaviours that matter more than they look:

- **Drift warning** - say when the config has changed since the last build, so a
  stale artifact is never mistaken for a current one.
- **Auto-bump version code** - Play rejects a reused one, and the rejection
  arrives after both the build and the upload have been waited through.

**Done when:** the whole flow works without touching a terminal.

Built. Both behaviours landed: the drift warning compares the config's
`updated_at` against the last successful build's start time, and the version
code is bumped before the build so the artifact and the stored version can never
disagree.

---

## Phase 7 - payments - **demo complete**

Built ahead of accounts, on purpose, because the Collecto integration turned out
to be the part most likely to be designed wrong. It was: the first draft of the
UI mockup drew a hosted checkout and a signed webhook, which is what Stripe and
Flutterwave do. Collecto does neither.

What the real reference changed:

- **No redirect.** The customer never leaves Cissy. They type a number, we call
  `requestToPay`, and their handset gets the prompt.
- **No webhook.** Nothing calls us. Outcomes are fetched by polling
  `requestToPayStatus` with the reference we chose. Signature verification and
  event replay - two problems in the original design - simply do not exist here.
- **A new problem in their place:** something server-side has to keep asking
  after the browser has gone. That is `PaymentService`'s sweeper thread, and the
  reason every piece of payment state is a file rather than a variable.

Decisions worth keeping:

- The record is written **before** the gateway call. A crash in between must
  still leave a reference to look the payment up by.
- `requestToPay` is never auto-retried; a repeat risks two prompts and two
  debits. Status lookups retry freely because they are reads.
- Anything unknown - a timeout, a dropped connection, a body that is not JSON,
  an unrecognised status word - counts as *pending*. Reading it as failure is
  how you keep someone's money and give them nothing.
- An unanswered prompt becomes `abandoned` after five minutes, not `failed`.
  Nothing was charged and they can simply start again.
- The price comes from the plan table, never from the request.

`DemoGateway` runs the same two calls in memory with a fake handset, so the flow
is complete and demonstrable with no Collecto account. It can decline, stall,
drop a connection and return rubbish; its prompts live on disk so that
restarting the server mid-payment shows the sweeper resuming.

Accounts landed after this, so a payment now activates the plan on the account
that made it and the build allowance is enforced per customer.

**Needed to go live:** a Collecto username and an `x-api-key` registered against
`198.23.52.184`. Real prices. HTTPS.

---

## Phase 8 - accounts - **done**

Phone number and a code, `hashlib.scrypt` for passwords, session tokens stored
as SHA-256 digests. The one part of this server that is SQLite rather than JSON
files, for a single line of schema: `phone TEXT NOT NULL UNIQUE`. Two people
signing up on the same number at the same moment is exactly the case a file
layout gets wrong, and the fix in files is a lock you have to remember to take
everywhere.

Each customer's apps live under their own directory, so a store rooted at one
user cannot name another's - ownership is structural rather than a check
somebody has to remember. Miss one check with a flat layout and a customer
downloads another customer's keystore, which is a key Google will never reset.

## Phase 9 - the native layer - **done**

Written against `Web2App_Implementation_Spec.md`, in the order that let each
release stand alone.

- **The emitter split.** `template.py` was 2038 lines emitting one `main.dart`.
  Now a package - `common`, `platform`, `nav`, `screens`, `webview`, `push` -
  with `__init__` as the whole public surface. Proven neutral by diffing 178 KB
  of generated output against the previous commit: byte-identical.
- **Hiding the website's navigation** (spec §6). CSS selectors, a body class,
  and a flag on the entry URL. Injected at document start rather than on load
  finishing, because waiting means the site's own bar paints and then vanishes
  on every page. Only on allowed hosts: the app does not restyle pages that are
  not the customer's.
- **Tab echo** (spec §7). The spec assumes one WebView; this app keeps one per
  tab so switching never reloads. So a matched page lights its tab up without
  switching to it, which is honest about where the user is without moving them.
- **Push notifications** (spec §9-29, §35-43). Covered below.

## Phase 10 - push notifications - **done**

The customer owns the Firebase project. This server holds the two client
configuration files, which ship inside every app and are not secrets, and never
the service-account key that can actually send. The Apple key is never accepted
at all - it goes straight to Firebase.

Decisions worth keeping:

- The package name is checked when the file is uploaded **and again at generate
  time**. The Android package id is editable afterwards, so a file that matched
  in March can stop matching in April, and the result would be an app that
  builds, signs, installs and silently never receives anything.
- The Gradle patch raises when it recognises neither shape, the way
  `signing.patch_gradle` does. A silent no-op there produces exactly the same
  invisible failure.
- A notification payload arrives from the network, so the router accepts a fixed
  set of actions, resolves the URL against the app's own home address, and runs
  the result through the same allowed-domain check the WebView uses.
- The permission prompt waits for the second visit and shows the customer's own
  explanation first. Both platforms give an app one chance.
- `project.pbxproj` is not patched. Adding the push capability there means
  writing a format with no public specification, and getting it wrong fails in
  Xcode on the customer's Mac where nobody here can see it. One checkbox.
- The status-bar icon is traced from the uploaded logo at build time, by a Dart
  tool generated into the project - Android keeps only the alpha channel of a
  small icon, so the launcher icon renders as a featureless square. Generate
  leaves a placeholder PNG at the same resource name, so a project built by
  hand before the tool ever ran still resolves every resource; the worst case
  is the square the status bar used to show, never a failed build.

Two bugs the tests caught that `flutter analyze` could not: the messenger key
was never attached to `MaterialApp`, so a foreground banner would have had a
null `currentState` forever; and the upload handler shadowed a variable, writing
the filename into `android_package_id` - which passed validation.

## Phase 11 - documentation - **done**

Thirteen pages under `web/docs/`, served at `/docs` signed out. Reuses the app's
own stylesheet tokens so it reads as part of the product. No build step: one
list in `docs.js` renders the sidebar and the previous/next links, so adding a
page is a file and a line.

---

## Deferred

- **Artifact cleanup.** 7-day retention. 80 GB makes this tidiness rather than
  survival - roughly 15 GB of toolchain plus ~1 GB per app.
- **iOS builds.** Not possible on Linux at any point; the `.zip` is the answer.

## Open

- Whether Preview, Manifest and Diagnostics from the desktop app belong in the
  sidebar here.
- Artifact retention. Nothing deletes old builds yet. 80 GB makes it tidiness
  rather than survival, but a 40 MB APK per build adds up.
- A hosted notification gateway (spec §30). The spec argues against it in §31
  and it is right: the Collecto integration is already a demonstration of what
  being in somebody else's runtime path costs.
- The optional native modules from spec §8 - biometrics, QR scanner, maps. Three
  are already sitting in `STUDIO_SOON`, which is the right place for them.
