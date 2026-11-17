"""Upload Android XR builds to ArborXR, ManageXR and Meta Horizon, plus guided manual uploads.

The window (gui.pyw) is the usual way in. This module also runs on its own:

    python release.py myapp 1.9.2 --dry-run
    python release.py myapp 1.9.2 --device quest --only meta
"""

from __future__ import annotations

import argparse
import codecs
import os
import re
import shlex
import shutil
import subprocess
import sys
import sysconfig
import tomllib
import webbrowser
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NOTES_DIR = ROOT / "notes"
DESTINATIONS = ("arborxr", "managexr", "meta", "manual")
DESTINATION_NAMES = {
    "arborxr": "ArborXR",
    "managexr": "ManageXR",
    "meta": "Meta Horizon",
    "manual": "Manual upload",
}
GIB = 1024 ** 3
META_MAX_APK_BYTES = GIB  # Meta Horizon rejects a bigger APK, so big Quest builds ship as APK + OBB.
MAX_OBB_BYTES = 4 * GIB  # FAT32 limit on the headset; Meta and ArborXR enforce it.
MANAGEXR_CLI = "@managexr/mxr-cli@1.3.1"
ARBORXR_WAIT_SECONDS = "1800"  # abxr-cli gives up waiting for server processing after 60 s by default.

REQUIRED_SETTINGS = {
    "arborxr": ("app_id",),
    "managexr": (),
    "meta": ("app_id", "secret_env", "channel", "age_group"),
    "manual": ("name", "url"),
}
REQUIRED_ENV = {
    "arborxr": ("ABXR_API_TOKEN",),
    "managexr": ("MXR_API_KEY_ID", "MXR_API_KEY_SECRET"),
}
TOOL_HINTS = {
    "arborxr": "abxr-cli (pip install -r requirements.txt)",
    "managexr": "npx (install Node.js)",
    "meta": "ovr-platform-util (set OVR_PLATFORM_UTIL in .env)",
}


@dataclass
class Build:
    apk: Path
    obb: Path | None


@dataclass
class Step:
    device: str
    destination: str
    settings: dict
    build: Build
    package: str
    version: str
    version_code: str | None

    @property
    def label(self) -> str:
        return step_label(self.device, self.destination, self.settings)


def step_label(device: str, destination: str, settings: dict) -> str:
    name = settings.get("name") if destination == "manual" else None
    return f"{device.capitalize()} -> {name or DESTINATION_NAMES[destination]}"


# --- Configuration ---------------------------------------------------------------------------


def load_env(path: Path = ROOT / ".env") -> None:
    """Load KEY=VALUE lines into the environment; variables already set win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and not key.strip().startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip('"'))


def load_config(path: Path = ROOT / "config.toml") -> dict:
    if not path.is_file():
        raise FileNotFoundError(f"{path} not found. Copy config.example.toml to config.toml and fill it in.")
    with open(path, "rb") as file:
        return tomllib.load(file)


def find_tool(destination: str) -> str | None:
    if destination == "meta":
        return os.environ.get("OVR_PLATFORM_UTIL") or shutil.which("ovr-platform-util")
    name = {"arborxr": "abxr-cli", "managexr": "npx"}[destination]
    # pythonw started from Explorer may not have the Scripts folder of its own Python on PATH.
    scripts = os.pathsep.join([sysconfig.get_path("scripts"), sysconfig.get_path("scripts", f"{os.name}_user")])
    return shutil.which(name) or shutil.which(name, path=scripts)


# --- Builds and notes ------------------------------------------------------------------------


def builds_dir(app: dict) -> Path:
    return (ROOT / app["builds_dir"]).resolve()


def build_folder(app: dict, device: str, settings: dict | None = None) -> Path:
    """A destination's own `folder` wins over its device's, e.g. a split build only for Meta."""
    folder = (settings or {}).get("folder") or app["devices"][device]["folder"]
    return builds_dir(app) / folder


def folders(app: dict, device: str) -> list[tuple[dict | None, Path]]:
    """Every distinct build folder of a device, with the settings that point to it."""
    device_config = app["devices"][device]
    found = {}
    for settings in [None, *(device_config[d] for d in DESTINATIONS if d in device_config)]:
        found.setdefault(build_folder(app, device, settings), settings)
    return [(settings, folder) for folder, settings in found.items()]


def apk_regex(device_config: dict) -> re.Pattern:
    before, sep, after = device_config["apk"].partition("{version}")
    if not sep:
        raise ValueError(f"apk pattern {device_config['apk']!r} must contain {{version}}.")
    return re.compile(rf"{re.escape(before)}(\d+\.\d+\.\d+){re.escape(after)}")


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def version_code(app: dict, version: str) -> str | None:
    """Android versionCode from the app's `version_code` format, e.g. "{major}{minor:02}{patch:02}"."""
    if "version_code" not in app:
        return None
    major, minor, patch = version_key(version)
    return app["version_code"].format(major=major, minor=minor, patch=patch)


