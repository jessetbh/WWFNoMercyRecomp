# Building Guide

**Building is NOT required to play** — download a release zip instead (see the
README). This guide is for contributors building from source on Windows (the beta
is Windows-only; CI in `.github/workflows/validate.yml` runs these exact steps).

The pipeline has two halves:

1. **Recompile** — run `N64Recomp` on your ROM to generate `RecompiledFuncs/`
   (~19 MB of C), run the **mandatory post-regen fixups** (§4 — this game needs
   more of them than its sisters), and `RSPRecomp` to generate the
   audio-microcode CPU translation.
2. **Port build** — compile the generated C plus the runtime stack
   (ultramodern + librecomp + RecompFrontend + RT64) with **clang-cl** into
   `NoMercyRecompiled.exe`.

Like its sisters Revenge and VPW64, there is no `patches/` pipeline yet, so no
MIPS-capable clang or GNU make is needed — the toolchain list is short.

## 1. Prerequisites

- **Git**
- **VS Build Tools 2022** with clang-cl, CMake, and Ninja (needs UAC):

  ```powershell
  winget install --id Microsoft.VisualStudio.2022.BuildTools --override "--passive --wait --add Microsoft.VisualStudio.Workload.VCTools --add Microsoft.VisualStudio.Component.VC.Llvm.Clang --add Microsoft.VisualStudio.Component.VC.Llvm.ClangToolset --add Microsoft.VisualStudio.Component.VC.CMake.Project --includeRecommended"
  ```

  RT64 is D3D12/Vulkan and needs clang-cl + the Windows SDK; MinGW cannot
  substitute for the port build.
- **Python 3** — the post-regen fixup scripts in §4 require it (unlike the
  sisters, it is not optional here). A splat venv is only needed for
  regenerating symbols (§7).

## 2. Clone

```powershell
git clone --recursive https://github.com/jessetbh/WWFNoMercyRecomp.git
cd WWFNoMercyRecomp
```

`--recursive` matters: `lib/` contains the runtime-stack forks (with required
`[wcw fix]` / `[wcw2k]` changes shared with the sister projects). Symbol metadata
is tracked directly in this repo under `syms/`.

