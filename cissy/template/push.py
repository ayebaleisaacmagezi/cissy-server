"""Push notifications in the generated app.

The customer owns the Firebase project and sends from their own backend, so
almost nothing here is about sending. What the app has to do is narrower:
register, ask for permission at a moment the user can say yes to, subscribe to
the categories they chose, and open the right page when a notification is
tapped.

Three things drive the shape of this file.

A notification payload arrives from the network, so the router treats it as
untrusted: a fixed set of actions, a URL resolved against the app's own home
address, and the same allowed-domain check the WebView uses. A notification can
never name arbitrary code or send somebody to an arbitrary site.

The tap has to reach the WebView from three different places - a foreground
message, a background tap, and a cold start from a terminated app - and in the
last case before there is a widget tree at all. So the destination is published
to a top-level `ValueNotifier` and the screen listens, rather than push knowing
anything about the shell.

The background handler runs in its own isolate. It has to be a top-level
function marked `vm:entry-point`, and nothing reachable only from the widget
tree exists inside it.
"""

from __future__ import annotations

import json

from ..config import AppConfig
from .common import dart_string

# Pinned like everything else. firebase_messaging carries firebase_core's
# platform channels, and flutter_local_notifications is what actually puts a
# banner on screen when a message arrives with the app already open - FCM hands
# those to the app rather than displaying them.
PUSH_DEPENDENCIES = {
    "firebase_core": "^3.8.0",
    "firebase_messaging": "^15.1.5",
    "flutter_local_notifications": "^18.0.1",
}

# The Android channel every notification lands in unless the payload names
# another. Declared in the manifest as well, so a notification that arrives
# while the app has never been opened still has somewhere to go.
CHANNEL_ID = "cissy_default"
CHANNEL_NAME = "Notifications"

# The status-bar icon. Android keeps only the alpha channel of a notification's
# small icon and tints the silhouette, so the full-colour launcher icon renders
# as a featureless square. A dedicated white-on-transparent drawable is derived
# from the uploaded logo at build time by tool/notification_icon.dart; the
# resource name is shared between the Dart below, the manifest, and that tool.
STAT_ICON = "ic_stat_notify"

# One PNG per density bucket, at Android's canonical 24dp small-icon size.
STAT_ICON_SIZES = {"mdpi": 24, "hdpi": 36, "xhdpi": 48, "xxhdpi": 72, "xxxhdpi": 96}


def imports(config: AppConfig) -> list[str]:
    return [
        "import 'package:firebase_core/firebase_core.dart';",
        "import 'package:firebase_messaging/firebase_messaging.dart';",
        "import 'package:flutter_local_notifications/flutter_local_notifications.dart';",
    ]


def constants(config: AppConfig) -> str:
    """The configuration the generated push code reads."""
    topics = [
        {
            "id": topic.get("id", ""),
            "label": topic.get("label", ""),
            "default": bool(topic.get("default")),
        }
        for topic in config.push_topics
    ]
    title = config.push_prompt_title or "Stay updated"
    body = config.push_prompt_body or (
        "Get told when something happens that you care about. You can turn "
        "this off at any time."
    )
    return "\n".join([
        f"const pushTopics = <Map<String, Object>>{_dart_maps(topics)};",
        f"const pushPromptTitle = {dart_string(title)};",
        f"const pushPromptBody = {dart_string(body)};",
        f"const pushForeground = {dart_string(config.push_foreground)};",
        f"const pushTokenEndpoint = {dart_string(config.push_token_endpoint)};",
        f"const pushChannelId = {dart_string(CHANNEL_ID)};",
        f"const pushChannelName = {dart_string(CHANNEL_NAME)};",
        "",
        "/// A page a notification has asked the app to open.",
        "///",
        "/// A ValueNotifier rather than a call into the shell, because the tap",
        "/// that starts a terminated app is read before there is a widget tree",
        "/// to call into. The screen listens and loads whatever appears here.",
        "final pendingPushUrl = ValueNotifier<String?>(null);",
    ])


