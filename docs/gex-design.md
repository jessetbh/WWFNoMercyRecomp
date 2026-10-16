# Game-side matrix-group (G_EX) tags: design

Roadmap: jessetbh/aki-roadmap#37. Status: design only. Nothing here is built or run yet.

Frame interpolation in the AKI games pairs matrices on the rt64 side today, with
heuristics. This doc plans the alternative: have the game tag each object's matrix with
RT64 extended-GBI (G_EX) matrix-group commands, so rt64 is told which object is which.
It covers all six games. They share one engine, and each has the same emitter.

## 1. The emitter, in every game

Each game has exactly one code site that builds a force-matrix command: `DC38000E` on
F3DEX2, or `MOVEMEM 0x9E` on F3DEX. That site is the per-object emitter. A search of
every `disasm/asm/*.s` finds one site per game. So every 3D or screen-space transform
rt64 sees in these games passes through one function per game.

The function is the same in all six games:
`emit(Gfx **dlp, s16 parity, s16 startIdx) -> s16 nextIdx`.

1. It reads `key = list[startIdx]->word0` (the layer key).
2. It walks `list[i]` while `list[i]->word0 == key` and `i < count`. Each entry starts a
   chain of nodes linked at `+0x08`.
3. **Pass 1, for each node in the chain:**
   - force-load the matrix at `node + 0x38 + parity*0x40` (physical address);
   - if `node+0x30` (vertex pointer) is set, G_VTX loads `s16 count (+0x2C)` vertices
     into buffer slot `s16 index (+0x2E)`, masked to 0x7F, in chunks of 32 on some
     games.
4. **Pass 2, for each node in the chain:** if `node+0x34` (model display list) is set,
   emit it with `gSPDisplayList`. The WM2000-generation games first emit prim/env
   colours from per-actor tables (actor = `node+0x04`, skipped if `actor+0x19` is set,
   index `(actor+0x1A & 0xF)*4 + arenaIdx*16`).
5. It returns the first index with a different key. The caller turns each key run into
   **one RSP task**. Its close function picks the task's microcode from the key and
   gets a "last task of the frame" flag, `nextIdx >= count`.

Pass 1 loads every node's vertices with its own matrix, then pass 2 draws triangles
that index across those vertices (this is the skinning). rt64 records the transform
when the vertices load (`RSP::setVertexCommon`). **So the wrap point is pass 1:** one
matrix-group command just before each node's force matrix.

Node layout, shared by all six games: `+0x00` layer key, `+0x04` actor, `+0x08` next,
`+0x2C`/`+0x2E` vertex count/index, `+0x30` vertices, `+0x34` model DL, `+0x38` and
`+0x78` the two matrix slots.

### Per-game table

All addresses are USA ROM, from each repo's `disasm/asm/1050.s`. "Arena" is the
display-list buffer of one RSP task. Its OSTask/scheduler block sits right after the DL
area, so the DL area is the hard limit.

| | WT | VPW64 | Revenge | WM2000 | No Mercy | VPW2 |
|---|---|---|---|---|---|---|
| GBI | F3DEX (gbi1) | F3DEX (gbi1) | F3DEX2 | F3DEX2 | F3DEX2 | F3DEX2 |
| Emitter | `func_80002904` | `func_800029BC` | `func_80002454` | `func_80002B84` | `func_800028B4` | `func_80002B84` |
| Caller (one call per task) | `func_8000329C` | `func_80003358` | `func_80002CC4` | `func_800033D4` | `func_80002FA4` | `func_800033D4` |
| Task open / close | `80002B98` / `80002EAC` | `80002C50` / `80002F64` | `80002664` / `800028B4` | `80002E40` / `80003120` | `80002BB4` / `80002DE4` | `80002E40` / `80003120` |
| Visible list | `D_80041C70[2][512]` (by parity) | `D_800534E0[2][512]` | `D_80047070[2][512]` | `D_800567E0[]` | `D_80061D40[]` | `D_800565C0[]` |
| List count (s16) | `D_80041C6A` | `D_800534DA` | `D_80047066` | `D_80056584` | `D_800619B4` | `D_80056364` |
| Arena pointers / index | `D_8003ED90[D_8002D2A0]` | `D_80050600[D_8002DE60]` | `D_80044590[D_8002EEBC]` | `D_80052780[D_8003BAD0]` | `D_8005D8E0[D_80043610]` | `D_80052560[D_8003C8B0]` |
| DL area per task | 0x4000 (16384 B) | 0x4000 | 0x4000 | 0x28A0 (10400 B) | 0x2EE0 (12000 B) | 0x2BC0 (11200 B) |
| Matrix load | 4× MOVEMEM `039E/0398/039A/039C 0010` | same | `DC38000E` + `DB0C0000 00010000` | same | same | same |
| G_VTX | 0x04, chunks of 32 | same | 0x01, chunks of 32 | 0x01, single load | 0x01, chunks of 32 | 0x01, single load |
| Prim/env colour tables | none | none | none | `D_80057158` / `D_80057178` | `D_80062B68` / `D_80062B88` | `D_80056F38` / `D_80056F58` |
| v→p helper | `func_80013C00` | `func_80014290` | `func_8001CE80` | `func_8002FC50` | `func_80034AE0` | `func_80030A20` |
| Bytes per node now (1 vertex chunk) | 48 | 48 | 32 | 48 (32 without colours) | 48 | 48 |
| Camera hand-off already in place | `patches/raytracing.c` (RECOMP_PATCH of the caller) | hand-edit after `80002C50` | hand-edit after `80002664` | hand-edit after `80002E40` | hand-edit after `80002BB4` | hand-edit after `80002E40` |

