import unittest

from src.settings_sections import SECTION_KEYS, apply_section, known_section


def full_config():
    return {
        "discord_webhook": "https://discord.example/hook",
        "log_level": "DEBUG",
        "logging": {"max_file_size_mb": 9, "retention_days": 30},
        "max_trash_items": 500,
        "max_trash_percent": 10,
        "clean_bundles_before_empty": True,
        "schedule": {"default_cron": "0 4 * * *"},
        "notify": {"on_emptied": False, "on_error": True},
        "notifications": {"destinations": [{"url": "apprise://x"}]},
        "features": {"mark_watched": True},
        "providers": {"realdebrid": "key"},
        "auth": {"username": "jason", "password_hash": "hash", "api_token_hash": "tok"},
        "mark_watched": {"webhook_secret": "s3cret", "workers": 4},
        "timestamp_repair_workers": [{"name": "w1"}],
        "plex_instances": [{
            "name": "Streamstead-Unlimited",
            "url": "http://plex:32400",
            "token": "plex-token",
            "metadata_health": {"ignored_libraries": ["Youtube"]},
            "timestamp_repair": {"enabled": True, "worker": "local"},
            "libraries": [{
                "name": "TV Shows", "type": "debrid", "paths": [{"path": "/mnt/tv"}],
                "cron": "0 * * * *", "section_id": "7",
                "refresh_enabled": True, "refresh_cron": "0 6 * * *",
                "refresh_guard_minutes": 15,
            }],
        }],
    }


class SectionOwnershipTests(unittest.TestCase):
    def assert_only_changed(self, section, data, changed_paths):
        before = full_config()
        after = apply_section(before, section, data)
        self.assertEqual(before, full_config(), "apply_section must not mutate its input")
        for key in set(before) | set(after):
            if key in changed_paths:
                continue
            self.assertEqual(
                before.get(key), after.get(key),
                f"{section} save must not touch {key}",
            )
        return after

    def test_every_section_in_the_map_is_known(self):
        for section in SECTION_KEYS:
            self.assertTrue(known_section(section))
        self.assertFalse(known_section("not-a-section"))

    def test_unknown_section_is_refused(self):
        with self.assertRaises(ValueError):
            apply_section(full_config(), "nope", {})

    def test_notifications_save_leaves_logging_and_trash_limits_alone(self):
        after = self.assert_only_changed(
            "notifications",
            {"discord_webhook": "https://discord.example/new",
             "notify": {"on_emptied": True},
             "notifications": {"destinations": []}},
            {"discord_webhook", "notify", "notifications"},
        )
        self.assertEqual(after["discord_webhook"], "https://discord.example/new")

    def test_a_section_cannot_write_a_field_it_does_not_own(self):
        # The old whole-config POST is exactly this shape.
        after = apply_section(full_config(), "general", {
            "log_level": "INFO",
            "discord_webhook": "",
            "max_trash_items": 0,
            "features": {},
            "auth": {},
        })
        self.assertEqual(after["log_level"], "INFO")
        self.assertEqual(after["discord_webhook"], "https://discord.example/hook")
        self.assertEqual(after["max_trash_items"], 500)
        self.assertEqual(after["features"], {"mark_watched": True})
        self.assertEqual(after["auth"]["username"], "jason")

    def test_an_omitted_instance_list_never_drops_instances(self):
        # This is the case the server used to salvage with a warning.
        after = apply_section(full_config(), "notifications", {"discord_webhook": "x"})
        self.assertEqual(len(after["plex_instances"]), 1)
        self.assertEqual(after["plex_instances"][0]["token"], "plex-token")

    def test_library_refresh_updates_flags_without_touching_paths(self):
        after = apply_section(full_config(), "library-refresh", {"plex_instances": [{
            "name": "Streamstead-Unlimited",
            "libraries": [{"name": "TV Shows", "refresh_enabled": False}],
        }]})
        library = after["plex_instances"][0]["libraries"][0]
        self.assertFalse(library["refresh_enabled"])
        self.assertEqual(library["paths"], [{"path": "/mnt/tv"}])
        self.assertEqual(library["type"], "debrid")
        self.assertEqual(library["refresh_cron"], "0 6 * * *")

    def test_trash_removal_updates_paths_without_touching_refresh_flags(self):
        after = apply_section(full_config(), "trash-removal", {
            "max_trash_items": 100,
            "plex_instances": [{
                "name": "Streamstead-Unlimited",
                "libraries": [{"name": "TV Shows", "type": "physical",
                               "paths": [{"path": "/mnt/new"}]}],
            }],
        })
        library = after["plex_instances"][0]["libraries"][0]
        self.assertEqual(library["paths"], [{"path": "/mnt/new"}])
        self.assertEqual(library["type"], "physical")
        self.assertTrue(library["refresh_enabled"])
        self.assertEqual(after["max_trash_items"], 100)
        self.assertEqual(after["plex_instances"][0]["token"], "plex-token")

    def test_metadata_health_touches_only_its_own_instance_block(self):
        after = apply_section(full_config(), "metadata-health", {"plex_instances": [{
            "name": "Streamstead-Unlimited",
            "metadata_health": {"ignored_libraries": []},
        }]})
        instance = after["plex_instances"][0]
        self.assertEqual(instance["metadata_health"], {"ignored_libraries": []})
        self.assertEqual(instance["timestamp_repair"], {"enabled": True, "worker": "local"})
        self.assertEqual(instance["libraries"][0]["name"], "TV Shows")

    def test_only_the_plex_section_can_add_or_remove_a_server(self):
        removed = apply_section(full_config(), "metadata-health", {"plex_instances": []})
        self.assertEqual(len(removed["plex_instances"]), 1)
        replaced = apply_section(full_config(), "plex", {"plex_instances": [
            {"name": "Streamstead-Unlimited", "url": "http://plex:32400", "token": "t"},
            {"name": "Second", "url": "http://other:32400", "token": "t2"},
        ]})
        self.assertEqual(
            [item["name"] for item in replaced["plex_instances"]],
            ["Streamstead-Unlimited", "Second"],
        )
        # A surviving server keeps the feature config other sections own.
        self.assertEqual(
            replaced["plex_instances"][0]["metadata_health"],
            {"ignored_libraries": ["Youtube"]},
        )

    def test_a_field_no_section_owns_survives_every_save(self):
        for section in SECTION_KEYS:
            after = apply_section(full_config(), section, {"clean_bundles_before_empty": False})
            self.assertTrue(
                after["clean_bundles_before_empty"],
                f"{section} must not write clean_bundles_before_empty",
            )

    def test_missing_config_file_yields_a_usable_shape(self):
        after = apply_section({}, "features", {"features": {"mark_watched": False}})
        self.assertEqual(after["features"], {"mark_watched": False})
        self.assertEqual(after["plex_instances"], [])


if __name__ == "__main__":
    unittest.main()
