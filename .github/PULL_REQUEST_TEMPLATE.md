## What does this PR do?



## How was it tested?

<!-- e.g. booted a release build, played a match, opened the config menu -->

## Checklist

- [ ] No ROM data or extracted game assets are included
- [ ] Changes under `lib/` were made in the corresponding jessetbh fork repo (its
      `wcw` branch) and the submodule pin here is bumped to the pushed commit
- [ ] New symbol identifications include evidence in `disasm/libultra.md`
- [ ] If the recompiler output was regenerated, ALL post-regen steps in
      `BUILDING.md` were re-run (fix_stumps, fix_switches, fix_backbranches,
      readd_hand_edits, audit_coresidency)
