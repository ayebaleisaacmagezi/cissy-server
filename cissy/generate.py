"""Turning a config into a Flutter project on disk.

`flutter create` lays down the scaffold, then the files that carry the app's
identity are overwritten from the config. The scaffold is kept between builds:
recreating it costs minutes and it does not change unless the package id or
project name does.

Nothing here builds anything - that is `build.py`. Splitting them means the
generated project can be inspected, downloaded, or built by hand over SSH
without running a build first.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from . import firebase, template
from .config import (
    DEFAULT_SPLASH_DARK,
    DEFAULT_SPLASH_LIGHT,
    AppConfig,
)
from .errors import CissyError
from .process import LogSink, stream
from .store import ProjectStore

STATE_FILE = ".cissy-scaffold.json"


def generate(
    config: AppConfig,
    store: ProjectStore,
    on_log: LogSink,
) -> Path:
    """Produce a complete Flutter project and return its directory."""
    directory = store.generated_dir(config.id)
    directory.parent.mkdir(parents=True, exist_ok=True)

    name = template.project_name(config)
    org = template.organisation(config)

    if _needs_scaffold(directory, name, org):
        on_log("Creating the Flutter project scaffold...")
        _create_scaffold(directory, name=name, org=org, on_log=on_log)
        _write_state(directory, name=name, org=org)
    else:
        on_log("Reusing the existing project scaffold.")

    on_log("Writing the app source...")
    splash_asset = _copy_assets(config, store, directory)
    icon_asset = _copy_icon(config, store, directory)
    offline_asset = _write_offline_html(config, directory)

    _write(
        directory / "pubspec.yaml",
        template.pubspec(config, splash_asset, icon_asset, offline_asset),
    )
    _write(
        directory / "lib" / "main.dart",
        template.main_dart(config, splash_asset, offline_asset, icon_asset),
    )
    _write(
        directory / "android" / "app" / "src" / "main" / "AndroidManifest.xml",
        template.android_manifest(
            config, has_push_icon=config.push_enabled and icon_asset is not None
        ),
    )
    _apply_launch_screen(config, store, directory)
    _write(directory / "android" / "gradle.properties", template.gradle_properties())
    _write_main_activity(directory, config)
    _apply_firebase(config, store, directory, on_log)
    _apply_notification_icon(config, directory, icon_asset)
    _patch_info_plist(directory, config, on_log)
    _remove_default_test(directory)
    _pin_android_toolchain(directory, on_log)

    on_log(f"Project ready at {directory}")
    return directory


# ── scaffold ─────────────────────────────────────────────────────────────


def _needs_scaffold(directory: Path, name: str, org: str) -> bool:
    """Whether `flutter create` has to run again.

    Only the identity matters. Everything else this server writes is overwritten
    on every generate, so a stale scaffold cannot survive a change to it.
    """
    if not (directory / "pubspec.yaml").is_file():
        return True
    state_path = directory / STATE_FILE
    if not state_path.is_file():
        return True
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True
    return state.get("name") != name or state.get("org") != org


def _create_scaffold(directory: Path, *, name: str, org: str, on_log: LogSink) -> None:
    # --overwrite because a half-finished scaffold from a killed build would
    # otherwise make every later attempt fail the same way.
    exit_code = stream(
        [
            "flutter",
            "create",
            "--overwrite",
            "--no-pub",
            "--platforms=android,ios",
            "--org",
            org,
            "--project-name",
            name,
            str(directory),
        ],
        on_line=on_log,
    )
    if exit_code != 0:
        raise CissyError(
            "Could not create the Flutter project. The log above has the "
            "details from Flutter itself."
        )


def _write_state(directory: Path, *, name: str, org: str) -> None:
    (directory / STATE_FILE).write_text(
        json.dumps({"name": name, "org": org}, indent=2) + "\n", encoding="utf-8"
    )


# ── files ────────────────────────────────────────────────────────────────


def _write(path: Path, contents: str) -> None:
    """Write only when the content differs.

    Gradle and the Dart compiler both key off modification times, so rewriting
    identical files would discard incremental state and slow every build.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.read_text(encoding="utf-8") == contents:
        return
    path.write_text(contents, encoding="utf-8", newline="\n")