def _dart_maps(rows: list[dict[str, object]]) -> str:
    out = []
    for row in rows:
        out.append(
            "{"
            + ", ".join(
                f"{dart_string(key)}: "
                + (json.dumps(value) if isinstance(value, bool) else dart_string(value))
                for key, value in row.items()
            )
            + "}"
        )
    return "[" + ", ".join(out) + "]"


BACKGROUND_HANDLER = """\
/// A message that arrived with the app not running.
///
/// Flutter runs this in its own isolate, so it must be a top-level function
/// marked vm:entry-point and it must set Firebase up again - none of the
/// initialisation done for the UI exists here.
@pragma('vm:entry-point')
Future<void> pushBackgroundHandler(RemoteMessage message) async {
  await Firebase.initializeApp();
}
"""


def router(config: AppConfig) -> str:
    """The one place a notification payload is turned into an action."""
    return """\
/// Turns a notification payload into somewhere to go, or into nothing.
///
/// Everything here is deliberately closed rather than open. The action must be
/// one this app knows, the destination is resolved against the app's own home
/// URL so a relative "/orders/123" works, and the result has to survive the
/// same allowed-domain check the WebView applies to a tapped link. A payload
/// arrives from the network; it does not get to choose freely.
class PushRouter {
  static const _actions = <String>{'open_url', 'none'};

  static String? destination(Map<String, dynamic> data) {
    final action = (data['action'] ?? 'open_url').toString();
    if (!_actions.contains(action) || action == 'none') {
      return null;
    }
    final raw = (data['url'] ?? '').toString();
    if (raw.isEmpty) {
      return null;
    }
    final base = Uri.tryParse(homeUrl);
    final resolved = base == null ? Uri.tryParse(raw) : base.resolve(raw);
    if (resolved == null) {
      return null;
    }
    if (resolved.scheme != 'http' && resolved.scheme != 'https') {
      return null;
    }
    final host = resolved.host.toLowerCase();
    final permitted = allowedDomains.any(
      (domain) => host == domain || host.endsWith('.$domain'),
    );
    return permitted ? resolved.toString() : null;
  }

  /// Publish a tap. Safe to call before the app has a widget tree.
  static void open(Map<String, dynamic> data) {
    final target = destination(data);
    if (target != null) {
      pendingPushUrl.value = target;
    }
  }
}
"""