Pairs:
- WT and VPW64 are the same function apart from addresses and signed/unsigned compares.
- WM2000 and VPW2 are identical apart from addresses.
- Revenge is No Mercy without the colour pass, with WT's parity-indexed list.

Matrix-load details:
- F3DEX2: the `DB0C` moveword (`G_MW_FORCEMTX`, value 0x10000) is a no-op in rt64:
  `setModelViewProjChanged(false)` runs right after `forceMatrix`.
- F3DEX: rt64 reads the whole matrix on `MV_MATRIX_1` and then skips the next three
  commands, so all four MOVEMEMs must stay.

Other facts:
- Arena index: it flips after every task, not every frame.
- Mods: no AkiMods mod hooks any of the six emitters. So the
  emitters can take a hand-edit under the rule "never put a hand-edit in a function a
  mod hooks". WT's caller `func_8000329C` is already a RECOMP_PATCH.
- Data symbols: only WT has the list/count/arena symbols (in `WCWSyms/data_dump.toml`).
  The other five main checkouts have no data syms. The host-side route below uses
  constant addresses, the same way each game's `src/game/raytracing.cpp` already uses
  `CAMERA_MATRIX`, so no syms work is needed for them.

## 2. What rt64 already does without tags

This sets the bar the tags have to beat. The merged `wcw` fork already approximates
game-side identity:

- **Same-object identity.** `WcwLogic::MatrixSlotIdentity` folds the two parity slots
  (±0x40) into one id. `GameFrame::buildTransformIdMap` gives each untagged transform
  the key `address ^ ordinal`. This is roughly the "node base pointer" id the tags
  would carry.
- **Camera split.** The camera hand-off (gEXSetViewMatrixFloat / gEXSetProjMatrixFloat
  at each task head) splits each forced MVP into an object matrix and a camera.
  `matchTransform` interpolates the object matrix. Pairs whose combined matrix moved
  less than 30% are lerped as combined matrices instead.
- **Run publishing.** `WcwLogic::publishShortRun` handles runs that end early (#6
  cause 1).

So tags mostly turn guesses into facts. They do not unlock interpolation itself, which
already works.

## 3. Design

### 3.1 Where the code lives

Replace each emitter with a host C++ re-implementation, `wcw_gex_emit()`, one copy per
game in `src/game/gex_emit.cpp`. This follows the `raytracing.cpp` precedent: the core
is the same everywhere and each copy carries a constants block (addresses, GBI flavour,
colour pass yes/no, chunked or single G_VTX).

- Five games: a hand-edit at the top of the recompiled emitter, added through
  `tools/readd_hand_edits.py`. It calls `wcw_gex_emit(rdram, a0, a1, a2)` and returns
  its value in `v0`. The hand-edit returns early only when tagging is on, so the
  original recompiled body still runs otherwise.
- WT: `RECOMP_PATCH func_80002904` in `patches/raytracing.c`, calling the same core
  through a host export, as `recomp_wcw_rt_camera_view` does today.
