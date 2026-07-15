<!-- Fill in the changelog, delete this comment, then publish the draft. -->
## Changes

- TODO changelog bullets

## Notes

- **No game assets are included.** You must supply your own WWF No Mercy
  (USA, Rev 1) ROM — SHA1 `91CEE3D096F4A76644D8B35B9AEAD6448909ABD1`. On first
  launch, click **Load ROM** and select it; the launcher validates and remembers
  it. The original USA release (Rev 0) and other regions are not accepted.
- **Windows SmartScreen**: the exe is unsigned, so Windows may warn on first run.
  Click "More info" → "Run anyway".
- **Saves and settings** live in `%LOCALAPPDATA%\NoMercyRecompiled\` — the cart's
  flash save (progress, unlocks, created wrestlers) persists automatically. Save data is
  compatible across releases unless a release note says otherwise. For a portable
  install, create an empty `portable.txt` next to the exe.
- **GPU**: D3D12 by default. If you hit rendering issues, update your GPU drivers
  first, and please attach `%LOCALAPPDATA%\NoMercyRecompiled\NoMercyRecompiled.log`
  to any bug report.
- Known issues are tracked in the README.
