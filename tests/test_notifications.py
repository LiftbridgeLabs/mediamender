import unittest
from unittest.mock import Mock, patch

import app
from src import notifications
from src.config import (
    AppConfig,
    NotificationDestination,
    parse_config,
)


class NotificationConfigTests(unittest.TestCase):
    def test_apprise_destinations_are_parsed_with_routing(self):
        parsed = parse_config({
            "notifications": {
                "destinations": [{
                    "name": "Telegram alerts",
                    "service": "telegram",
                    "url": "tgram://token/chat",
                    "enabled": True,
                    "events": ["health_fail", "error", "unknown"],
                }],
            },
        })

        self.assertEqual(len(parsed.notification_destinations), 1)
        destination = parsed.notification_destinations[0]
        self.assertEqual(destination.name, "Telegram alerts")
        self.assertEqual(destination.service, "telegram")
        self.assertEqual(destination.events, ["health_fail", "error"])

    def test_validation_rejects_duplicate_names(self):
        raw = {
            "notifications": {
                "destinations": [
                    {
                        "name": "Alerts",
                        "service": "ntfy",
                        "url": "ntfy://ntfy.sh/one",
                        "events": ["error"],
                    },
                    {
                        "name": "alerts",
                        "service": "gotify",
                        "url": "gotify://host/token",
                        "events": ["health_fail"],
                    },
                ],
            },
        }

        with self.assertRaisesRegex(ValueError, "duplicated"):
            app._validate_notifications(raw)

    def test_validation_requires_at_least_one_routed_event(self):
        raw = {
            "notifications": {
                "destinations": [{
                    "name": "No events",
                    "service": "custom",
                    "url": "json://host/path",
                    "events": [],
                }],
            },
        }

        with self.assertRaisesRegex(ValueError, "at least one event"):
            app._validate_notifications(raw)


class NotificationDeliveryTests(unittest.TestCase):
    def test_discord_delivery_logs_http_status_without_webhook(self):
        webhook = "https://discord.com/api/webhooks/123/super-secret"
        response = Mock(status_code=204)
        with patch("src.notifications.requests.post", return_value=response), \
             self.assertLogs("mediamender.notifications", level="INFO") as captured:
            notifications._post(webhook, {"content": "test"})

        output = "\n".join(captured.output)
        self.assertIn("HTTP 204", output)
        self.assertNotIn("super-secret", output)
        self.assertNotIn(webhook, output)

    def test_discord_failure_logs_only_sanitized_exception_type(self):
        webhook = "https://discord.com/api/webhooks/123/super-secret"
        failure = RuntimeError(f"request failed for {webhook}")
        with patch("src.notifications.requests.post", side_effect=failure), \
             self.assertLogs("mediamender.notifications", level="WARNING") as captured:
            notifications._post(webhook, {"content": "test"})

        output = "\n".join(captured.output)
        self.assertIn("RuntimeError", output)
        self.assertNotIn("super-secret", output)
        self.assertNotIn(webhook, output)

    def test_fanout_only_starts_enabled_matching_destinations(self):
        config = AppConfig(
            instances=[],
            notification_destinations=[
                NotificationDestination(
                    name="Errors",
                    url="ntfy://ntfy.sh/errors",
                    service="ntfy",
                    events=["error"],
                ),
                NotificationDestination(
                    name="Success",
                    url="ntfy://ntfy.sh/success",
                    service="ntfy",
                    events=["emptied"],
                ),
                NotificationDestination(
                    name="Disabled",
                    url="ntfy://ntfy.sh/disabled",
                    service="ntfy",
                    enabled=False,
                    events=["error"],
                ),
            ],
        )

        with patch("src.notifications.threading.Thread") as thread:
            notifications._apprise_fanout(
                config, "error", "Failure", "Something failed", "failure",
            )

        self.assertEqual(thread.call_count, 1)
        self.assertEqual(thread.call_args.kwargs["name"], "notify-Errors")
        thread.return_value.start.assert_called_once_with()

    def test_dispatch_preserves_native_discord_and_apprise(self):
        config = AppConfig(
            instances=[],
            discord_webhook="https://discord.com/api/webhooks/1/token",
        )
        checks = {"Mount": {"pass": True, "detail": "available"}}

        with patch("src.notifications._native_async") as native, \
             patch("src.notifications._apprise_fanout") as apprise_fanout:
            notifications.dispatch_clean(config, "Plex", "Movies", checks)

        native.assert_called_once_with(
            notifications.notify_clean,
            config.discord_webhook, "Plex", "Movies", checks,
        )
        apprise_fanout.assert_called_once()
        self.assertEqual(apprise_fanout.call_args.args[1], "clean")


if __name__ == "__main__":
    unittest.main()