- Gating: tag only while the frame rate is above Original (needs a small frontend
  getter). `WCW_GEX=0` turns it off. At Original the DL stays byte-identical to today.

### 3.2 Commands per node

Before each node's matrix load, emit:

```
gEXMatrixGroup(id, G_EX_INTERPOLATE_DECOMPOSE, G_EX_NOPUSH, G_MTX_MODELVIEW,
               pos/rot/scale = INTERPOLATE (SKIP on a cut frame, 3.4), skew = SKIP,
               persp = AUTO, vert = SKIP, tile = AUTO, G_EX_ORDER_LINEAR,
               G_EX_EDIT_NONE, aspect AUTO, tc SKIP, lookat AUTO)     // 16 bytes
```

- **NOPUSH replace.** It replaces the base group, which `clearExtended` resets for each
  DL, so no pop is needed. At the end of the run, emit one NOPUSH group with
  `G_EX_ID_AUTO`. Then anything drawn later in the task goes back to today's behaviour.
- **ORDER_LINEAR** is needed: only ordered groups enter `idMap`.
- **Id = `node ^ (dl * 0x9E3779B9)`**, where `dl` is `node+0x34`, or `node+0x30` when
  there is no DL. Remap 0 and 0xFFFFFFFF, which are IGNORE and AUTO. The node pointer
  alone repeats when a pool slot is reused for another object. Folding in the model DL
  makes a reused slot a new object, so it snaps instead of lerping from the old one.
  This covers #6's "new objects" idea.
- **Projection.** Wrap the existing camera hand-off in
  `gEXMatrixGroup(cameraShotId, ..., G_MTX_PROJECTION)`. The shot id changes on a
  camera cut (3.4).
- **gEXEnable.** Today `wcw_rt_emit_camera` skips gEXEnable until the first camera
  exists. The emitter must emit gEXEnable itself when the camera block is missing,
  because menus and boot have no camera.

### 3.3 The arena budget (the risk the decode found)

The emitters never bounds-check. A tag costs 16 bytes per node:

| | Now | Tagged | Tagged, F3DEX2 `DB0C` dropped |
|---|---|---|---|
| WT / VPW64 | 48 | 64 | — (F3DEX keeps all 4 MOVEMEMs) |
| Revenge | 32 | 48 | 40 |
| WM2000 / NM / VPW2 | 48 | 64 | 56 |

The task open and close functions also use the arena. Their worst case is the
screen-clear path, an 80-command fill loop: about 94 commands (about 750 bytes) per
function that has it.
- WM2000, No Mercy and VPW2 clear in the open only. Their close emits E9 + DF (16 bytes).
- WT, VPW64 and Revenge have the fill loop in both open and close.

Approximate room per task, in nodes with one vertex chunk:

| | Untagged | Tagged, `DB0C` dropped | Tagged |
|---|---|---|---|
| No Mercy | 233 | 200 | 175 |
| VPW2 | 217 | 186 | 163 |
| WM2000 (tightest) | 200 | 172 | 150 |
| WT / VPW64 | 310 | — | 232 |
| Revenge | 465 | 372 | 310 |

In-match No Mercy runs about 130 nodes per frame, spread over several tasks, so normal
frames fit. Worst-case frames (entrances, replays, a crowded ring) are not measured.

**Design: never grow the arena. Measure first, then fit the run.** Before emitting a
run, `wcw_gex_emit` walks it once and counts nodes, vertex chunks and colour pairs, so
it knows the exact byte cost. It compares that with
`arena[idx] + DLarea - *dlp - closeReserve`:

- `closeReserve` is the close function's worst-case output: 16 bytes on WM2000, No
  Mercy and VPW2. Use 0x400 on WT, VPW64 and Revenge, whose close can run the fill loop.
- If the tagged run doesn't fit, emit it untagged, exactly as the game would. rt64 then
  falls back to today's slot pairing for that task, and a counter logs it.