def service(config: AppConfig, *, has_icon: bool = False) -> str:
    """Registration, permission, categories, and what to do with a message.

    `has_icon` says whether a logo was uploaded, which is what decides the
    status-bar icon: with one, the silhouette derived from it; without one,
    the launcher mipmap, which at least always exists.
    """
    foreground = config.push_foreground
    small_icon = f"@drawable/{STAT_ICON}" if has_icon else "@mipmap/ic_launcher"

    if foreground == "notification":
        show = """\
    final notification = message.notification;
    if (notification == null) {
      return;
    }
    await _local.show(
      notification.hashCode,
      notification.title,
      notification.body,
      NotificationDetails(
        android: AndroidNotificationDetails(
          pushChannelId,
          pushChannelName,
          importance: Importance.high,
          priority: Priority.high,
        ),
        iOS: const DarwinNotificationDetails(),
      ),
      payload: jsonEncode(message.data),
    );
"""
    elif foreground == "banner":
        show = """\
    final notification = message.notification;
    if (notification == null) {
      return;
    }
    // A bar inside the app rather than a system notification: the user is
    // already looking at the app, so a notification shade entry is noise.
    final messenger = pushMessengerKey.currentState;
    final target = PushRouter.destination(message.data);
    messenger?.showMaterialBanner(
      MaterialBanner(
        content: Text(
          notification.body == null || notification.body!.isEmpty
              ? (notification.title ?? '')
              : '${notification.title ?? ''}\\n${notification.body}',
        ),
        actions: [
          if (target != null)
            TextButton(
              onPressed: () {
                messenger.hideCurrentMaterialBanner();
                pendingPushUrl.value = target;
              },
              child: const Text('Open'),
            ),
          TextButton(
            onPressed: messenger.hideCurrentMaterialBanner,
            child: const Text('Dismiss'),
          ),
        ],
      ),
    );
"""
    else:
        show = """\
    // Nothing visible, by configuration. The message is still recorded, so
    // anything that wanted to know it arrived can still find out.
"""

    token_report = """\
  /// Hand the registration token to the customer's own backend.
  ///
  /// Only when they configured somewhere to send it. With no endpoint the app
  /// never phones anywhere, and the token reaches their server through the
  /// JavaScript bridge instead - from a page where they already know who is
  /// logged in.
  static Future<void> _reportToken(String? token) async {
    if (pushTokenEndpoint.isEmpty || token == null || token.isEmpty) {
      return;
    }
    try {
      final client = HttpClient();
      final request = await client.postUrl(Uri.parse(pushTokenEndpoint));
      request.headers.contentType = ContentType.json;
      request.write(jsonEncode({
        'token': token,
        'platform': Platform.isIOS ? 'ios' : 'android',
        'appVersion': appVersion,
      }));
      final response = await request.close();
      await response.drain<void>();
      client.close();
    } catch (_) {
      // A token the backend did not receive is not worth interrupting anyone
      // over. onTokenRefresh will come round again.
    }
  }
"""

    return f"""\
/// Everything the app does about notifications.
///
/// Static because there is exactly one of it and because the background
/// isolate has to reach parts of it without a widget tree to hang it from.
class PushService {{
  static final _local = FlutterLocalNotificationsPlugin();
  static const _topicsKey = 'cissy.push.topics';

  /// Called once at startup, before runApp.
  static Future<void> initialise() async {{
    FirebaseMessaging.onBackgroundMessage(pushBackgroundHandler);

    await _local.initialize(
      const InitializationSettings(
        android: AndroidInitializationSettings('{small_icon}'),
        // All three false: the app asks for permission itself, at a moment the
        // user has a reason to say yes to. See requestPermission below.
        iOS: DarwinInitializationSettings(
          requestAlertPermission: false,
          requestBadgePermission: false,
          requestSoundPermission: false,
        ),
      ),
      onDidReceiveNotificationResponse: (response) {{
        final payload = response.payload;
        if (payload == null || payload.isEmpty) {{
          return;
        }}
        final decoded = jsonDecode(payload);
        if (decoded is Map<String, dynamic>) {{
          PushRouter.open(decoded);
        }}
      }},
    );

    await _local
        .resolvePlatformSpecificImplementation<
            AndroidFlutterLocalNotificationsPlugin>()
        ?.createNotificationChannel(
          const AndroidNotificationChannel(
            pushChannelId,
            pushChannelName,
            importance: Importance.high,
          ),
        );

    FirebaseMessaging.onMessage.listen(_onForeground);
    FirebaseMessaging.onMessageOpenedApp.listen(
      (message) => PushRouter.open(message.data),
    );

    // The tap that started a terminated app. Read before the first frame, and
    // published to the notifier the screen listens on.
    final initial = await FirebaseMessaging.instance.getInitialMessage();
    if (initial != null) {{
      PushRouter.open(initial.data);
    }}

    FirebaseMessaging.instance.onTokenRefresh.listen(_reportToken);

    // Already allowed on a previous run: pick the subscriptions back up
    // without asking again.
    if (await isAllowed()) {{
      await _afterPermission();
    }}
  }}

  static Future<void> _onForeground(RemoteMessage message) async {{
  {show}  }}

  static Future<bool> isAllowed() async {{
    final settings = await FirebaseMessaging.instance.getNotificationSettings();
    return settings.authorizationStatus == AuthorizationStatus.authorized ||
        settings.authorizationStatus == AuthorizationStatus.provisional;
  }}

  /// Ask the operating system. Only ever called after the user has said yes to
  /// the app's own explanation - both platforms give you one chance, and a
  /// refusal can only be undone by the user in system settings.
  static Future<bool> requestPermission() async {{
    final settings = await FirebaseMessaging.instance.requestPermission();
    final granted =
        settings.authorizationStatus == AuthorizationStatus.authorized ||
            settings.authorizationStatus == AuthorizationStatus.provisional;
    if (granted) {{
      await _afterPermission();
    }}
    return granted;
  }}

  static Future<void> _afterPermission() async {{
    await syncTopics();
    await _reportToken(await FirebaseMessaging.instance.getToken());
  }}

  static Future<String?> token() => FirebaseMessaging.instance.getToken();

  /// Which categories this install is subscribed to.
  ///
  /// Nothing stored means a fresh install, which takes the defaults set in the
  /// Studio. Once the user has touched the list, their choice is what counts.
  static Future<Set<String>> enabledTopics() async {{
    final prefs = await SharedPreferences.getInstance();
    final stored = prefs.getStringList(_topicsKey);
    if (stored != null) {{
      return stored.toSet();
    }}
    return {{
      for (final topic in pushTopics)
        if (topic['default'] == true) topic['id'] as String,
    }};
  }}

  static Future<void> setTopic(String id, bool on) async {{
    final prefs = await SharedPreferences.getInstance();
    final current = await enabledTopics();
    if (on) {{
      current.add(id);
    }} else {{
      current.remove(id);
    }}
    await prefs.setStringList(_topicsKey, current.toList());
    await syncTopics();
  }}

  static Future<void> syncTopics() async {{
    final on = await enabledTopics();
    for (final topic in pushTopics) {{
      final id = topic['id'] as String;
      try {{
        if (on.contains(id)) {{
          await FirebaseMessaging.instance.subscribeToTopic(id);
        }} else {{
          await FirebaseMessaging.instance.unsubscribeFromTopic(id);
        }}
      }} catch (_) {{
        // No connection, most likely. The next start syncs again.
      }}
    }}
  }}

{token_report}}}
"""