def _write_main_activity(directory: Path, config: AppConfig) -> None:
    """Place MainActivity at the path its package declaration claims.

    `flutter create` puts it under the org it was given. If the package id later
    changes, the old file is left behind declaring the wrong package, and the
    build fails with a message that does not mention any of this.
    """
    kotlin_root = directory / "android" / "app" / "src" / "main" / "kotlin"
    target = kotlin_root.joinpath(*config.android_package_id.split(".")) / "MainActivity.kt"

    for stale in kotlin_root.rglob("MainActivity.kt"):
        if stale != target:
            stale.unlink()

    _write(target, template.main_activity(config))

    # Leave no empty package directories behind; Gradle ignores them but they
    # make the generated project confusing to read.
    for path in sorted(kotlin_root.rglob("*"), reverse=True):
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()


def _remove_default_test(directory: Path) -> None:
    """Delete the scaffold's placeholder widget test.

    `flutter create` writes a test that references `MyApp`, which this app does
    not have. Left in place it is a hard analyzer error, so the generated
    project fails `flutter analyze` and `flutter test` on a clean checkout -
    the first thing anyone opening it on a Mac would hit.

    Not replaced with a working test: pumping the widget tree would instantiate
    the platform WebView, which has no implementation in the test environment.
    """
    default_test = directory / "test" / "widget_test.dart"
    if default_test.is_file():
        default_test.unlink()
    test_dir = directory / "test"
    if test_dir.is_dir() and not any(test_dir.iterdir()):
        test_dir.rmdir()


# ── Android toolchain pin ────────────────────────────────────────────────

# The newest Android Gradle Plugin flutter_inappwebview can be built with, and
# the Gradle release that pairs with it.
#
# AGP 9 turned `getDefaultProguardFile('proguard-android.txt')` from a tolerated
# call into a hard error. flutter_inappwebview_android 1.1.3 still makes that
# call, its stable release is 22 months old, and the fix exists only in a beta -
# so on any Flutter new enough to scaffold AGP 9, every build fails while
# evaluating the plugin, before compiling a line of app code.
#
# This exact pair is what Flutter 3.35.5 scaffolds, and it is the combination
# that has actually produced a working release APK here. Remove the pin once the
# plugin ships a stable release that survives AGP 9.
PINNED_AGP = "8.9.1"
PINNED_GRADLE = "8.12"

_AGP_LINE = re.compile(
    r'(id\("com\.android\.application"\)\s+version\s+")([^"]+)(")'
)
_GRADLE_DIST = re.compile(r"(distributionUrl=.*gradle-)([^-]+)(-all\.zip)")


def _pin_android_toolchain(directory: Path, on_log: LogSink) -> None:
    """Hold AGP at a version the WebView plugin can still be built against.

    Only steps in when the scaffold asks for AGP 9 or newer. A Flutter that
    already scaffolds AGP 8 is left exactly as it is - the narrower the
    intervention, the less there is to undo later.
    """
    settings = directory / "android" / "settings.gradle.kts"
    if not settings.is_file():
        return

    contents = settings.read_text(encoding="utf-8")
    found = _AGP_LINE.search(contents)
    if not found:
        # A scaffold shaped differently to every version seen so far. Say so
        # rather than pinning something that was not understood.
        on_log(
            "WARNING: could not find the Android Gradle Plugin version in "
            "settings.gradle.kts, so it was left alone. If the build fails "
            "while evaluating a flutter_ plugin, this is why."
        )
        return

    current = found.group(2)
    if _major(current) < 9:
        return

    settings.write_text(
        _AGP_LINE.sub(rf"\g<1>{PINNED_AGP}\g<3>", contents, count=1), encoding="utf-8"
    )
    on_log(
        f"Pinned the Android Gradle Plugin from {current} to {PINNED_AGP} - "
        f"the WebView plugin cannot be built with AGP 9."
    )

    wrapper = (
        directory / "android" / "gradle" / "wrapper" / "gradle-wrapper.properties"
    )
    if not wrapper.is_file():
        return

    # AGP 8.9 does not run under Gradle 9, so pinning one without the other
    # trades this failure for a less obvious one.
    wrapper_contents = wrapper.read_text(encoding="utf-8")
    match = _GRADLE_DIST.search(wrapper_contents)
    if not match or _major(match.group(2)) < 9:
        return

    wrapper.write_text(
        _GRADLE_DIST.sub(rf"\g<1>{PINNED_GRADLE}\g<3>", wrapper_contents, count=1),
        encoding="utf-8",
    )
    on_log(f"Pinned Gradle from {match.group(2)} to {PINNED_GRADLE} to match.")