def find_versions(app: dict) -> list[str]:
    """Versions with at least one APK in any configured build folder, newest first."""
    versions = set()
    for device, device_config in app["devices"].items():
        pattern = apk_regex(device_config)
        for _, folder in folders(app, device):
            if folder.is_dir():
                versions |= {match[1] for file in folder.iterdir() if (match := pattern.fullmatch(file.name))}
    return sorted(versions, key=version_key, reverse=True)


def apk_path(app: dict, device: str, version: str, settings: dict | None = None) -> Path:
    return build_folder(app, device, settings) / app["devices"][device]["apk"].replace("{version}", version)


def find_build(app: dict, device: str, version: str, settings: dict | None = None) -> Build | None:
    apk = apk_path(app, device, version, settings)
    if not apk.is_file():
        return None
    obb = apk.with_suffix(".main.obb")  # Unity names the expansion file after the APK.
    return Build(apk, obb if obb.is_file() else None)


def format_size(size: int) -> str:
    return f"{size / GIB:.1f} GB" if size >= GIB / 10 else f"{size / 1024 ** 2:.0f} MB"


def describe_build(build: Build) -> str:
    text = f"APK {format_size(build.apk.stat().st_size)}"
    return text + (f" + OBB {format_size(build.obb.stat().st_size)}" if build.obb else "")


def build_summary(app: dict, device: str, version: str) -> str:
    """One entry per build folder, e.g. "Quest: APK 1.7 GB · Quest/Meta: APK 56 MB + OBB 1.6 GB"."""
    parts = []
    for settings, folder in folders(app, device):
        build = find_build(app, device, version, settings)
        parts.append(f"{folder.relative_to(builds_dir(app)).as_posix()}: "
                     f"{describe_build(build) if build else 'not found'}")
    return " · ".join(parts)


def notes_path(app_key: str, version: str) -> Path:
    return NOTES_DIR / app_key / f"{version}.md"


def load_notes(app_key: str, version: str) -> str:
    path = notes_path(app_key, version)
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def save_notes(app_key: str, version: str, text: str) -> None:
    if text.strip():
        path = notes_path(app_key, version)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text.strip() + "\n", encoding="utf-8")


# --- Planning --------------------------------------------------------------------------------


def plan(config: dict, app_key: str, version: str, devices=None, destinations=DESTINATIONS,
         notes: str = "") -> tuple[list[Step], list[str]]:
    """Return the uploads to run and every problem that blocks them."""
    app = config["apps"][app_key]
    steps, problems, missing = [], [], set()
    if not notes.strip():
        problems.append("Release notes are empty.")
    for device in devices or app["devices"]:
        device_config = app["devices"].get(device, {})
        for destination in destinations:
            if destination not in device_config:
                continue
            settings = device_config[destination]
            build = find_build(app, device, version, settings)
            if build is None:
                path = apk_path(app, device, version, settings)
                if path not in missing:
                    missing.add(path)
                    problems.append(f"{step_label(device, destination, settings)}: {path} not found.")
                continue
            step = Step(device, destination, settings, build, device_config["package"], version,
                        version_code(app, version))
            problems += [f"{step.label}: {problem}" for problem in check(step)]
            steps.append(step)
    if not steps and not problems:
        problems.append("Nothing to upload: no selected destination is configured for the selected devices.")
    return steps, problems


