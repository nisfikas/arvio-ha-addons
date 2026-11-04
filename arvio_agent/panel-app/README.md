# panel-app

What wall panels get from the hub besides `panel.html`. `icon.png` (180 px, the Home app's icon) is
the home-screen icon of an iPad in wall mode (`/panel/icon.png` and the profile from
`/panel/ipad.mobileconfig`). The rest of this file is about the Android app.

The Arvio Panel app that the hub installs on wall panels over ADB and serves to them for self-update
(`/panel/app.json`, `/panel/app.apk`). Design: `docs/ADR/012-native-wall-panel-app.md` in the arvio repo.

Without a release here, the hub answers `no_apk` to «Εγκατάσταση» and 404 to the two URLs.

## Putting a release here

1. In the arvio repo, with SFK's panel key (on Νικόλας's Mac, never in a repo; bump `VERSION_CODE`):
   `ARVIO_PANEL_KEYSTORE=~/arvio-keys/arvio-panel.p12 VERSION_CODE=2 VERSION_NAME=0.1.1 apps/panel-android/build.sh --release`
2. Copy `out/arvio-panel.apk` and `out/app.json` into this folder.
3. Bump the agent version as usual. Panels take the new app from their hub, offline.

`release-cert.sha256` holds the SHA-256 of the release certificate, written once at the key ceremony.
The agent's tests refuse an APK whose `app.json` names another signer. A dev build here would be
installed on customers' panels, and Android would then refuse every release update until a factory
reset, because an app can only be updated with the key it was first signed with.
