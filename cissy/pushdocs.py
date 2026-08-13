"""The customer's own integration guide, with their values already in it.

Web2App knows the Firebase project id, the package name, the topic names and
the base URL, so a customer should never have to read a generic page and
substitute ten placeholders by hand. That substitution is where people give up,
and a customer who gives up here never sends a notification at all - which
makes the whole direct-sending architecture theoretical.

So this renders working code for the stack they actually use. Nothing here
reaches the network or touches Firebase; it is string templating over a config
plus the project id read from the uploaded `google-services.json`.

One rule runs through all of it: the service-account key belongs on the
customer's server. It is never requested, never stored here, and every snippet
says so.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import AppConfig
from .errors import NotFoundError

# The FCM HTTP v1 endpoint every snippet ultimately talks to, directly or
# through an SDK.
ENDPOINT = "https://fcm.googleapis.com/v1/projects/{project}/messages:send"
SCOPE = "https://www.googleapis.com/auth/firebase.messaging"


@dataclass(frozen=True)
class Stack:
    id: str
    label: str
    # What the customer runs to get the library, or "" where there is none.
    install: str
    language: str


STACKS = (
    Stack("node", "Node.js", "npm install firebase-admin", "javascript"),
    Stack("php", "PHP", "composer require kreait/firebase-php", "php"),
    Stack("laravel", "Laravel", "composer require kreait/laravel-firebase", "php"),
    Stack("python", "Python", "pip install firebase-admin", "python"),
    Stack("dotnet", ".NET", "dotnet add package FirebaseAdmin", "csharp"),
    Stack("wordpress", "WordPress", "composer require kreait/firebase-php", "php"),
    Stack("rest", "Other / REST", "", "bash"),
)

_BY_ID = {stack.id: stack for stack in STACKS}


@dataclass(frozen=True)
class Guide:
    stack: str
    label: str
    language: str
    install: str
    code: str
    project_id: str
    topic: str
    notes: tuple[str, ...]

    def to_json(self) -> dict[str, object]:
        return {
            "stack": self.stack,
            "label": self.label,
            "language": self.language,
            "install": self.install,
            "code": self.code,
            "project_id": self.project_id,
            "topic": self.topic,
            "notes": list(self.notes),
        }


def stacks() -> list[dict[str, str]]:
    return [{"id": s.id, "label": s.label} for s in STACKS]


def render(config: AppConfig, project_id: str, stack_id: str) -> Guide:
    """Build the guide for one backend stack."""
    stack = _BY_ID.get(stack_id)
    if stack is None:
        raise NotFoundError(
            f'"{stack_id}" is not a backend this guide covers. '
            f"Pick one of: {', '.join(s.id for s in STACKS)}."
        )

    topic = _first_topic(config)
    example = _example_path(config)
    builder = _BUILDERS[stack.id]
    return Guide(
        stack=stack.id,
        label=stack.label,
        language=stack.language,
        install=stack.install,
        code=builder(config, project_id, topic, example),
        project_id=project_id,
        topic=topic,
        notes=_notes(config, project_id, topic),
    )


def _first_topic(config: AppConfig) -> str:
    """The category to show in the example.

    A default-on one if there is one, because that is the category every fresh
    install is already subscribed to and therefore the one a first test message
    will actually reach.
    """
    for topic in config.push_topics:
        if topic.get("default"):
            return str(topic.get("id", ""))
    if config.push_topics:
        return str(config.push_topics[0].get("id", ""))
    return "general"


def _example_path(config: AppConfig) -> str:
    return "/orders/123"


def _notes(config: AppConfig, project_id: str, topic: str) -> tuple[str, ...]:
    notes = [
        "Your Firebase service-account key belongs on your server only. Never "
        "put it in browser JavaScript, and never inside the app.",
        f'Sending to the topic "{topic}" reaches every install subscribed to '
        f"that category. Firebase does the fan-out - you never handle a list "
        f"of devices.",
        'The "url" in data can be a path. The app resolves it against '
        f"{config.website_url} and refuses anything outside your allowed "
        f"domains.",
    ]
    if not config.push_topics:
        notes.append(
            'No categories are configured yet, so this example uses "general". '
            "Add categories on the Notifications page and this guide updates."
        )
    return tuple(notes)


# ── per-stack snippets ───────────────────────────────────────────────────
#
# Each one is complete enough to paste into a project and run. Credentials are
# read from the environment rather than written inline, which is both the
# supported way and the way that does not end up in a git history.


def _node(config: AppConfig, project: str, topic: str, path: str) -> str:
    return f"""\
// GOOGLE_APPLICATION_CREDENTIALS points at your service-account JSON.
const admin = require("firebase-admin");

admin.initializeApp({{
  credential: admin.credential.applicationDefault(),
  projectId: "{project}",
}});

async function notify() {{
  await admin.messaging().send({{
    topic: "{topic}",
    notification: {{
      title: "Order ready",
      body: "Order #123 is ready for pickup.",
    }},
    data: {{
      action: "open_url",
      url: "{path}",
    }},
  }});
}}

