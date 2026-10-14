# ROM hacks as patch mods

A ROM hack can ship as a regular recomp mod (`.nrm`) that holds only a BPS patch against the
stock ROM. Players install it like any other mod; the runtime applies the patch to their own
ROM when the game starts. No ROM data is distributed.

## Making one

The packager is shared by all six AKI ports and lives in
[AKI Mods](https://github.com/jessetbh/AkiMods) (`tools/romhack/make_patch_mod.py`, with its own
README and tests). `make_patch_mod.py` here is a wrapper that runs it from this repository root;
it finds AKI Mods through `AKIMODS_DIR` or a sibling `..\AkiMods` checkout.

```powershell
python tools\romhack\make_patch_mod.py --base nomercy.z64 --hack my_hack.z64 `
    --game-id wwfnomercy --id my_hack --name "My Hack" --out my_hack.nrm
```

## Repaired functions

`list_repaired_functions.py` writes `RecompiledFuncs/repaired_functions.txt`: the functions this
port's post-recompile pipeline or hand-edits changed. The live recompiler that runs a patch mod's
changed functions has none of those repairs, so:

- the packager marks such a function REPAIRED in its report;
- the build embeds the list (CMakeLists.txt generates `repaired_functions.cpp`) and registers it
  with the runtime at startup, which logs `[romhack] <mod>: 0x... was repaired by the port
  offline; recompiling it from the patch drops that repair` when a patch mod changes one. The
  policy is Warn (the mod still loads); `recomp::mods::RepairedFunctionPolicy::Refuse` would
  refuse it.

CI runs `list_repaired_functions.py` after the pipeline, so release builds carry a current list.
Run it yourself after regenerating `RecompiledFuncs/` (then re-run CMake's configure step).

## Verified

Survey of 18 hack ROMs (2026-10-10): runs as a patch mod today: VPW2 Freem Edition, VPW2
English Patched, VPW2 English Beta 3 (rey), VPW2 Unlocked, All-Japan Pro Wrestling 64 (Beta 3,
a VPW2 hack), WWF No Mercy Unlocked. Needs a full recompile (relinked code or another base):
No Mercy Plus v2.1/V3, ECW Born to be Wired, ECW House Party Mixtape, Project Outsider
Extreme, WrestleMania X 64, SUMMIT VPW Gamma, Revenge Starrcade Edition, WCW vs. nWo 9
Underground, and the VPW2 "(English Patch)" zip release (escaping branches).

VPW2 Freem Edition (2024-06-15): 17 changed functions (75 instructions) plus ~31 MB of data.
On the stock build with the patch mod, all 17 are recompiled at load, the game boots into
Freem, and attract-mode demos match the dedicated full-recompile Freem build frame for frame
(with the same save; VPW2 keeps edited costumes in SRAM).