def check(step: Step) -> list[str]:
    settings, build = step.settings, step.build
    problems = [f"set '{key}' in config.toml." for key in REQUIRED_SETTINGS[step.destination] if not settings.get(key)]
    env = REQUIRED_ENV.get(step.destination, ())
    if step.destination == "meta" and settings.get("secret_env"):
        env += (settings["secret_env"],)
    problems += [f"set {name} in .env." for name in env if not os.environ.get(name)]
    if step.destination != "manual" and find_tool(step.destination) is None:
        problems.append(f"{TOOL_HINTS[step.destination]} not found.")
    if step.destination == "meta" and build.obb is None and build.apk.stat().st_size > META_MAX_APK_BYTES:
        problems.append("APK is over 1 GB and has no OBB. Build with Split Application Binary on.")
    if step.destination == "arborxr" and build.obb and step.version_code is None:
        problems.append("set 'version_code' for the app in config.toml; it names the OBB on the headset.")
    if build.obb and build.obb.stat().st_size > MAX_OBB_BYTES:
        problems.append("OBB is over 4 GB.")
    return problems


def obb_device_name(step: Step) -> str:
    """The name Android looks for in /sdcard/Android/obb/<package>/."""
    return f"main.{step.version_code}.{step.package}.obb"


def bundle_folder(step: Step) -> Path:
    return step.build.apk.parent / f".arborxr-{step.version}"


def channel(step: Step) -> str:
    if step.destination == "manual":
        return "set by hand"
    return step.settings.get("channel") or step.settings.get("channel_id") or "none"


def summary(step: Step) -> str:
    return f"{step.label:<28} {describe_build(step.build):<26} channel: {channel(step)}"


def command(step: Step, notes: str) -> list[str] | None:
    """The CLI call for one upload, or None for a manual upload."""
    if step.destination == "manual":
        return None
    settings, build = step.settings, step.build
    tool = find_tool(step.destination) or TOOL_HINTS[step.destination].split()[0]  # placeholder for --dry-run
    if step.destination == "arborxr":
        if build.obb:
            # A bundle folder makes abxr-cli push the OBB to /sdcard/Android/obb/<package>/ by its name.
            cmd = [tool, "app_bundles", "upload", settings["app_id"], str(build.apk), str(bundle_folder(step))]
        else:
            cmd = [tool, "apps", "upload", settings["app_id"], str(build.apk),
                   "--wait", "--wait_time", ARBORXR_WAIT_SECONDS]
        cmd += ["--notes", notes]
        if settings.get("channel_id"):
            cmd += ["--release-channel-id", settings["channel_id"]]
        return cmd
    if step.destination == "managexr":
        # mxr-cli has no release notes option; --description would overwrite the app description.
        cmd = [tool, "--yes", MANAGEXR_CLI, "upload-app", str(build.apk)]
        if build.obb:
            cmd += ["--obb-file", str(build.obb)]
        if settings.get("channel"):
            cmd += ["--release-channel", settings["channel"]]
        return cmd
    cmd = [tool, "upload-quest-build",  # meta
           "--app-id", settings["app_id"],
           "--app-secret", os.environ.get(settings["secret_env"], ""),
           "--apk", str(build.apk),
           "--channel", settings["channel"],
           "--age-group", settings["age_group"],
           "--notes", notes]
    if build.obb:
        cmd += ["--obb", str(build.obb)]
    return cmd


def preview(step: Step, notes: str) -> str:
    """The command as text, with the notes shortened and the Meta secret hidden."""
    cmd = command(step, notes)
    if cmd is None:
        return f"Upload by hand at {step.settings.get('url')}"
    hidden = {"--notes": "<release notes>", "--app-secret": "***"}
    return " ".join(shlex.quote(hidden.get(flag, arg)) for flag, arg in zip([""] + cmd, cmd))


# --- Uploading -------------------------------------------------------------------------------


def read_lines(stream):
    """Yield (text, replaces_previous) from a byte stream; a bare \\r is a progress bar redrawing its line."""
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    line, after_cr, redraw = "", False, False
    for chunk in iter(lambda: stream.read1(65536), b""):
        for char in decoder.decode(chunk):
            if after_cr:
                after_cr = False
                if char == "\n":
                    yield line, redraw
                    line, redraw = "", False
                    continue
                yield line, redraw
                line, redraw = "", True
            if char == "\r":
                after_cr = True
            elif char == "\n":
                yield line, redraw
                line, redraw = "", False
            else:
                line += char
    if line or after_cr:
        yield line, redraw


