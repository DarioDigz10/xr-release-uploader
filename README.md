# XR Release Uploader

[![tests](https://github.com/DarioDigz10/xr-release-uploader/actions/workflows/tests.yml/badge.svg)](https://github.com/DarioDigz10/xr-release-uploader/actions/workflows/tests.yml)

One window to ship an Android XR release to every device-management portal and store: pick the
version, write the release notes once, click **Upload**.

![The uploader window](docs/screenshot.png)

## Why

An enterprise VR app usually ships to several places at once. Each release of the apps this tool
was built for meant:

- two builds (Pico and Quest), each about 2 GB;
- up to four destinations per build: [ArborXR](https://arborxr.com),
  [ManageXR](https://www.managexr.com), the [Meta Horizon Store](https://developers.meta.com/horizon/)
  and an in-house dashboard;
- different rules per destination: Meta rejects APKs over 1 GB, so Quest builds go there as
  APK + OBB, while ArborXR needs the OBB under the exact name the headset looks for.

That is up to 14 manual uploads per release, in four web portals, with the release notes pasted
into each one. This tool turns it into one click and a coffee.

## Design

- **Python standard library only.** `tkinter` for the window, `subprocess` for the uploads. It
  runs on a stock Python install, so anyone on the team can use it.
- **It wraps the official CLIs.** It calls `abxr-cli`, `mxr-cli` and `ovr-platform-util` instead
  of reimplementing their APIs, so uploads keep working when the vendors change their endpoints.
- **Check everything before uploading anything.** Missing builds, credentials, tools or config
  fields are listed together, and **Simulate** prints every command (secrets masked) before a
  byte leaves the machine.
- **No state.** A failed upload does not stop the others; to retry, select only what failed.
- **Config, not code.** Apps, devices, file names, build folders and release channels live in
  `config.toml`. Secrets live in `.env`. Neither is committed.

## Destinations

| Destination | How | Release notes | Release channel |
|---|---|---|---|
| ArborXR | `abxr-cli apps upload`; `app_bundles upload` for APK + OBB | Yes | `channel_id`, optional |
| ManageXR | `npx @managexr/mxr-cli upload-app` | No: the CLI has no notes field | `channel`, optional |
| Meta Horizon | `ovr-platform-util upload-quest-build` | Yes | `channel`, required |
| Any other portal (`manual`) | Opens the URL, lists the files, copies the notes; you upload by hand | Copied to the clipboard | Set by hand |

## Setup

1. Install Python 3.11 or later. On Windows, use python.org or
   `winget install Python.Python.3.12`; the Microsoft Store alias does not include everything.
2. Install the ArborXR CLI: `py -m pip install -r requirements.txt`.
3. Install [Node.js](https://nodejs.org) for the ManageXR CLI, which runs through `npx`.
4. Download `ovr-platform-util` from Meta's
   [Platform Command Line Utility](https://developers.meta.com/horizon/resources/publish-reference-platform-command-line-utility/)
   page.
5. Copy `.env.example` to `.env` and fill in the API keys and the path to `ovr-platform-util`.
6. Copy `config.example.toml` to `config.toml` and describe your apps. The comments at the top
   of the example explain every field.

Only the destinations you configure need their tool and credentials.

## Build folders

The tool does not build anything: it picks up what Unity wrote. A typical layout, where Meta
gets the split build and every other destination the single APK:

```
Builds/
├── Pico/
│   └── MyApp_Pico_1.9.2.apk
└── Quest/
    ├── MyApp_Quest_1.9.2.apk           <- ArborXR, ManageXR, manual
    └── Meta/
        ├── MyApp_Quest_1.9.2.apk       <- Meta Horizon
        └── MyApp_Quest_1.9.2.main.obb
```

A build is split when a `<apk name>.main.obb` sits next to the APK, which is what Unity writes
with **Split Application Binary** on. Each destination can take its build from its own `folder`.

## Usage

Double-click `gui.pyw` (or run `python gui.pyw`):

1. Choose the app. The newest version found in its build folders is selected.
2. Write or paste the release notes. They are saved per app and version in `notes/`.
3. Click **Simulate** to see every upload, its command and anything left to fix.
4. Click **Upload** and confirm. Output from each CLI streams into the progress log.

The same flow without the window:

```bash
python release.py myapp 1.9.2 --dry-run
python release.py myapp 1.9.2 --device quest --only meta
python release.py myapp --notes-file notes.md
```

## Tests

```bash
python -m unittest
```

## License

[MIT](LICENSE)