def _major(version: str) -> int:
    head = version.split(".", 1)[0].strip()
    return int(head) if head.isdigit() else 0


def _copy_assets(
    config: AppConfig, store: ProjectStore, directory: Path
) -> str | None:
    """Copy the splash image in, and return its pubspec asset path."""
    if not config.splash_file:
        return None
    source = store.assets_dir(config.id) / config.splash_file
    if not source.is_file():
        return None
    target = directory / "assets" / config.splash_file
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return f"assets/{config.splash_file}"


OFFLINE_ASSET = "assets/offline.html"


def _write_offline_html(config: AppConfig, directory: Path) -> str | None:
    """Bake the developer's own offline screen in, and return its asset path.

    The scaffold survives between builds, so switching the custom screen off
    must remove the file it left behind - pubspec no longer lists it, but a
    stale copy would make "did my change take?" needlessly confusing.
    """
    target = directory / OFFLINE_ASSET
    if not (config.offline_fallback_enabled and config.offline_custom_html):
        target.unlink(missing_ok=True)
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(config.offline_custom_html, encoding="utf-8")
    return OFFLINE_ASSET


def _apply_launch_screen(
    config: AppConfig, store: ProjectStore, directory: Path
) -> None:
    """Make the tap-to-app moment show the splash, not a flash of the icon.

    Two separate mechanisms, because Android has two launch phases:

    - The launch window (all versions): `launch_background.xml` is rewritten
      to draw the splash image, so the first frame the app itself shows is
      already the splash rather than a white screen.
    - The system splash (Android 12+): the OS draws the launcher icon before
      the app starts and cannot be skipped, but a values-v31 LaunchTheme
      hands it a transparent icon, so that phase is a plain surface instead
      of the icon.

    With no splash image everything is put back to the scaffold's stock
    behaviour - the overrides are removed, not left half-applied, because the
    scaffold survives between builds.
    """
    res = directory / "android" / "app" / "src" / "main" / "res"
    if not (directory / "android").is_dir():
        return

    source = (
        store.assets_dir(config.id) / config.splash_file
        if config.splash_file
        else None
    )
    has_splash = source is not None and source.is_file()

    drawable = res / "drawable"
    drawable.mkdir(parents=True, exist_ok=True)

    # The extension may have changed between uploads (png today, jpg
    # tomorrow), and two files with one resource name is a build error.
    for stale in drawable.glob(f"{template.SPLASH_DRAWABLE}.*"):
        stale.unlink()

    if has_splash:
        # aapt knows .png and .jpg; a .jpeg upload is stored under .jpg.
        suffix = ".jpg" if source.suffix.lower() == ".jpeg" else source.suffix.lower()
        shutil.copy2(source, drawable / f"{template.SPLASH_DRAWABLE}{suffix}")
        _write(
            drawable / f"{template.SPLASH_DRAWABLE}_icon.xml",
            template.splash_icon_drawable(),
        )
    else:
        icon_drawable = drawable / f"{template.SPLASH_DRAWABLE}_icon.xml"
        if icon_drawable.is_file():
            icon_drawable.unlink()

    # The icon splash has no image for the launch window to draw, so the window
    # is its background colour instead - the same colour the first Flutter
    # frame paints, which makes the handover invisible. Written per-variant so
    # a dark-mode phone gets the dark one.
    icon_splash = config.splash_style != "image"
    if icon_splash:
        for variant, colour in (
            ("values", config.splash_bg_light or DEFAULT_SPLASH_LIGHT),
            ("values-night", config.splash_bg_dark or DEFAULT_SPLASH_DARK),
        ):
            _write(
                res / variant / "cissy_splash.xml",
                template.splash_colour_resource(colour),
            )
    else:
        for variant in ("values", "values-night"):
            stale = res / variant / "cissy_splash.xml"
            if stale.is_file():
                stale.unlink()

    background = template.launch_background(
        has_splash, colour=DEFAULT_SPLASH_LIGHT if icon_splash else ""
    )
    for variant in ("drawable", "drawable-v21"):
        _write(res / variant / "launch_background.xml", background)

    # Both v31 qualifiers, because in dark mode `values-night` outranks
    # `values-v31` - without the night-v31 file, dark-mode phones would show
    # the icon flash this exists to remove.
    for variant, night in (("values-v31", False), ("values-night-v31", True)):
        path = res / variant / "styles.xml"
        if has_splash or icon_splash:
            colour = ""
            if icon_splash:
                colour = (
                    (config.splash_bg_dark or DEFAULT_SPLASH_DARK)
                    if night
                    else (config.splash_bg_light or DEFAULT_SPLASH_LIGHT)
                )
            _write(path, template.styles_v31(night=night, colour=colour))
        elif path.is_file():
            path.unlink()


