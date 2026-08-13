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


def has_inbox(config: AppConfig) -> bool:
    """Whether the app keeps a local list of what it has received."""
    return any(
        tab.get("target") == "native:notifications" for tab in config.nav_tabs
    )


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
  await PushInbox.record(message);
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


def inbox(config: AppConfig) -> str:
    """The local record of what has arrived.

    Written from two isolates - the background handler and the app - so it is
    read back from disk every time rather than cached in memory.
    """
    return """\
/// What this device has received, kept on the device.
///
/// A convenience history, not a message store: it holds what this install
/// happened to be sent, it is capped, and clearing the app's data clears it.
/// Anything that has to survive belongs on the customer's own server.
class PushInbox {
  static const _key = 'cissy.push.inbox';
  static const _limit = 50;

  static Future<void> record(RemoteMessage message) async {
    final notification = message.notification;
    if (notification == null) {
      return;
    }
    final prefs = await SharedPreferences.getInstance();
    final entries = prefs.getStringList(_key) ?? <String>[];
    entries.insert(
      0,
      jsonEncode({
        'title': notification.title ?? '',
        'body': notification.body ?? '',
        'url': PushRouter.destination(message.data) ?? '',
        'ts': DateTime.now().millisecondsSinceEpoch,
      }),
    );
    if (entries.length > _limit) {
      entries.removeRange(_limit, entries.length);
    }
    await prefs.setStringList(_key, entries);
  }

  static Future<List<Map<String, dynamic>>> entries() async {
    final prefs = await SharedPreferences.getInstance();
    final stored = prefs.getStringList(_key) ?? <String>[];
    final out = <Map<String, dynamic>>[];
    for (final entry in stored) {
      try {
        final decoded = jsonDecode(entry);
        if (decoded is Map<String, dynamic>) {
          out.add(decoded);
        }
      } on FormatException {
        // One unreadable entry must not empty the whole list.
      }
    }
    return out;
  }

  static Future<void> clear() async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.remove(_key);
  }
}
"""


def service(config: AppConfig) -> str:
    """Registration, permission, categories, and what to do with a message."""
    foreground = config.push_foreground

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
        android: AndroidInitializationSettings('@mipmap/ic_launcher'),
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
    await PushInbox.record(message);
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


INBOX_SCREEN = """\
/// What this device has been sent.
class PushInboxScreen extends StatefulWidget {
  const PushInboxScreen({super.key, required this.onOpen});

  final void Function(String url) onOpen;

  @override
  State<PushInboxScreen> createState() => PushInboxScreenState();
}

class PushInboxScreenState extends State<PushInboxScreen> {
  List<Map<String, dynamic>> entries = const [];
  bool loading = true;

  @override
  void initState() {
    super.initState();
    refresh();
  }

  void _openSettings(BuildContext context) {
    Navigator.of(context).push(
      MaterialPageRoute<void>(
        builder: (_) => Scaffold(
          appBar: AppBar(title: const Text('Notifications')),
          body: const PushSettingsScreen(),
        ),
      ),
    );
  }

  Future<void> refresh() async {
    final stored = await PushInbox.entries();
    if (!mounted) {
      return;
    }
    setState(() {
      entries = stored;
      loading = false;
    });
  }

  @override
  Widget build(BuildContext context) {
    if (loading) {
      return const Center(child: CircularProgressIndicator());
    }
    if (entries.isEmpty) {
      return Column(
        children: [
          ListTile(
            leading: const Icon(Icons.tune_rounded),
            title: const Text('Notification settings'),
            trailing: const Icon(Icons.chevron_right_rounded),
            onTap: () => _openSettings(context),
          ),
          const Divider(height: 1),
          const Expanded(
            child: EmptyNote(
              icon: Icons.notifications_none_rounded,
              message: 'Notifications you receive will be listed here.',
            ),
          ),
        ],
      );
    }
    return Column(
      children: [
        ListTile(
          leading: const Icon(Icons.tune_rounded),
          title: const Text('Notification settings'),
          trailing: const Icon(Icons.chevron_right_rounded),
          onTap: () => _openSettings(context),
        ),
        const Divider(height: 1),
        Expanded(
          child: ListView.separated(
            itemCount: entries.length,
            separatorBuilder: (_, __) => const Divider(height: 1),
            itemBuilder: (context, position) {
              final entry = entries[position];
              final url = (entry['url'] ?? '').toString();
              return ListTile(
                title: Text((entry['title'] ?? '').toString()),
                subtitle: Text((entry['body'] ?? '').toString()),
                trailing: url.isEmpty
                    ? null
                    : const Icon(Icons.chevron_right_rounded),
                onTap: url.isEmpty ? null : () => widget.onOpen(url),
              );
            },
          ),
        ),
        SafeArea(
          top: false,
          child: TextButton.icon(
            onPressed: () async {
              await PushInbox.clear();
              await refresh();
            },
            icon: const Icon(Icons.delete_sweep_rounded),
            label: const Text('Clear'),
          ),
        ),
      ],
    );
  }
}
"""
