"""Release signing for Android.

Passwords live for the length of one build. They are never written to
`config.json`, never logged, and the `key.properties` file that carries them to
Gradle is deleted as soon as the build finishes and excluded from every
download.

That matters more than it might seem: a leaked upload key cannot be revoked.
Google will not reset it, and losing control of it means losing the ability to
ship updates to an app that is already installed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .errors import CissyError, ValidationError

MARKER = "// Managed by Cissy: release signing."


@dataclass(frozen=True)
class SigningCredentials:
    """Everything needed to sign one build. Never persisted."""

    keystore_path: Path
    key_alias: str
    store_password: str
    key_password: str

    @property
    def properties_contents(self) -> str:
        """The `key.properties` Gradle reads.

        Backslashes are escape characters in a Java properties file, so a
        Windows-style path has to be written with forward slashes. Java resolves
        those correctly on every platform.
        """
        store_file = str(self.keystore_path).replace("\\", "/")
        return (
            f"storePassword={self.store_password}\n"
            f"keyPassword={self.key_password}\n"
            f"keyAlias={self.key_alias}\n"
            f"storeFile={store_file}\n"
        )


def validate(credentials: SigningCredentials) -> None:
    if not credentials.keystore_path.is_file():
        raise ValidationError(
            f"The keystore is missing at {credentials.keystore_path}. "
            f"Upload it again."
        )
    if not credentials.key_alias.strip():
        raise ValidationError("The key alias is required to sign a release build.")
    if not credentials.store_password or not credentials.key_password:
        raise ValidationError(
            "Both the keystore password and the key password are needed. They "
            "are not stored between builds, so they have to be entered each time."
        )


def apply(project_dir: Path, credentials: SigningCredentials | None) -> None:
    """Prepare the generated project to sign — or deliberately not to."""
    key_properties = project_dir / "android" / "key.properties"
    gradle = project_dir / "android" / "app" / "build.gradle.kts"

    if credentials is None:
        # A leftover key.properties would silently sign a later build with old
        # credentials, which is the kind of thing nobody notices until the Play
        # console rejects an upload for the wrong signature.
        key_properties.unlink(missing_ok=True)
        return

    validate(credentials)
    key_properties.parent.mkdir(parents=True, exist_ok=True)
    key_properties.write_text(credentials.properties_contents, encoding="utf-8")

    if not gradle.is_file():
        raise CissyError(
            "The generated Android project has no build.gradle.kts. Regenerate "
            "the project and try again."
        )
    gradle.write_text(patch_gradle(gradle.read_text(encoding="utf-8")), encoding="utf-8")


def cleanup(project_dir: Path) -> None:
    """Remove the passwords from disk. Safe to call more than once."""
    (project_dir / "android" / "key.properties").unlink(missing_ok=True)


# ── the Gradle patch ─────────────────────────────────────────────────────

_IMPORTS = "import java.io.FileInputStream\nimport java.util.Properties\n\n"

_LOADER = f"""
{MARKER}
val cissyKeystoreProperties = Properties().apply {{
    val file = rootProject.file("key.properties")
    if (file.exists()) {{
        FileInputStream(file).use {{ load(it) }}
    }}
}}
"""

_SIGNING_CONFIGS = """
    signingConfigs {
        create("release") {
            keyAlias = cissyKeystoreProperties["keyAlias"] as String?
            keyPassword = cissyKeystoreProperties["keyPassword"] as String?
            storeFile = cissyKeystoreProperties["storeFile"]?.let { file(it) }
            storePassword = cissyKeystoreProperties["storePassword"] as String?
        }
    }
"""


def patch_gradle(contents: str) -> str:
    """Add a release signing config, idempotently.

    Kotlin DSL is strict about order: imports must come before everything, and
    Gradle requires `plugins {}` to be the first *statement*, so the property
    loading has to sit between the two rather than at the top.

    Written to tolerate template changes between Flutter versions — the debug
    line is replaced if present, otherwise the release build type is given a
    signing config directly. If neither shape is found it raises, because
    silently producing a debug-signed artifact that looks like a release one is
    the worst available outcome.
    """
    if MARKER in contents:
        return contents

    patched = _IMPORTS + contents if not contents.startswith("import ") else contents

    match = re.search(r"^plugins\s*\{", patched, re.MULTILINE)
    if not match:
        raise CissyError(
            "The generated build.gradle.kts has no plugins block, so Cissy "
            "cannot add a signing config to it."
        )
    end = _closing_brace(patched, match.end() - 1)
    patched = patched[: end + 1] + "\n" + _LOADER + patched[end + 1 :]

    android = re.search(r"^android\s*\{", patched, re.MULTILINE)
    if not android:
        raise CissyError(
            "The generated build.gradle.kts has no android block, so Cissy "
            "cannot add a signing config to it."
        )
    patched = (
        patched[: android.end()] + "\n" + _SIGNING_CONFIGS + patched[android.end() :]
    )

    debug_line = 'signingConfig = signingConfigs.getByName("debug")'
    if debug_line in patched:
        patched = patched.replace(
            debug_line, 'signingConfig = signingConfigs.getByName("release")', 1
        )
    else:
        patched = _sign_release_build_type(patched)

    # The template's own comments describe debug signing and would now say the
    # opposite of what the file does.
    for stale in (
        "            // TODO: Add your own signing config for the release build.\n",
        "            // Signing with the debug keys for now, so `flutter run --release` works.\n",
    ):
        patched = patched.replace(stale, "", 1)

    return patched


def _sign_release_build_type(contents: str) -> str:
    match = re.search(r"^(\s*)release\s*\{", contents, re.MULTILINE)
    if not match:
        raise CissyError(
            "Could not find the release build type in build.gradle.kts, so "
            "Cissy cannot be sure the artifact would be signed with your "
            "upload key. Refusing rather than producing a debug-signed build."
        )
    indent = match.group(1) + "    "
    insert = f'\n{indent}signingConfig = signingConfigs.getByName("release")'
    return contents[: match.end()] + insert + contents[match.end() :]


def _closing_brace(text: str, open_index: int) -> int:
    """Index of the brace closing the one at `open_index`."""
    depth = 0
    for index in range(open_index, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return index
    raise CissyError("The generated build.gradle.kts has an unbalanced block.")