def _copy_icon(config: AppConfig, store: ProjectStore, directory: Path) -> str | None:
    """Copy the launcher icon in, and return its pubspec path.

    Only copied here - turning it into the dozens of sizes Android and iOS
    actually read is left to flutter_launcher_icons at build time, because
    doing it here would mean an image library and this server has no
    dependencies to spend.
    """
    if not config.icon_file:
        return None
    source = store.assets_dir(config.id) / config.icon_file
    if not source.is_file():
        return None
    target = directory / "assets" / "icon" / config.icon_file
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return f"assets/icon/{config.icon_file}"


# ── the status-bar notification icon ─────────────────────────────────────


def _apply_notification_icon(
    config: AppConfig, directory: Path, icon_asset: str | None
) -> None:
    """The Android status-bar icon, or take every trace of it back out.

    Android renders only the alpha channel of a notification's small icon, so
    the launcher icon shows up as a featureless tinted square. The real fix -
    a white-on-transparent silhouette traced from the logo - needs an image
    library, so `tool/notification_icon.dart` is written here and run by the
    build, after `pub get` has fetched one.

    What is written here is the guarantee underneath that: a plain placeholder
    PNG for every density, so the resource the manifest and the Dart both name
    exists even in a project that was downloaded and built by hand. The worst
    case is a plain square in the status bar - exactly what shipping the
    launcher icon used to look like - never a build that fails over a missing
    resource. The tool overwrites these with the traced silhouette.
    """
    if not (directory / "android").is_dir():
        return

    tool = directory / "tool" / "notification_icon.dart"
    res = directory / "android" / "app" / "src" / "main" / "res"
    colour_file = res / "values" / f"{template.NOTIFICATION_COLOUR}.xml"

    if not (config.push_enabled and icon_asset):
        # The scaffold survives between builds, so switching push off (or
        # removing the icon) has to remove all of this, not orphan it.
        tool.unlink(missing_ok=True)
        colour_file.unlink(missing_ok=True)
        for density in template.STAT_ICON_SIZES:
            stale = res / f"drawable-{density}" / f"{template.STAT_ICON}.png"
            stale.unlink(missing_ok=True)
        return

    tool.parent.mkdir(parents=True, exist_ok=True)
    _write(tool, template.notification_icon_tool(icon_asset))

    if config.theme_color:
        _write(
            colour_file, template.notification_colour_resource(config.theme_color)
        )
    else:
        colour_file.unlink(missing_ok=True)

    # Only when missing: a silhouette a previous build traced is better than
    # a square, and the next build's tool run replaces it either way.
    for density, size in template.STAT_ICON_SIZES.items():
        target = res / f"drawable-{density}" / f"{template.STAT_ICON}.png"
        if not target.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(_plain_square_png(size))


