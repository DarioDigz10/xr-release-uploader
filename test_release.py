import io
import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

import release


def make_config(builds: Path) -> dict:
    return {"apps": {"myapp": {
        "name": "My XR App",
        "builds_dir": str(builds),
        "version_code": "{major}{minor:02}{patch:02}",
        "devices": {
            "pico": {
                "folder": "Pico",
                "apk": "MyApp_Pico_{version}.apk",
                "package": "com.example.myapp",
                "arborxr": {"app_id": "abx-pico", "channel_id": ""},
                "manual": {"name": "Company Dashboard", "url": "https://dashboard.example"},
            },
            "quest": {
                "folder": "Quest",
                "apk": "MyApp_Quest_{version}.apk",
                "package": "com.example.myapp.quest",
                "arborxr": {"app_id": "abx-quest", "channel_id": "chan-1"},
                "managexr": {"channel": "Dev"},
                "meta": {"folder": "Quest/Meta", "app_id": "123", "secret_env": "META_SECRET_TEST",
                         "channel": "ALPHA", "age_group": "TEENS_AND_ADULTS"},
            },
        },
    }}}


class ReleaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.builds = self.root / "Builds"
        for name in ("Pico/MyApp_Pico_1.9.0.apk",
                     "Pico/MyApp_Pico_1.10.0.apk",
                     "Pico/OtherApp_Pico_2.0.0.apk",
                     "Quest/MyApp_Quest_1.9.0.apk",
                     "Quest/Meta/MyApp_Quest_1.9.0.apk",
                     "Quest/Meta/MyApp_Quest_1.9.0.main.obb",
                     "Quest/Meta/MyApp_Quest_1.11.0.apk"):
            path = self.builds / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x" * 64)
        self.config = make_config(self.builds)
        self.app = self.config["apps"]["myapp"]
        patches = [
            mock.patch.object(release, "find_tool", lambda destination: f"/bin/{destination}"),
            mock.patch.object(release, "NOTES_DIR", self.root / "notes"),
            mock.patch.dict(os.environ, {"ABXR_API_TOKEN": "t", "MXR_API_KEY_ID": "i",
                                         "MXR_API_KEY_SECRET": "s", "META_SECRET_TEST": "very-secret"}),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def steps(self, devices=None, destinations=release.DESTINATIONS):
        steps, problems = release.plan(self.config, "myapp", "1.9.0", devices, destinations, "Fixes")
        self.assertEqual(problems, [])
        return {step.label: step for step in steps}

    def test_example_config_parses(self):
        with open(release.ROOT / "config.example.toml", "rb") as file:
            app = tomllib.load(file)["apps"]["myapp"]
        self.assertEqual(release.apk_regex(app["devices"]["quest"]).fullmatch("MyApp_Quest_1.2.3.apk")[1], "1.2.3")

    def test_version_code_uses_the_app_format(self):
        self.assertEqual(release.version_code(self.app, "1.9.2"), "10902")
        self.assertEqual(release.version_code({"version_code": "{major}{minor}{patch:03}"}, "2.1.5"), "21005")
        self.assertIsNone(release.version_code({}, "1.0.0"))

    def test_versions_come_from_every_folder_and_ignore_other_apps(self):
        self.assertEqual(release.find_versions(self.app), ["1.11.0", "1.10.0", "1.9.0"])

    def test_a_destination_folder_overrides_the_device_folder(self):
        steps = self.steps(["quest"])
        self.assertIsNone(steps["Quest -> ArborXR"].build.obb)
        self.assertEqual(steps["Quest -> Meta Horizon"].build.obb,
                         self.builds / "Quest/Meta/MyApp_Quest_1.9.0.main.obb")

    def test_build_summary_lists_each_folder(self):
        self.assertEqual(release.build_summary(self.app, "quest", "1.9.0"),
                         "Quest: APK 0 MB · Quest/Meta: APK 0 MB + OBB 0 MB")

    def test_plan_only_uses_destinations_configured_for_each_device(self):
        self.assertEqual(list(self.steps()), [
            "Pico -> ArborXR", "Pico -> Company Dashboard",
            "Quest -> ArborXR", "Quest -> ManageXR", "Quest -> Meta Horizon",
        ])

    def test_plan_reports_empty_notes_missing_build_and_missing_secret(self):
        _, problems = release.plan(self.config, "myapp", "1.10.0", notes=" ")
        self.assertIn("Release notes are empty.", problems)
        self.assertEqual(sum("not found" in p for p in problems), 2)  # Quest and Quest/Meta, each once
        del os.environ["META_SECRET_TEST"]
        _, problems = release.plan(self.config, "myapp", "1.9.0", ["quest"], ["meta"], "Fixes")
        self.assertEqual(problems, ["Quest -> Meta Horizon: set META_SECRET_TEST in .env."])

    def test_meta_rejects_a_big_apk_without_obb(self):
        (self.builds / "Quest/Meta/MyApp_Quest_1.9.0.main.obb").unlink()
        with mock.patch.object(release, "META_MAX_APK_BYTES", 10):
            _, problems = release.plan(self.config, "myapp", "1.9.0", ["quest"], ["meta"], "Fixes")
        self.assertEqual(len(problems), 1)
        self.assertIn("Split Application Binary", problems[0])

    def test_arborxr_split_build_needs_a_version_code(self):
        self.app["devices"]["quest"]["arborxr"]["folder"] = "Quest/Meta"
        del self.app["version_code"]
        _, problems = release.plan(self.config, "myapp", "1.9.0", ["quest"], ["arborxr"], "Fixes")
        self.assertIn("version_code", problems[0])

    def test_commands(self):
        commands = {label: release.command(step, "Fixes") for label, step in self.steps().items()}
        meta_obb = str(self.builds / "Quest/Meta/MyApp_Quest_1.9.0.main.obb")
        self.assertEqual(commands["Pico -> ArborXR"][1:4], ["apps", "upload", "abx-pico"])
        self.assertNotIn("--release-channel-id", commands["Pico -> ArborXR"])
        self.assertEqual(commands["Quest -> ArborXR"][-2:], ["--release-channel-id", "chan-1"])
        self.assertEqual(commands["Quest -> ManageXR"][-2:], ["--release-channel", "Dev"])
        meta = commands["Quest -> Meta Horizon"]
        self.assertEqual(meta[meta.index("--obb") + 1], meta_obb)
        self.assertEqual(meta[meta.index("--notes") + 1], "Fixes")
        self.assertIsNone(commands["Pico -> Company Dashboard"])

    def test_preview_hides_the_secret_and_shortens_the_notes(self):
        text = release.preview(self.steps(["quest"], ["meta"])["Quest -> Meta Horizon"], "Long\nnotes")
        self.assertNotIn("very-secret", text)
        self.assertIn("--app-secret '***'", text)
        self.assertIn("--notes '<release notes>'", text)

    def test_arborxr_split_build_uploads_the_obb_under_its_device_name(self):
        self.app["devices"]["quest"]["arborxr"]["folder"] = "Quest/Meta"
        step = self.steps(["quest"], ["arborxr"])["Quest -> ArborXR"]
        self.assertEqual(release.command(step, "Fixes")[1:3], ["app_bundles", "upload"])
        folder = release.prepare_bundle_folder(step)
        self.assertEqual([p.name for p in folder.iterdir()], ["main.10900.com.example.myapp.quest.obb"])

    def test_notes_round_trip(self):
        release.save_notes("myapp", "1.9.0", "  - Fixes\n\n")
        self.assertEqual(release.load_notes("myapp", "1.9.0"), "- Fixes\n")
        self.assertEqual(release.load_notes("myapp", "9.9.9"), "")

    def test_read_lines_turns_carriage_returns_into_redraws(self):
        lines = list(release.read_lines(io.BytesIO("10%\r50%\r100%\r\ndone ✓\nend".encode())))
        self.assertEqual(lines, [("10%", False), ("50%", True), ("100%", True), ("done ✓", False), ("end", False)])


if __name__ == "__main__":
    unittest.main()
