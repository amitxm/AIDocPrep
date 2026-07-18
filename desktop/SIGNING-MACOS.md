# Code signing the macOS build

Companion to [SIGNING.md](SIGNING.md), which covers Windows.

## Why this one matters more

Windows SmartScreen says "unknown publisher" — that's hesitation. macOS says the
app **"is damaged and can't be opened. You should move it to the Trash"** — that
reads as malware, and most people believe it. Today the README ships an `xattr`
workaround, which is fine for developers and useless for everyone else. Signing
plus notarization removes the warning entirely, and it is almost certainly the
single highest-conversion change available to the Mac download.

## Prerequisite: Apple Developer Program

**$99/year**, at [developer.apple.com/programs](https://developer.apple.com/programs/).
There is no free path to a Developer ID certificate. Enrollment takes a day or
two (individual is faster than organization). Everything below is blocked on it.

## Which certificate

| Destination | Certificate | Also required |
|---|---|---|
| **Direct download** (`.dmg` on GitHub / Gumroad) | **Developer ID Application** | Notarization |
| Mac App Store | Apple Distribution | App Sandbox — hard with a Python sidecar; treat as a separate project |

This document covers direct download.

## The tools

Tauri wraps all of these, so you rarely call them by hand:

- `codesign` — signs bundles and individual binaries
- `xcrun notarytool` — uploads the build to Apple's malware scan (replaced the old `altool`)
- `xcrun stapler` — attaches the notarization ticket so it validates offline

Install with `xcode-select --install`; full Xcode is not required.

*Off-Mac alternative:* [`rcodesign`](https://github.com/indygreg/apple-platform-rs)
(the Rust `apple-codesign` crate) can sign **and** notarize macOS apps from
Windows or Linux — worth knowing since the main dev box here is Windows.

## One-time setup

1. Create a **Developer ID Application** certificate (Xcode → Settings →
   Accounts → Manage Certificates, or the developer portal). It lands in your
   login keychain.
2. Find the identity string:
   ```bash
   security find-identity -v -p codesigning
   ```
   Copy the full `Developer ID Application: Your Name (TEAMID)` value.
3. Create an **App Store Connect API key** (App Store Connect → Users and Access
   → Integrations → Keys). Download the `.p8` — you only get one chance. Note
   the Key ID and Issuer ID. This is preferred over an Apple ID plus
   app-specific password, and it works unattended in CI.

## Build

```bash
export APPLE_SIGNING_IDENTITY="Developer ID Application: Your Name (TEAMID)"
export APPLE_API_KEY=ABCD123456
export APPLE_API_ISSUER=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
export APPLE_API_KEY_PATH="$HOME/private_keys/AuthKey_ABCD123456.p8"

cd desktop
npm run tauri build
```

With `APPLE_SIGNING_IDENTITY` set, Tauri signs automatically; with the API key
variables set it also submits for notarization, waits for the result, and
staples the ticket.

## The part that will fail first: the Python sidecar

Notarization requires **every** Mach-O binary inside the bundle to be signed
with your Developer ID and built with the hardened runtime. This app ships:

- `docprep-core` — the Tauri `externalBin` sidecar
- `_internal/` — the PyInstaller onedir payload: hundreds of `.so` / `.dylib`
  files from Python, markitdown, and spaCy

Tauri signs the app and the sidecar executable, but nested libraries in bundled
resources are a known gap ([tauri-apps/tauri#11992](https://github.com/tauri-apps/tauri/issues/11992)).
Expect the first notarization attempt to be rejected.

Fix it by signing **inside-out** in `build-sidecar.sh`, before `tauri build`.
Sign libraries *without* entitlements and executables *with* them — entitlements
on a library are ineffective and can themselves trigger rejections:

```bash
IDENT="Developer ID Application: Your Name (TEAMID)"

# 1. every bundled library — hardened runtime, no entitlements
find binaries/_internal -type f \( -name "*.so" -o -name "*.dylib" \) \
  -exec codesign --force --timestamp --options runtime -s "$IDENT" {} +

# 2. the sidecar executable — hardened runtime, with entitlements
codesign --force --timestamp --options runtime \
  --entitlements src-tauri/entitlements.plist -s "$IDENT" binaries/docprep-core
```

Create `desktop/src-tauri/entitlements.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>com.apple.security.cs.allow-unsigned-executable-memory</key><true/>
  <key>com.apple.security.cs.allow-jit</key><true/>
</dict>
</plist>
```

and point `tauri.conf.json` → `bundle.macOS.entitlements` at it. CPython needs
`allow-unsigned-executable-memory`.

Do **not** add `com.apple.security.cs.disable-library-validation` reflexively.
Once every bundled `.so` carries your own Team ID signature, library validation
passes on its own, and the entitlement only makes Gatekeeper stricter elsewhere.
Add it only if you actually hit a library-validation failure.

## When notarization is rejected, read the log

```bash
xcrun notarytool log <submission-id> \
  --key "$APPLE_API_KEY_PATH" --key-id "$APPLE_API_KEY" --issuer "$APPLE_API_ISSUER"
```

The log names every offending binary. Iterate until it comes back clean.

## Verify before shipping

```bash
codesign -dv --verbose=4 "/Applications/AI DocPrep.app"
spctl -a -vvv -t install "/Applications/AI DocPrep.app"   # want: accepted, source=Notarized Developer ID
xcrun stapler validate "src-tauri/target/release/bundle/dmg/AI DocPrep_"*.dmg
```

The real test: download the `.dmg` in a browser on a Mac that has never seen the
app, and open it. No warning means it's done — and the `xattr` instructions can
come out of the README.

## Intel Macs

The alpha ships `aarch64` only, so Intel Macs cannot run it at all. A universal
build needs a universal Python sidecar, and PyInstaller cannot cross-build: you
would build the sidecar on both architectures and merge them with `lipo`. Decide
whether Intel is worth supporting before promoting the Mac download widely.

## CI

GitHub Actions with `tauri-action` on a macOS runner works well. Store the
certificate as base64 in `APPLE_CERTIFICATE` with `APPLE_CERTIFICATE_PASSWORD`,
plus the API key values, as repository secrets. Unlike the Windows
hardware-token route, macOS signing runs fine on hosted CI.