def _plain_square_png(size: int) -> bytes:
    """A solid white PNG, written with the standard library.

    Deliberately unremarkable: it is the placeholder under the traced
    silhouette, and a white square is what the status bar was already showing
    before any of this existed.
    """
    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (
            struct.pack(">I", len(data))
            + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    # 8-bit RGBA, one filter byte (0, "none") per row.
    raw = b"".join(b"\x00" + b"\xff\xff\xff\xff" * size for _ in range(size))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


# ── Firebase ─────────────────────────────────────────────────────────────

# The Google Services Gradle plugin, which turns google-services.json into the
# resources firebase_core reads at startup. Pinned for the same reason the AGP
# version is: a generated project should build the same way in six months.
GOOGLE_SERVICES_VERSION = "4.4.2"
GOOGLE_SERVICES_ID = "com.google.gms.google-services"

_PLUGINS_BLOCK = re.compile(r"plugins\s*\{", re.MULTILINE)


def _apply_firebase(
    config: AppConfig, store: ProjectStore, directory: Path, on_log: LogSink
) -> None:
    """Put the customer's Firebase configuration into the project.

    The package check is repeated here even though the upload already made it.
    The Android package id is editable afterwards, so a file that matched in
    March can stop matching in April without anyone touching it - and the
    result would be an app that builds, signs, installs and never receives a
    notification. Better to refuse the build.

    Switching push off removes the files again. The scaffold survives between
    builds, so a stale google-services.json would otherwise keep initialising
    Firebase in an app whose Dart no longer mentions it.
    """
    android_target = directory / "android" / "app" / firebase.ANDROID_FILENAME
    ios_target = directory / "ios" / "Runner" / firebase.IOS_FILENAME

    if not config.push_enabled:
        for stale in (android_target, ios_target):
            stale.unlink(missing_ok=True)
        _patch_google_services(directory, enabled=False, on_log=on_log)
        return

    source = (
        store.assets_dir(config.id) / config.firebase_android_file
        if config.firebase_android_file
        else None
    )
    if source is None or not source.is_file():
        raise CissyError(
            f"Push notifications are on, but no {firebase.ANDROID_FILENAME} has "
            f"been uploaded. Add an Android app in your Firebase project using "
            f"the package name {config.android_package_id}, download the file, "
            f"and upload it on the Notifications page."
        )

    app = firebase.read(source.read_bytes(), firebase.ANDROID)
    firebase.check(app, config.android_package_id)
    android_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, android_target)
    on_log(f"Firebase project {app.project_id} configured for Android.")

    ios_source = (
        store.assets_dir(config.id) / config.firebase_ios_file
        if config.firebase_ios_file
        else None
    )
    if ios_source is not None and ios_source.is_file():
        ios_app = firebase.read(ios_source.read_bytes(), firebase.IOS)
        firebase.check(ios_app, config.ios_bundle_id)
        ios_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ios_source, ios_target)
        on_log(f"Firebase project {ios_app.project_id} configured for iOS.")
    else:
        ios_target.unlink(missing_ok=True)
        on_log(
            "No iOS Firebase file uploaded - the Android app is unaffected, but "
            "the project opened on a Mac will not receive notifications."
        )

    _patch_google_services(directory, enabled=True, on_log=on_log)


def _patch_google_services(
    directory: Path, *, enabled: bool, on_log: LogSink
) -> None:
    """Declare and apply the Google Services plugin, or take it back out.

    Written the way `signing.patch_gradle` is: recognise the shape, change it,
    and say so loudly when neither shape is recognised. A silent no-op here
    produces an app that builds and installs and never receives anything, which
    is the failure this whole path exists to prevent.
    """
    settings = directory / "android" / "settings.gradle.kts"
    build = directory / "android" / "app" / "build.gradle.kts"
    if not settings.is_file() or not build.is_file():
        if enabled:
            raise CissyError(
                "The generated Android project is not in the expected format, "
                "so Firebase could not be wired into it."
            )
        return

    declaration = (
        f'    id("{GOOGLE_SERVICES_ID}") version "{GOOGLE_SERVICES_VERSION}" '
        f"apply false"
    )
    application = f'    id("{GOOGLE_SERVICES_ID}")'

    for path, line in ((settings, declaration), (build, application)):
        contents = path.read_text(encoding="utf-8")
        present = GOOGLE_SERVICES_ID in contents

        if not enabled:
            if present:
                kept = [
                    row
                    for row in contents.split("\n")
                    if GOOGLE_SERVICES_ID not in row
                ]
                _write(path, "\n".join(kept))
                on_log(f"Removed the Firebase plugin from {path.name}.")
            continue

        if present:
            continue

        match = _PLUGINS_BLOCK.search(contents)
        if not match:
            raise CissyError(
                f"Could not find the plugins block in {path.name}, so the "
                f"Firebase plugin was not added. Notifications would not work "
                f"in the built app, so the build has been stopped rather than "
                f"producing one that looks fine."
            )
        cut = match.end()
        _write(path, contents[:cut] + "\n" + line + contents[cut:])
        on_log(f"Added the Firebase plugin to {path.name}.")


# ── Info.plist ───────────────────────────────────────────────────────────