**Windows path-length warning**: the nested submodule tree produces long internal
git paths, and cloning into a deep directory (OneDrive Documents, etc.) can fail
with `Filename too long`. Either clone into a short path (e.g. `C:\src\`) or enable
long-path support first:

```powershell
git config --global core.longpaths true
```

## 3. Provide the ROM

Place the **WWF No Mercy (USA, Rev 1)** ROM as `nomercy.z64` in the repo
root — SHA1 `91CEE3D096F4A76644D8B35B9AEAD6448909ABD1`, big-endian. Earlier USA
revisions (V1.0/V1.1) and other regions will not work. It is used only by the
recompiler steps below; at runtime the launcher asks for (and remembers) a ROM
on first start. The code is not compressed, so there is no decompression step.

## 4. Build N64Recomp + RSPRecomp, then recompile

Build the recompiler CLI from upstream at the pinned commit
(`ffb39cdad1da5de07eaaa48bd1db4a89a7986771` — the commit `RecompiledFuncs/` was
last generated with; any toolchain works, VS is fine):

```powershell
git clone --recurse-submodules https://github.com/N64Recomp/N64Recomp.git N64RecompSource
git -C N64RecompSource checkout ffb39cdad1da5de07eaaa48bd1db4a89a7986771
git -C N64RecompSource submodule update --init --recursive
cmake -S N64RecompSource -B N64RecompSource/build -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build N64RecompSource/build --target N64RecompCLI RSPRecomp
copy N64RecompSource\build\N64Recomp.exe .
copy N64RecompSource\build\RSPRecomp.exe .
```

(Not to be confused with the `jessetbh/N64Recomp` fork pinned inside
`lib/N64ModernRuntime` — that one only supplies the `recomp.h` header the port
compiles against.)

Then recompile the game and run the **mandatory post-regen pipeline** (from the
repo root). The AKI engine's binaries (proven on WrestleMania 2000, No Mercy's
direct predecessor) are unusually hostile to naive
recompilation — IDO cold-path fragments, overlapping symbols, and jump tables
that cross section boundaries each produce a *silent* miscompile class that one
of these scripts detects or repairs. **Every step below is required after every
regeneration**; skipping one produces a build that boots but corrupts itself
later.

```powershell
.\N64Recomp.exe nomercy.toml                  # -> RecompiledFuncs/
#   (or: python tools\recomp_loop.py — auto-handles IDO shared-tail errors)
python tools\fix_stumps.py                 # rechains functions N64Recomp truncated
#   at overlapping same-section symbols (dropped epilogues = stack corruption)
python tools\fix_switches.py               # completes jump tables truncated at
#   out-of-function entries (truncated tables abort the process via switch_error)
python tools\fix_backbranches.py           # audits every function for branches
#   outside its decoded span (N64Recomp silently reroutes them to a bogus local
#   label). If it prints MISSING lines: add the printed extra_funcs entries,
#   regenerate, and repeat until it reports 0 missing.
python tools\fix_selfentry.py              # relocates misplaced self-entry branch
#   labels: when a function branches to its own first instruction, N64Recomp emits
#   the label AFTER the entry instruction, so looped iterations skip it (this caused
#   the exploded-wrestler match-render bug — see docs history). Idempotent.
python tools\readd_hand_edits.py           # re-applies documented in-place
#   diagnostics to the generated C (they are wiped by every regen)
python tools\audit_coresidency.py          # cross-overlay static-bind sweep;
#   must print 0 violations
.\RSPRecomp.exe rsp\nomercy_audio.toml        # -> rsp/nomercy_audio.cpp
```

If the regeneration changed the set of `RecompiledFuncs/funcs_*.c` files,
re-run the CMake **configure** step (§5) before building — the file list is
captured at configure time.

## 5. Build the port

Configure and build with clang-cl (`tools/env-msvc.ps1` puts the VS tools on
PATH for the current shell):

```powershell
. .\tools\env-msvc.ps1
cmake -S . -B build-msvc -G Ninja -DCMAKE_C_COMPILER=clang-cl -DCMAKE_CXX_COMPILER=clang-cl -DCMAKE_BUILD_TYPE=Release
cmake --build build-msvc --target NoMercyRecompiled
```

The exe lands at `build-msvc\NoMercyRecompiled.exe`, with `assets/`,
`recompcontrollerdb.txt`, and the SDL2/DXC DLLs copied next to it — that folder is
a complete, runnable install.

Build types: `Release` is the shipping configuration (windowed app; stdio goes to
`%LOCALAPPDATA%\NoMercyRecompiled\NoMercyRecompiled.log`, or launch with
`--show-console`); `Debug` keeps the console.

## 6. Run

Double-click `NoMercyRecompiled.exe`. First run: **Load ROM** → pick your ROM →
it is validated and stored → **Start Game**. Saves and config live in
`%LOCALAPPDATA%\NoMercyRecompiled\` (create an empty `portable.txt` next to the
exe for a portable install).

Dev conveniences:

- `WCW_AUTOBOOT=<path|1>` — skip the launcher and boot straight into the game
  (`1` uses the stored ROM).
- When testing, capture **both** stdio streams
  (`.\NoMercyRecompiled.exe > out.log 2> err.log`) — recompiler `switch_error`
  aborts print to stdout only.
- Crash backtraces print RVAs that resolve against `build-msvc\
  NoMercyRecompiled.map` (always emitted): `python tools\symbolize.py --log <log>`.
- Env-gated diagnostics: `WCW_HEALTH_LOG=1` (1/s frame-rate + message-queue
  health), `WCW_AUDIO_LOG=1`, `WCW_VI_LOG=1`, plus deeper hunt tooling
  (`WCW2K_WRITEWATCH`, `WCW2K_TASKWATCH`, `WCW2K_VTXLOG`, `WCW2K_MTXLOG`) —
  see the comments at their definition sites.

## 7. Regenerating symbols (contributors)

`syms/dump.toml` is generated; the regeneration tooling is the splat project in
`disasm/` + `tools/gen_symbols.py` (the libultra `RENAME` map — populated by
fingerprint transfer from the sister project Revenge, see `tools/recon*.py` and
`disasm/libultra.md`). Requires Python and a splat venv, plus the ROM copied to
`disasm\nomercy.z64`:

```powershell
python -m venv disasm\.venv
disasm\.venv\Scripts\pip install splat64 spimdisasm rabbitizer n64img crunch64 pygfxd
cd disasm; ..\disasm\.venv\Scripts\python.exe -m splat split nomercy.yaml; cd ..
python tools\gen_symbols.py                # -> syms/dump.toml
python tools\gen_stubs.py                  # refresh the stub block in nomercy.toml
```

After any re-split or symbol change, rerun the recompiler AND the full §4
post-regen pipeline (`syms/extra_funcs.txt` holds the multi-entry injections
the pipeline maintains — audit stale entries after a re-split; a stale entry
with a wrapped offset silently hijacks the overlay function lookup).

## 8. Changing anything under `lib/`

`lib/` submodules are jessetbh forks carrying the `[wcw fix]` + `[wcw2k]` sets on
`wcw` branches, shared with the sister projects
([World Tour](https://github.com/jessetbh/WCWvsNWOWorldTourRecomp),
[Revenge](https://github.com/jessetbh/WCWnWoRevengeRecomp)). After ANY edit under
`lib/`: commit on that repo's `wcw` branch, push to the fork, and bump the
submodule pin here **and in the sisters**. The diff-vs-upstream record
(`lib-patches/`) is maintained in the World Tour repo.