notify().catch(console.error);
"""


def _php(config: AppConfig, project: str, topic: str, path: str) -> str:
    return f"""\
<?php
// GOOGLE_APPLICATION_CREDENTIALS points at your service-account JSON.
require __DIR__ . '/vendor/autoload.php';

use Kreait\\Firebase\\Factory;
use Kreait\\Firebase\\Messaging\\CloudMessage;

$messaging = (new Factory())
    ->withProjectId('{project}')
    ->createMessaging();

$messaging->send(CloudMessage::new()
    ->withChangedTarget('topic', '{topic}')
    ->withNotification([
        'title' => 'Order ready',
        'body'  => 'Order #123 is ready for pickup.',
    ])
    ->withData([
        'action' => 'open_url',
        'url'    => '{path}',
    ]));
"""


def _laravel(config: AppConfig, project: str, topic: str, path: str) -> str:
    return f"""\
<?php
// config/firebase.php holds the credentials path; FIREBASE_PROJECT_ID={project}
use Kreait\\Firebase\\Contract\\Messaging;
use Kreait\\Firebase\\Messaging\\CloudMessage;

class OrderReady
{{
    public function __construct(private Messaging $messaging) {{}}

    public function handle(): void
    {{
        $this->messaging->send(CloudMessage::new()
            ->withChangedTarget('topic', '{topic}')
            ->withNotification([
                'title' => 'Order ready',
                'body'  => 'Order #123 is ready for pickup.',
            ])
            ->withData([
                'action' => 'open_url',
                'url'    => '{path}',
            ]));
    }}
}}
"""


def _python(config: AppConfig, project: str, topic: str, path: str) -> str:
    return f"""\
# GOOGLE_APPLICATION_CREDENTIALS points at your service-account JSON.
import firebase_admin
from firebase_admin import messaging

firebase_admin.initialize_app(options={{"projectId": "{project}"}})

messaging.send(
    messaging.Message(
        topic="{topic}",
        notification=messaging.Notification(
            title="Order ready",
            body="Order #123 is ready for pickup.",
        ),
        data={{
            "action": "open_url",
            "url": "{path}",
        }},
    )
)
"""


def _dotnet(config: AppConfig, project: str, topic: str, path: str) -> str:
    return f"""\
// GOOGLE_APPLICATION_CREDENTIALS points at your service-account JSON.
using FirebaseAdmin;
using FirebaseAdmin.Messaging;
using Google.Apis.Auth.OAuth2;

FirebaseApp.Create(new AppOptions
{{
    Credential = GoogleCredential.GetApplicationDefault(),
    ProjectId = "{project}",
}});

await FirebaseMessaging.DefaultInstance.SendAsync(new Message
{{
    Topic = "{topic}",
    Notification = new Notification
    {{
        Title = "Order ready",
        Body = "Order #123 is ready for pickup.",
    }},
    Data = new Dictionary<string, string>
    {{
        ["action"] = "open_url",
        ["url"] = "{path}",
    }},
}});
"""


def _wordpress(config: AppConfig, project: str, topic: str, path: str) -> str:
    return f"""\
<?php
// In your theme's functions.php or a small plugin. Fires when a post is
// published; change the hook for whatever event matters to you.
add_action('publish_post', function ($post_id, $post) {{
    require_once __DIR__ . '/vendor/autoload.php';

    $messaging = (new Kreait\\Firebase\\Factory())
        ->withProjectId('{project}')
        ->createMessaging();

    $messaging->send(Kreait\\Firebase\\Messaging\\CloudMessage::new()
        ->withChangedTarget('topic', '{topic}')
        ->withNotification([
            'title' => get_the_title($post_id),
            'body'  => wp_trim_words($post->post_content, 20),
        ])
        ->withData([
            'action' => 'open_url',
            'url'    => wp_make_link_relative(get_permalink($post_id)),
        ]));
}}, 10, 2);
"""


def _rest(config: AppConfig, project: str, topic: str, path: str) -> str:
    endpoint = ENDPOINT.format(project=project)
    return f"""\
# An OAuth 2.0 access token minted on your server from the service-account
# key, with the scope below. It is short-lived - mint one per batch, do not
# store it, and never send it to a browser.
#
#   scope: {SCOPE}

curl -X POST \\
  -H "Authorization: Bearer $ACCESS_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{{
    "message": {{
      "topic": "{topic}",
      "notification": {{
        "title": "Order ready",
        "body": "Order #123 is ready for pickup."
      }},
      "data": {{
        "action": "open_url",
        "url": "{path}"
      }}
    }}
  }}' \\
  {endpoint}
"""


_BUILDERS = {
    "node": _node,
    "php": _php,
    "laravel": _laravel,
    "python": _python,
    "dotnet": _dotnet,
    "wordpress": _wordpress,
    "rest": _rest,
}
