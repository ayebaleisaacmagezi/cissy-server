"""Reading the Firebase configuration files the customer uploads.

Two files in two formats, and one question worth asking of both: does this
belong to the app being built? A `google-services.json` downloaded from the
wrong Firebase app produces a project that compiles, installs, runs, and never
receives a notification. Nothing later in the pipeline notices, and the customer
finds out from a user.

So the package name is checked here, and checked twice - once when the file
arrives, and again at generate time. The second check is not redundant: the
Android package id is editable on the Identity page, so a file that matched when
it was uploaded can stop matching weeks later without anyone touching it.

`plistlib` and `json` are both standard library, so neither format costs a
dependency.

Nothing here knows about HTTP or about the filesystem.
"""

from __future__ import annotations

import json
import plistlib
from dataclasses import dataclass

from .errors import ValidationError

ANDROID = "android"
IOS = "ios"

# What each platform's file is called when Firebase hands it over. The customer
# is told to look for these names, and the generated project needs them spelled
# exactly this way or the Google Services plugin will not find them.
ANDROID_FILENAME = "google-services.json"
IOS_FILENAME = "GoogleService-Info.plist"


@dataclass(frozen=True)
class FirebaseApp:
    """One app registered inside a customer's Firebase project."""

    platform: str
    project_id: str
    app_id: str
    identifier: str
    # Every identifier the file carries, which is what an error message needs:
    # one file can register several apps, and "it belongs to X" is only useful
    # when X is the whole list.
    identifiers: tuple[str, ...]

    @property
    def filename(self) -> str:
        return ANDROID_FILENAME if self.platform == ANDROID else IOS_FILENAME


def read(data: bytes, platform: str) -> FirebaseApp:
    """Parse an uploaded configuration file, or say what is wrong with it."""
    if platform == ANDROID:
        return _read_android(data)
    return _read_ios(data)


def _read_android(data: bytes) -> FirebaseApp:
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValidationError(
            f"That is not a readable {ANDROID_FILENAME}. Download it again from "
            f"your Firebase project settings.",
            detail=str(error),
        ) from error

    if not isinstance(parsed, dict):
        raise ValidationError(f"That {ANDROID_FILENAME} is not in the expected format.")

    project_id = _dig(parsed, "project_info", "project_id")
    if not project_id:
        raise ValidationError(
            f"That file has no Firebase project in it. Make sure it is the "
            f"{ANDROID_FILENAME} from Project settings, not another file."
        )

    # One file can carry several apps - Firebase writes every Android app in
    # the project into it, and the Gradle plugin picks the one matching the
    # applicationId. So this collects them all and lets the caller decide.
    clients = parsed.get("client")
    packages: list[str] = []
    app_ids: dict[str, str] = {}
    if isinstance(clients, list):
        for client in clients:
            if not isinstance(client, dict):
                continue
            name = _dig(client, "client_info", "android_client_info", "package_name")
            if name and name not in packages:
                packages.append(name)
                app_ids[name] = _dig(client, "client_info", "mobilesdk_app_id")

    if not packages:
        raise ValidationError(
            f"That {ANDROID_FILENAME} has no Android app registered in it. In "
            f"Firebase, add an Android app first, then download the file again."
        )

    return FirebaseApp(
        platform=ANDROID,
        project_id=project_id,
        app_id=app_ids.get(packages[0], ""),
        identifier=packages[0],
        identifiers=tuple(packages),
    )


def _read_ios(data: bytes) -> FirebaseApp:
    try:
        parsed = plistlib.loads(data)
    except Exception as error:  # plistlib raises several unrelated types
        raise ValidationError(
            f"That is not a readable {IOS_FILENAME}. Download it again from "
            f"your Firebase project settings.",
            detail=str(error),
        ) from error

    if not isinstance(parsed, dict):
        raise ValidationError(f"That {IOS_FILENAME} is not in the expected format.")

    project_id = str(parsed.get("PROJECT_ID") or "").strip()
    bundle_id = str(parsed.get("BUNDLE_ID") or "").strip()
    if not project_id or not bundle_id:
        raise ValidationError(
            f"That {IOS_FILENAME} is missing its project or bundle ID. Make "
            f"sure it is the file Firebase produced for an iOS app."
        )

    return FirebaseApp(
        platform=IOS,
        project_id=project_id,
        app_id=str(parsed.get("GOOGLE_APP_ID") or "").strip(),
        identifier=bundle_id,
        identifiers=(bundle_id,),
    )


def matches(app: FirebaseApp, expected: str) -> bool:
    """Whether the file registers the app being built.

    Case-insensitive for iOS, where Apple treats bundle ids that way and the
    config schema does not force one case.
    """
    wanted = expected.strip().lower()
    return any(name.strip().lower() == wanted for name in app.identifiers)


def check(app: FirebaseApp, expected: str) -> None:
    """Raise unless the file belongs to the app being built."""
    if matches(app, expected):
        return

    belongs = ", ".join(app.identifiers)
    label = "package name" if app.platform == ANDROID else "bundle ID"
    raise ValidationError(
        f"This Firebase configuration belongs to {belongs}, but this app's "
        f"{label} is {expected}. Notifications would never arrive. In Firebase, "
        f"add an app using {expected} and download that app's {app.filename}.",
        detail=belongs,
    )


def _dig(data: object, *keys: str) -> str:
    """Follow a path of dict keys, returning "" rather than raising.

    An uploaded file is arbitrary JSON, so every level has to be checked. A
    missing key here means a clear message from the caller, not a 500.
    """
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return ""
        current = current.get(key)
    return str(current).strip() if isinstance(current, str) else ""
