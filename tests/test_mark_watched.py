import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import app
from src.config import AppConfig, MarkWatchedConfig
from src.mark_watched import (
    MarkWatchedManager,
    PlexEpisodePending,
    normalize_sonarr_download,
)


def sonarr_download():
    return {
        "eventType": "Download",
        "series": {"id": 12, "title": "Example Show", "tvdbId": 1234, "year": 2024},
        "episodes": [{"id": 45, "seasonNumber": 2, "episodeNumber": 3, "title": "Done"}],
        "episodeFile": {"id": 99, "path": "/tv/Example Show/S02E03.mkv"},
        "isUpgrade": False,
    }


class SonarrWebhookApiTests(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()
        self.config = AppConfig(
            instances=[], mark_watched=MarkWatchedConfig(webhook_secret="sonarr-secret"),
        )

    def test_webhook_requires_valid_secret(self):
        with patch.object(app, "config", self.config):
            response = self.client.post("/api/webhooks/sonarr", json=sonarr_download())
        self.assertEqual(response.status_code, 401)

    def test_finalized_download_is_queued_outside_request(self):
        manager = Mock()
        manager.enqueue.return_value = ({"id": "job-1", "status": "queued"}, True)
        with patch.object(app, "config", self.config), patch.object(app, "mark_watched", manager):
            response = self.client.post(
                "/api/webhooks/sonarr", json=sonarr_download(),
                headers={"X-Sonarr-Webhook-Secret": "sonarr-secret"},
            )
        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.get_json()["queued"])
        manager.enqueue.assert_called_once()
        manager.process.assert_not_called()

    def test_non_final_event_is_rejected(self):
        payload = sonarr_download()
        payload["eventType"] = "Grab"
        with patch.object(app, "config", self.config):
            response = self.client.post(
                "/api/webhooks/sonarr", json=payload,
                headers={"Authorization": "Bearer sonarr-secret"},
            )
        self.assertEqual(response.status_code, 400)

    def test_download_without_episode_file_is_rejected(self):
        payload = sonarr_download()
        payload.pop("episodeFile")
        with patch.object(app, "config", self.config):
            response = self.client.post(
                "/api/webhooks/sonarr", json=payload,
                headers={"X-Sonarr-Webhook-Secret": "sonarr-secret"},
            )
        self.assertEqual(response.status_code, 400)


class MarkWatchedQueueTests(unittest.TestCase):
    def setUp(self):
        self.runtime = Path("tests/.mark-watched-runtime")
        self.runtime.mkdir(exist_ok=True)
        (self.runtime / "mark-watched-jobs.json").unlink(missing_ok=True)

    def tearDown(self):
        (self.runtime / "mark-watched-jobs.json").unlink(missing_ok=True)
        self.runtime.rmdir()

    def test_duplicate_webhooks_create_one_job(self):
        manager = MarkWatchedManager(str(self.runtime), autostart=False)
        first, created = manager.enqueue(sonarr_download())
        duplicate, created_again = manager.enqueue(sonarr_download())
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first["id"], duplicate["id"])
        self.assertEqual(len(manager.status()["jobs"]), 1)

    def test_pending_match_retries_with_bounded_backoff(self):
        attempts = []

        def process(_event):
            attempts.append(True)
            if len(attempts) < 3:
                raise PlexEpisodePending("not scanned")
            return {"message": "Episode marked watched"}

        sleeps = []
        manager = MarkWatchedManager(
            str(self.runtime), processor=process, retry_delays=(1, 2, 3),
            autostart=False, sleep=sleeps.append,
        )
        record, _ = manager.enqueue(sonarr_download())
        result = manager.process(record["id"])
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["attempts"], 3)
        self.assertEqual(sleeps, [1, 2])

    def test_normalizer_accepts_sonarr_test_without_queueing(self):
        self.assertIsNone(normalize_sonarr_download({"eventType": "Test"}))


if __name__ == "__main__":
    unittest.main()