PROMPT_SHEET = """\
/// The app's own explanation, shown before the system prompt.
///
/// Not on first launch: somebody who has not seen the app yet has no reason to
/// say yes, and on both platforms a refusal is permanent as far as the app is
/// concerned. This is shown once the user has come back.
class PushPrompt extends StatelessWidget {
  const PushPrompt({super.key});

  static const _askedKey = 'cissy.push.asked';
  static const _visitsKey = 'cissy.push.visits';

  /// Whether to put the explanation up now.
  static Future<bool> due() async {
    final prefs = await SharedPreferences.getInstance();
    if (prefs.getBool(_askedKey) == true) {
      return false;
    }
    if (await PushService.isAllowed()) {
      return false;
    }
    final visits = (prefs.getInt(_visitsKey) ?? 0) + 1;
    await prefs.setInt(_visitsKey, visits);
    return visits >= 2;
  }

  static Future<void> _remember() async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setBool(_askedKey, true);
  }

  static Future<void> show(BuildContext context) async {
    await showModalBottomSheet<void>(
      context: context,
      builder: (_) => const PushPrompt(),
    );
    await _remember();
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return SafeArea(
      child: Padding(
        padding: const EdgeInsets.fromLTRB(24, 28, 24, 20),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(
              Icons.notifications_active_rounded,
              size: 40,
              color: theme.colorScheme.primary,
            ),
            const SizedBox(height: 14),
            Text(pushPromptTitle, style: theme.textTheme.titleLarge),
            const SizedBox(height: 8),
            Text(
              pushPromptBody,
              textAlign: TextAlign.center,
              style: theme.textTheme.bodyMedium,
            ),
            const SizedBox(height: 22),
            SizedBox(
              width: double.infinity,
              child: FilledButton(
                onPressed: () async {
                  final navigator = Navigator.of(context);
                  await PushService.requestPermission();
                  navigator.pop();
                },
                child: const Text('Turn on notifications'),
              ),
            ),
            TextButton(
              onPressed: () => Navigator.of(context).pop(),
              child: const Text('Not now'),
            ),
          ],
        ),
      ),
    );
  }
}
"""


