"""The native screens a navigation tab can open instead of a page of the site.

Saved items, downloads, settings, and the two small widgets they share. All
constants rather than functions: none of them vary with the configuration, so
the only decision `main_dart` makes about them is whether to emit them at all.
"""

from __future__ import annotations


# ── the native module screens ────────────────────────────────────────────

_SAVED_STORE = """\
/// Bookmarks and recently-viewed pages, stored on the device so they work
/// with no connection. Each entry is {title, url, ts}.
class SavedStore {
  static const _savedKey = 'saved_items';
  static const _recentKey = 'recent_items';
  static const _recentLimit = 20;

  static Future<List<Map<String, dynamic>>> saved() => _read(_savedKey);

  static Future<List<Map<String, dynamic>>> recent() => _read(_recentKey);

  static Future<void> save(String title, String url) async {
    final items = await _read(_savedKey);
    items.removeWhere((item) => item['url'] == url);
    items.insert(0, {
      'title': title,
      'url': url,
      'ts': DateTime.now().toIso8601String(),
    });
    await _write(_savedKey, items);
  }

  static Future<void> removeSaved(String url) async {
    final items = await _read(_savedKey);
    items.removeWhere((item) => item['url'] == url);
    await _write(_savedKey, items);
  }

  static Future<void> recordVisit(String title, String url) async {
    final items = await _read(_recentKey);
    items.removeWhere((item) => item['url'] == url);
    items.insert(0, {
      'title': title,
      'url': url,
      'ts': DateTime.now().toIso8601String(),
    });
    await _write(_recentKey, items.take(_recentLimit).toList());
  }

  static Future<List<Map<String, dynamic>>> _read(String key) async {
    final prefs = await SharedPreferences.getInstance();
    final raw = prefs.getString(key);
    if (raw == null) {
      return [];
    }
    try {
      final decoded = jsonDecode(raw);
      if (decoded is List) {
        return decoded
            .whereType<Map>()
            .map((item) => Map<String, dynamic>.from(item))
            .toList();
      }
    } on FormatException {
      // Corrupt storage reads as empty rather than crashing the screen.
    }
    return [];
  }

  static Future<void> _write(
    String key,
    List<Map<String, dynamic>> items,
  ) async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(key, jsonEncode(items));
  }
}
"""


_SAVED_SCREEN = """\
class SavedScreen extends StatefulWidget {
  const SavedScreen({super.key, required this.onOpen});

  final void Function(String url) onOpen;

  @override
  State<SavedScreen> createState() => _SavedScreenState();
}

class _SavedScreenState extends State<SavedScreen> {
  List<Map<String, dynamic>> saved = [];
  List<Map<String, dynamic>> recent = [];

  @override
  void initState() {
    super.initState();
    refresh();
  }

  Future<void> refresh() async {
    final savedItems = await SavedStore.saved();
    final recentItems = await SavedStore.recent();
    if (mounted) {
      setState(() {
        saved = savedItems;
        recent = recentItems;
      });
    }
  }

  Future<void> _remove(String url) async {
    await SavedStore.removeSaved(url);
    await refresh();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Saved')),
      body: saved.isEmpty && recent.isEmpty
          ? const EmptyNote(
              icon: Icons.bookmark_outline_rounded,
              message: 'Pages you save with the bookmark button appear here.',
            )
          : ListView(
              children: [
                if (saved.isNotEmpty) const SectionLabel('Saved pages'),
                for (final item in saved)
                  ListTile(
                    leading: const Icon(Icons.bookmark_rounded),
                    title: Text(
                      (item['title'] ?? '').toString(),
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                    ),
                    subtitle: Text(
                      (item['url'] ?? '').toString(),
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                    ),
                    trailing: IconButton(
                      tooltip: 'Remove',
                      icon: const Icon(Icons.delete_outline_rounded),
                      onPressed: () => _remove((item['url'] ?? '').toString()),
                    ),
                    onTap: () => widget.onOpen((item['url'] ?? '').toString()),
                  ),
                if (recent.isNotEmpty) const SectionLabel('Recently viewed'),
                for (final item in recent)
                  ListTile(
                    leading: const Icon(Icons.history_rounded),
                    title: Text(
                      (item['title'] ?? '').toString(),
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                    ),
                    subtitle: Text(
                      (item['url'] ?? '').toString(),
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                    ),
                    onTap: () => widget.onOpen((item['url'] ?? '').toString()),
                  ),
              ],
            ),
    );
  }
}
"""


