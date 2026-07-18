# Code signing the Windows build

The current `npm run tauri build` produces an **unsigned** NSIS installer, so
Windows SmartScreen shows an "unknown publisher / Windows protected your PC"
warning on first run. This document is the recipe for signing it. It is not
wired into the build yet — adding a signing config with no certificate would
break the working unsigned build, so enable it only once you have a cert.

## First decide the distribution channel

- **Direct download** (Gumroad, GitHub releases): you need your own code-signing
  certificate; Tauri signs the installer during `tauri build`. Continue below.
- **Microsoft Store** (MSIX): Microsoft signs the package for you — no
  certificate needed, one-time ~$19 developer account. Tauri emits NSIS/MSI, not
  MSIX, so this needs an extra MSIX-packaging step (out of scope here).

## Reality check on SmartScreen

Signing changes "Unknown publisher" to your verified name and starts a
per-identity SmartScreen reputation that clears as downloads accumulate without
malware reports. No certificate type guarantees an instant zero-warning first
release anymore (Microsoft walked back the old EV instant-reputation behavior).
Sign early so reputation starts building.

## Certificate options (direct download)

| Option | Cost | Hardware token | Notes |
|---|---|---|---|
| **Azure Trusted Signing** | ~$10/mo | No (cloud) | Microsoft-run, CI-friendly, cheapest. Recommended for a solo dev. Requires identity validation (organization, or individual). |
| OV cert (Sectigo, SSL.com, DigiCert) | ~$200–400/yr | Yes (mandatory since June 2023) | Traditional; sign with a plugged-in USB token. |
| EV cert | ~$300–700/yr | Yes | Historically instant trust; less advantageous now. |

### Two things to know before choosing Trusted Signing

**It is a signing *service*, not a certificate you own.** Microsoft holds the
cert in their cloud and you call the service to sign each artifact — that is why
there is no USB token and why it bills ~$9.99/month instead of once a year. It is
an ongoing subscription: stop paying and you can no longer sign *new* builds,
though anything already signed with a timestamp stays valid indefinitely. (An
OV/EV cert is the opposite: a yearly purchase you hold on a token.)

**Check eligibility before subscribing — validation is where people get stuck.**
Trusted Signing requires identity validation:
- **Organization**: needs a verifiable legal entity; at launch Microsoft required
  the business to be roughly **3+ years old** (checked against public / D&B
  records). A newer entity can be blocked on this path.
- **Individual**: a separate personal government-ID verification path — use this
  if you do not have an established company.

Confirm you qualify *first*, then subscribe. If the org-age rule blocks you and
individual validation does not fit, fall back to an OV cert on a token (no
entity-age requirement, but adds the token friction).

### Cross-platform cost

This document is Windows only. Signing is per-platform and the certificates do
not transfer:

| Platform | What you need | Cost |
|---|---|---|
| Windows | Azure Trusted Signing (or OV/EV cert) | ~$10/mo (or ~$200–700/yr) |
| macOS | Apple Developer Program (sign + notarize) | $99/yr |

So shipping both platforms means budgeting both. macOS signing is covered in its
companion doc, [SIGNING-MACOS.md](SIGNING-MACOS.md).

## Option A — Azure Trusted Signing (recommended)

1. In the Azure portal: create a **Trusted Signing account** and a **certificate
   profile**, then complete identity validation. Note the account endpoint
   (e.g. `https://wus2.codesigning.azure.net/`), account name, and profile name.
2. Install the signing CLI: `cargo install trusted-signing-cli` (or use the
   `dotnet sign` tool). Authenticate with `az login` or set the
   `AZURE_CLIENT_ID` / `AZURE_CLIENT_SECRET` / `AZURE_TENANT_ID` env vars.
3. Add a `signCommand` to `desktop/src-tauri/tauri.windows.conf.json` under
   `bundle.windows` — Tauri passes each artifact path in place of `%1`:

   ```json
   {
     "$schema": "https://schema.tauri.app/config/2",
     "bundle": {
       "resources": { "binaries/_internal": "_internal" },
       "windows": {
         "signCommand": "trusted-signing-cli -e https://wus2.codesigning.azure.net/ -a MyAccount -c MyCertProfile %1"
       }
     }
   }
   ```

## Option B — Hardware-token OV/EV certificate

1. Buy an OV or EV code-signing cert; the CA ships it on a FIPS USB token.
2. Install the token drivers and plug it in. Find the cert thumbprint:
   `Get-ChildItem Cert:\CurrentUser\My | Format-List Subject, Thumbprint`
3. Add to `tauri.windows.conf.json` under `bundle.windows`:

   ```json
   {
     "$schema": "https://schema.tauri.app/config/2",
     "bundle": {
       "resources": { "binaries/_internal": "_internal" },
       "windows": {
         "certificateThumbprint": "PASTE_THUMBPRINT_NO_SPACES",
         "digestAlgorithm": "sha256",
         "timestampUrl": "http://timestamp.digicert.com"
       }
     }
   }
   ```

   Always set `timestampUrl` — a timestamp keeps signatures valid after the
   certificate expires.

## Build and verify

```powershell
cd desktop
npm run tauri build -- --bundles nsis
# Confirm the signature on the produced installer:
signtool verify /pa /v "src-tauri\target\release\bundle\nsis\AI DocPrep_*_x64-setup.exe"
```

`signtool` ships with the Windows SDK (under
`C:\Program Files (x86)\Windows Kits\10\bin\<ver>\x64\`).

## Also sign the Python sidecar (optional but cleaner)

Tauri signs the app binary and the installer, but the bundled
`docprep-core.exe` sidecar is a separate nested executable it does not sign.
SmartScreen judges the installer the user runs, so this is not a blocker, but
for a fully-signed bundle add a signing call at the end of
`desktop/build-sidecar.ps1` after the exe is placed in `binaries/` (same
`trusted-signing-cli … %1` or `signtool sign …` command, pointed at the
sidecar exe).

## CI note

Store credentials as CI secrets, never in the repo. Azure Trusted Signing works
well in GitHub Actions (there is a `azure/trusted-signing-action`); hardware-token
certs generally cannot run in hosted CI because the physical token must be
present, which is another reason to prefer Trusted Signing.