SETTINGS_SCREEN = """\
/// Notification settings, as the app's own screen.
///
/// The phone's own permission is the first row rather than a footnote: someone
/// who refused the prompt and later changed their mind will otherwise switch
/// three categories on and conclude the app is broken.
class PushSettingsScreen extends StatefulWidget {
  const PushSettingsScreen({super.key});

  @override
  State<PushSettingsScreen> createState() => PushSettingsScreenState();
}

class PushSettingsScreenState extends State<PushSettingsScreen> {
  Set<String> enabled = <String>{};
  bool allowed = false;
  bool loading = true;

  @override
  void initState() {
    super.initState();
    refresh();
  }

  Future<void> refresh() async {
    final on = await PushService.enabledTopics();
    final permitted = await PushService.isAllowed();
    if (!mounted) {
      return;
    }
    setState(() {
      enabled = on;
      allowed = permitted;
      loading = false;
    });
  }

  @override
  Widget build(BuildContext context) {
    if (loading) {
      return const Center(child: CircularProgressIndicator());
    }
    return ListView(
      children: [
        const SectionLabel('This phone'),
        SwitchListTile(
          title: const Text('Notifications'),
          subtitle: Text(
            allowed
                ? 'Allowed by this phone'
                : 'Turned off in your phone settings',
          ),
          value: allowed,
          onChanged: (want) async {
            if (want) {
              await PushService.requestPermission();
            } else {
              await AppSettings.openNotificationSettings();
            }
            await refresh();
          },
        ),
        if (pushTopics.isNotEmpty) ...[
          const SectionLabel('What you hear about'),
          for (final topic in pushTopics)
            SwitchListTile(
              title: Text(topic['label'] as String),
              value: enabled.contains(topic['id'] as String),
              onChanged: !allowed
                  ? null
                  : (on) async {
                      await PushService.setTopic(topic['id'] as String, on);
                      await refresh();
                    },
            ),
        ],
      ],
    );
  }
}

/// Opening the operating system's own settings page for this app.
class AppSettings {
  static Future<void> openNotificationSettings() async {
    // The app's page in system settings. There is no cross-platform API for
    // this, and the scheme below is what both platforms accept.
    final uri = Platform.isIOS
        ? Uri.parse('app-settings:')
        : Uri.parse('package:$androidPackageId');
    if (await canLaunchUrl(uri)) {
      await launchUrl(uri, mode: LaunchMode.externalApplication);
    }
  }
}
"""


def icon_tool(icon_asset: str) -> str:
    """The build-time script that traces the logo into a status-bar icon.

    Runs on the build machine with `dart run`, like flutter_launcher_icons and
    for the same reason: this server keeps no image library, and the Dart
    toolchain is already there. Generated rather than shipped as a fixed file
    so the logo's asset path is baked in and the script takes no arguments.
    """
    sizes = ", ".join(
        f"'{density}': {size}" for density, size in STAT_ICON_SIZES.items()
    )
    return (
        "// Derives the Android status-bar notification icon from the logo.\n"
        "//\n"
        "// The status bar keeps only the alpha channel of a small icon and\n"
        "// tints the silhouette white or grey, so the full-colour launcher\n"
        "// icon shows up as a featureless square. This traces the logo into\n"
        "// white-on-transparent PNGs instead: its own transparency where it\n"
        "// has any, contrast against the background where it does not.\n"
        "//\n"
        "// Generated by Cissy; run from the project root by the build.\n"
        "import 'dart:io';\n"
        "\n"
        "import 'package:image/image.dart' as img;\n"
        "\n"
        f"const source = {dart_string(icon_asset)};\n"
        f"const name = {dart_string(STAT_ICON)};\n"
        f"const sizes = <String, int>{{{sizes}}};\n" + _ICON_TOOL_BODY
    )