_DOWNLOADS_SCREEN = """\
class DownloadsScreen extends StatefulWidget {
  const DownloadsScreen({super.key});

  @override
  State<DownloadsScreen> createState() => _DownloadsScreenState();
}

class _DownloadsScreenState extends State<DownloadsScreen> {
  List<File> files = [];

  @override
  void initState() {
    super.initState();
    refresh();
  }

  Future<void> refresh() async {
    final dir = await downloadsDirectory();
    final entries = dir.listSync().whereType<File>().toList()
      ..sort(
        (a, b) => b.statSync().modified.compareTo(a.statSync().modified),
      );
    if (mounted) {
      setState(() => files = entries);
    }
  }

  Future<void> _delete(File file) async {
    await file.delete();
    await refresh();
  }

  String _describe(File file) {
    final stat = file.statSync();
    final kb = (stat.size / 1024).clamp(1, double.infinity).round();
    final size = kb >= 1024
        ? '${(kb / 1024).toStringAsFixed(1)} MB'
        : '$kb KB';
    final modified = stat.modified;
    final date = '${modified.year}-'
        '${modified.month.toString().padLeft(2, '0')}-'
        '${modified.day.toString().padLeft(2, '0')}';
    return '$size · $date';
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Downloads')),
      body: files.isEmpty
          ? const EmptyNote(
              icon: Icons.download_rounded,
              message: 'Files you download from the site are kept here, '
                  'available offline.',
            )
          : ListView(
              children: [
                for (final file in files)
                  ListTile(
                    leading: const Icon(Icons.insert_drive_file_outlined),
                    title: Text(
                      file.uri.pathSegments.last,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                    ),
                    subtitle: Text(_describe(file)),
                    trailing: IconButton(
                      tooltip: 'Delete',
                      icon: const Icon(Icons.delete_outline_rounded),
                      onPressed: () => _delete(file),
                    ),
                    onTap: () => OpenFilex.open(file.path),
                  ),
              ],
            ),
    );
  }
}
"""


_SETTINGS_SCREEN = """\
class SettingsScreen extends StatelessWidget {
  const SettingsScreen({super.key, required this.onOpen});

  final void Function(String url) onOpen;

  Future<void> _clearCache(BuildContext context) async {
    final messenger = ScaffoldMessenger.of(context);
    await InAppWebViewController.clearAllCache();
    messenger.showSnackBar(
      const SnackBar(content: Text('Cached pages cleared.')),
    );
  }

  Future<void> _clearCookies(BuildContext context) async {
    final messenger = ScaffoldMessenger.of(context);
    await CookieManager.instance().deleteAllCookies();
    messenger.showSnackBar(
      const SnackBar(content: Text('Browsing data cleared. You may need to '
          'sign in to the site again.')),
    );
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Settings')),
      body: ListView(
        children: [
          ListTile(
            leading: const Icon(Icons.public_rounded),
            title: const Text('Open the website'),
            subtitle: const Text(homeUrl),
            onTap: () => onOpen(homeUrl),
          ),
          ListTile(
            leading: const Icon(Icons.cleaning_services_rounded),
            title: const Text('Clear cached pages'),
            subtitle: const Text('Frees space; pages load fresh next time.'),
            onTap: () => _clearCache(context),
          ),
          ListTile(
            leading: const Icon(Icons.cookie_outlined),
            title: const Text('Clear browsing data'),
            subtitle: const Text('Removes cookies and signs you out.'),
            onTap: () => _clearCookies(context),
          ),
          const ListTile(
            leading: Icon(Icons.info_outline_rounded),
            title: Text('About'),
            subtitle: Text('$appTitle · version $appVersion'),
          ),
        ],
      ),
    );
  }
}
"""


_LIST_HELPERS = """\
class SectionLabel extends StatelessWidget {
  const SectionLabel(this.text, {super.key});

  final String text;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 18, 16, 6),
      child: Text(
        text,
        style: Theme.of(context).textTheme.labelLarge?.copyWith(
              color: Theme.of(context).colorScheme.onSurfaceVariant,
            ),
      ),
    );
  }
}

class EmptyNote extends StatelessWidget {
  const EmptyNote({super.key, required this.icon, required this.message});

  final IconData icon;
  final String message;

  @override
  Widget build(BuildContext context) {
    final colors = Theme.of(context).colorScheme;
    return Center(
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 40),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(icon, size: 44, color: colors.onSurfaceVariant),
            const SizedBox(height: 14),
            Text(
              message,
              textAlign: TextAlign.center,
              style: TextStyle(color: colors.onSurfaceVariant, height: 1.45),
            ),
          ],
        ),
      ),
    );
  }
}
"""