def _patch_info_plist(directory: Path, config: AppConfig, on_log: LogSink) -> None:
    path = directory / "ios" / "Runner" / "Info.plist"
    if not path.is_file():
        on_log("No iOS project found; skipping Info.plist.")
        return

    contents = path.read_text(encoding="utf-8")
    contents = _set_plist_string(contents, "CFBundleDisplayName", config.display_name)
    contents = _set_plist_string(contents, "CFBundleName", template.project_name(config))

    for key, value in template.ios_usage_descriptions(config).items():
        contents = _set_plist_string(contents, key, value)

    if "Deep links" in set(config.features):
        contents = _add_url_scheme(contents, template.deep_link_scheme(config))

    # Lets iOS wake the app for a message it has not displayed yet. Removed
    # again when push is switched off - the scaffold survives between builds.
    contents = _set_plist_array(
        contents,
        "UIBackgroundModes",
        ["remote-notification"] if config.push_enabled else [],
    )

    entitlements = directory / "ios" / "Runner" / "Runner.entitlements"
    if config.push_enabled:
        _write(entitlements, ENTITLEMENTS)
        on_log(
            "Wrote ios/Runner/Runner.entitlements. On the Mac, add the Push "
            "Notifications capability in Xcode - that is what points the build "
            "at it."
        )
    else:
        entitlements.unlink(missing_ok=True)

    _write(path, contents)


_PLIST_ARRAY = re.compile(
    r"\n?\t*<key>{key}</key>\s*<array>.*?</array>", re.DOTALL
)


def _set_plist_array(contents: str, key: str, values: list[str]) -> str:
    """Set, replace or remove an array entry.

    `_set_plist_string` only knows how to write strings, and
    `UIBackgroundModes` is an array - the entry iOS reads to decide whether an
    app may be woken by a notification it has not shown yet.

    An empty list removes the entry rather than writing an empty array,
    because the scaffold survives between builds: turning push off has to take
    the entry back out, not leave a hollow one behind.
    """
    pattern = re.compile(
        _PLIST_ARRAY.pattern.format(key=re.escape(key)), re.DOTALL
    )
    if not values:
        return pattern.sub("", contents)

    rows = "".join(f"\t\t<string>{value}</string>\n" for value in values)
    entry = f"\n\t<key>{key}</key>\n\t<array>\n{rows}\t</array>"
    if pattern.search(contents):
        return pattern.sub(entry, contents, count=1)

    marker = "</dict>\n</plist>"
    if marker not in contents:
        raise CissyError("The iOS Info.plist is not in the expected format.")
    return contents.replace(marker, entry.lstrip("\n") + "\n" + marker, 1)


# What Xcode writes when somebody ticks Push Notifications. Generated so the
# file exists and says the right thing; it only takes effect once the
# capability is added in Xcode, which is also what points the build at it.
# Editing project.pbxproj from here to do that ourselves would mean writing a
# format with no public specification, and failing on the customer's Mac where
# we could not see it.
ENTITLEMENTS = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
\t<key>aps-environment</key>
\t<string>development</string>
</dict>
</plist>
"""


def _set_plist_string(contents: str, key: str, value: str) -> str:
    """Set a string entry, replacing an existing one or appending a new one.

    Text manipulation rather than plistlib because the file is a template full
    of `$(PRODUCT_NAME)` placeholders that a parse-and-rewrite would reformat,
    turning every generated diff into noise.
    """
    escaped = (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    pattern = re.compile(
        r"(<key>" + re.escape(key) + r"</key>\s*<string>)(.*?)(</string>)",
        re.DOTALL,
    )
    if pattern.search(contents):
        return pattern.sub(lambda m: m.group(1) + escaped + m.group(3), contents, count=1)

    entry = f"\t<key>{key}</key>\n\t<string>{escaped}</string>\n"
    marker = "</dict>\n</plist>"
    if marker not in contents:
        raise CissyError("The iOS Info.plist is not in the expected format.")
    return contents.replace(marker, entry + marker, 1)


def _add_url_scheme(contents: str, scheme: str) -> str:
    if "<key>CFBundleURLTypes</key>" in contents:
        return contents
    entry = (
        "\t<key>CFBundleURLTypes</key>\n"
        "\t<array>\n"
        "\t\t<dict>\n"
        "\t\t\t<key>CFBundleTypeRole</key>\n"
        "\t\t\t<string>Editor</string>\n"
        "\t\t\t<key>CFBundleURLSchemes</key>\n"
        "\t\t\t<array>\n"
        f"\t\t\t\t<string>{scheme}</string>\n"
        "\t\t\t</array>\n"
        "\t\t</dict>\n"
        "\t</array>\n"
    )
    marker = "</dict>\n</plist>"
    return contents.replace(marker, entry + marker, 1)