_ICON_TOOL_BODY = """
void main() {
  final decoded = img.decodeImage(File(source).readAsBytesSync());
  if (decoded == null) {
    stderr.writeln('Could not decode $source.');
    exitCode = 1;
    return;
  }
  final icon = decoded.convert(numChannels: 4);
  final total = icon.width * icon.height;

  // A logo exported with a transparent background carries its own shape.
  // "Some" transparency is not enough - a JPEG re-saved as PNG has none, and
  // a stray transparent corner should not make the whole logo the mask.
  var transparent = 0;
  for (final p in icon) {
    if (p.a < 128) transparent++;
  }
  final useAlpha = transparent > total ~/ 50;

  // Otherwise the shape is whatever stands out from the background, and the
  // background is whatever colour the edges of the image are.
  var background = 0.0;
  if (!useAlpha) {
    var sum = 0.0;
    var count = 0;
    for (var x = 0; x < icon.width; x++) {
      for (final y in [0, icon.height - 1]) {
        sum += img.getLuminance(icon.getPixel(x, y));
        count++;
      }
    }
    for (var y = 0; y < icon.height; y++) {
      for (final x in [0, icon.width - 1]) {
        sum += img.getLuminance(icon.getPixel(x, y));
        count++;
      }
    }
    background = sum / count;
  }

  final mask =
      img.Image(width: icon.width, height: icon.height, numChannels: 4);
  for (var y = 0; y < icon.height; y++) {
    for (var x = 0; x < icon.width; x++) {
      final p = icon.getPixel(x, y);
      int a;
      if (useAlpha) {
        a = p.a.toInt();
      } else {
        // A soft threshold rather than a hard one, so anti-aliased edges
        // keep their smoothness instead of turning to staircases.
        final diff = (img.getLuminance(p) - background).abs();
        a = ((diff - 24) * 4).clamp(0, 255).round();
      }
      mask.setPixelRgba(x, y, 255, 255, 255, a);
    }
  }

  // A logo that traced to nothing (one flat colour edge to edge) would be an
  // invisible notification icon, which is worse than a plain shape.
  var visible = 0;
  for (final p in mask) {
    if (p.a > 32) visible++;
  }
  if (visible < total ~/ 100) {
    stdout.writeln('The logo has no shape to trace; using a full square.');
    for (final p in mask) {
      p.a = 255;
    }
  } else {
    stdout.writeln(useAlpha
        ? 'Traced the logo by its transparency.'
        : 'Traced the logo by contrast against its background.');
  }

  for (final entry in sizes.entries) {
    final size = entry.value;
    final scale =
        size / (mask.width > mask.height ? mask.width : mask.height);
    final w = (mask.width * scale).round().clamp(1, size).toInt();
    final h = (mask.height * scale).round().clamp(1, size).toInt();
    final resized = img.copyResize(
      mask,
      width: w,
      height: h,
      interpolation: img.Interpolation.average,
    );
    final canvas = img.Image(width: size, height: size, numChannels: 4);
    img.compositeImage(canvas, resized,
        dstX: (size - w) ~/ 2, dstY: (size - h) ~/ 2);

    final file = File(
        'android/app/src/main/res/drawable-${entry.key}/$name.png');
    file.parent.createSync(recursive: true);
    file.writeAsBytesSync(img.encodePng(canvas));
  }
  stdout.writeln('Status-bar icon written for ${sizes.length} densities.');
}
"""