Implementation step 1 is a count-only mode, `WCW_GEX_LOG=1`: emit nothing new and log
the bytes per task. Soak it before turning tags on. Growing the arenas (the heap blocks
that sit below No Mercy's bone pools) stays the last resort.

### 3.4 Camera cuts

The camera latch hook already sees every camera (`wcw_rt_latch_camera_multiply`). A cut
is a big eye or direction jump between consecutive frames' latched cameras. The
threshold goes in `WcwLogic` with a unit test, fed from the latched V matrices. On a
cut:

- `cameraShotId++`, so the projection group doesn't map and the camera snaps;
- every node group that frame uses SKIP components, so the whole frame snaps together.

Today the camera lerps between two unrelated shots for one generated frame at 60 fps
(three at 120). This is the camera-cut smear.

Open: find the game's shot director (the code that picks a new camera shot) to replace
the threshold with a real cut signal. It isn't decoded in any game yet.

### 3.5 Run marker (#6 causes 1-2)

The emitter's caller already knows two things:
- the task's index in the frame: a counter reset when `startIdx == 0`;
- whether it is the frame's last scene task: `nextIdx >= count`, the flag the close
  function gets.

Emit a wcw-only extended command in each task, `G_EX_WCW_RUNINFO(index, last)`: 8 bytes
with a new opcode above `G_EX_MAX`, in the fork's `[wcw]` range. rt64 then publishes a
run when its last task arrives, not when the expected count is reached. This is a fork
change in `WcwLogic` with tests, and a family-wide lib change (the six-game rule
applies). It needs no matrix groups, so it can ship first.

Verify first, with `WCW_RUN_LOG`: that every gfx task of a frame comes from this loop.
Menus and fades may submit tasks from somewhere else.

## 4. What it would fix

**#6 cause 1 (short multi-layer run)**
- Fixed today by `publishShortRun`, which is a heuristic.
- With tags: exact, through the run marker (3.5).
- Confidence: high.

**#6 cause 2 (run longer than expected, published early)**
- Open today.
- With tags: fixed by the run marker. rt64 knows the last task when it arrives, with no
  added latency.
- Confidence: high, if the "all tasks come from this loop" check passes.

**#6 cause 3 (dim/bright generated frames in complete runs)**
- Open today: a timing-dependent target-set/present race.
- With tags: **not fixed.** It is a renderer race, and game tags don't touch it.
- Confidence: high that it's unaffected.

**Camera-cut smearing**
- Today: the camera lerps across the cut, and the per-pair 30% rule only covers objects.
- With tags: fixed by the shot id plus SKIP on cut frames (3.4).
- Confidence: medium. It depends on the cut threshold until the shot director is
  decoded.

**Slot reuse and new objects lerping from a stale matrix**
- Today: possible, because the address id ignores what the slot now holds.
- With tags: fixed by the node^DL id.
- Confidence: high.

**Mispairs left by the address heuristics**
- Today: rare after slot pairing.
- With tags: removed, since ids are facts.
- Confidence: high, but the visible gain is small.

**#8 HUD Placement**
- Today: rt64#10 groups touching 2D draws into elements and anchors them by position.
  Known risk: world effects drawn as screen-space 2D get anchored too.
- With tags: affine (screen-space) forced matrices come only from this emitter, so each
  HUD piece made of triangles is a node. The emitter can tag it and anchor it per node:
  `gEXSetViewportAlign` / `gEXSetRectAlign` before its DL and a reset after (+32 bytes
  per HUD node). The anchor comes from the node's screen bounds, read from its vertices
  on the host. The per-game exception table becomes a per-layer-key rule.
- Confidence: medium. Texrect HUD drawn inside model DLs still needs rect align, and
  which layer keys are HUD must be mapped per game.

**Interpolation quality outside the cases above**
- Unchanged. Pairing already works there (section 2).

Bottom line: the matrix tags turn rt64's identity guesses into facts and fix the
camera-cut smear and slot reuse. The flicker the owner sees comes from causes 2 and 3;
the run marker, the cheapest part of this design, fixes cause 2, and nothing here fixes
cause 3.

## 5. Suggested order

1. Run marker (3.5): lib PR plus one hand-edit per game. Fixes #6 cause 2. Smallest
   change, biggest flicker win.
2. `WCW_GEX_LOG` count-only emitter in No Mercy: measure arena headroom over a soak
   (attract + a full match + an entrance).
3. Node tags plus the fit check in No Mercy. A/B with `WCW_GEX=0`. aki-shot flicker and
   cut-frame crops for evidence.
4. Camera shot id and cut snap (`WcwLogic` threshold, unit test).
5. Port to the other five. Per pair: VPW2 from WM2000, VPW64 from WT, Revenge from No
   Mercy (minus colours, plus the parity list).
6. HUD per-node anchoring (#8 follow-up), after rt64#10 lands.