def prepare_bundle_folder(step: Step) -> Path:
    """A folder holding only the OBB, renamed for the device. A hard link avoids copying 2 GB."""
    folder = bundle_folder(step)
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir()
    target = folder / obb_device_name(step)
    try:
        os.link(step.build.obb, target)
    except OSError:
        shutil.copyfile(step.build.obb, target)
    return folder


def run_step(step: Step, notes: str, emit=None) -> bool:
    """Run one upload. `emit(text, replaces_previous)` gets the output; None leaves it on the console."""
    say = (lambda text: emit(text, False)) if emit else print
    folder = None
    try:
        if step.destination == "arborxr" and step.build.obb:
            folder = prepare_bundle_folder(step)
        cmd = command(step, notes)
        if emit is None:
            return subprocess.run(cmd, stdin=subprocess.DEVNULL).returncode == 0
        process = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},  # abxr-cli would print cp1252 into a pipe on Windows
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # no console popping up under pythonw
        )
        for text, replaces_previous in read_lines(process.stdout):
            emit(text, replaces_previous)
        return process.wait() == 0
    except OSError as error:
        say(f"ERROR: {error}")
        return False
    finally:
        if folder:
            shutil.rmtree(folder, ignore_errors=True)


def console_manual_upload(step: Step, notes: str) -> bool:
    print(f"Upload these files at {step.settings['url']}:")
    for path in filter(None, (step.build.apk, step.build.obb)):
        print(f"  {path}")
    print(f"Release notes:\n{notes}\n")
    webbrowser.open(step.settings["url"])
    return input(f"Type 'y' once the {step.settings['name']} upload is done: ").strip().lower() == "y"


def upload(steps: list[Step], notes: str, emit=None, manual=console_manual_upload) -> list[tuple[Step, bool]]:
    """Run every step in order. A failure does not stop the rest."""
    say = (lambda text: emit(text, False)) if emit else print
    results = []
    for step in steps:
        say(f"\n=== {step.label} ===")
        ok = manual(step, notes) if step.destination == "manual" else run_step(step, notes, emit)
        say("Done." if ok else "FAILED.")
        results.append((step, ok))
    return results


def main(argv=None) -> int:
    load_env()
    try:
        config = load_config()
    except FileNotFoundError as error:
        print(error)
        return 1
    all_devices = sorted({device for app in config["apps"].values() for device in app["devices"]})
    parser = argparse.ArgumentParser(description="Upload Android XR builds to ArborXR, ManageXR and Meta Horizon.")
    parser.add_argument("app", choices=config["apps"])
    parser.add_argument("version", nargs="?", help="default: the newest version in the builds folders")
    parser.add_argument("--device", nargs="+", choices=all_devices, help="default: every device of the app")
    parser.add_argument("--only", nargs="+", choices=DESTINATIONS, default=DESTINATIONS, help="destinations")
    parser.add_argument("--notes-file", type=Path, help="default: the notes saved by the window for this version")
    parser.add_argument("--dry-run", action="store_true", help="check everything and print the commands")
    args = parser.parse_args(argv)

    app = config["apps"][args.app]
    version = args.version or next(iter(find_versions(app)), None)
    if version is None:
        parser.error(f"no builds found in {builds_dir(app)}")
    if args.notes_file:
        save_notes(args.app, version, args.notes_file.read_text(encoding="utf-8"))
    notes = load_notes(args.app, version).strip()

    steps, problems = plan(config, args.app, version, args.device, args.only, notes)
    print(f"{app['name']} {version}")
    for step in steps:
        print(f"  {summary(step)}")
        if args.dry_run:
            print(f"    {preview(step, notes)}")
    for problem in problems:
        print(f"  ! {problem}")
    if args.dry_run or problems:
        return 1 if problems else 0
    if input("Upload? [y/N] ").strip().lower() != "y":
        return 1

    results = upload(steps, notes)
    print()
    for step, ok in results:
        print(f"{'OK    ' if ok else 'FAILED'} {step.label}")
    return 0 if all(ok for _, ok in results) else 1


if __name__ == "__main__":
    sys.exit(main())
