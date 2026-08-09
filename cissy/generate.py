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

from . import template
from .config import AppConfig
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
        template.main_dart(config, splash_asset, offline_asset),
    )
    _write(
        directory / "android" / "app" / "src" / "main" / "AndroidManifest.xml",
        template.android_manifest(config),
    )
    _apply_launch_screen(config, store, directory)
    _write(directory / "android" / "gradle.properties", template.gradle_properties())
    _write_main_activity(directory, config)
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

    background = template.launch_background(has_splash)
    for variant in ("drawable", "drawable-v21"):
        _write(res / variant / "launch_background.xml", background)

    # Both v31 qualifiers, because in dark mode `values-night` outranks
    # `values-v31` - without the night-v31 file, dark-mode phones would show
    # the icon flash this exists to remove.
    for variant, night in (("values-v31", False), ("values-night-v31", True)):
        path = res / variant / "styles.xml"
        if has_splash:
            _write(path, template.styles_v31(night=night))
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

    _write(path, contents)


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
