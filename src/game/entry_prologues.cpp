#include <cstring>

#include "recomp.h"
#include "librecomp/mods.hpp"

// [wcw fullconv] Entry prologues (aki-roadmap#76): the hand-edits tools/readd_hand_edits.py puts at the top of a
// recompiled function, as functions the runtime can call. When a conversion's ROM patch changes one of these functions
// (No Mercy Plus V3 and ECW Born to be Wired change func_80001E08), or a mod hooks it, the live recompiler replaces the
// static function; the replacement calls the prologue at its entry, so the 60 fps matrix capture and the RT camera
// latch keep working. Keep each one in step with its hand-edit (same calls, same order). A prologue only observes:
// the runtime restores the registers after it and it can't skip the function, so a hand-edit that returns early (the
// func_8011DCC4 char-select airbag) can't be one.

extern "C" void func_80001E08(uint8_t* rdram, recomp_context* ctx);

extern "C" void wcw_interp_process_multiply(uint32_t destAddr, uint32_t opBAddr, uint32_t srcAddr, const float* aMatrix16, const float* bMatrix16);
extern "C" void wcw_interp_invalidate_local_matrix(uint32_t destAddr);
extern "C" void wcw_rt_latch_camera_multiply(uint8_t* rdram, uint32_t view_vaddr, uint32_t proj_vaddr, uint32_t dest_vaddr);

// func_80001E08, the 4x4 multiply (a0 = A, a1 = B, a2 = out): readd_hand_edits.py WCW_MATRIX_HOOK, then the RT
// camera latch (rt_hand_edits).
static void prologue_80001E08(uint8_t* rdram, recomp_context* ctx) {
    const uint32_t dst = (uint32_t)ctx->r6;
    if ((uint32_t)ctx->r5 == 0x80062BA8u) {
        float a[16], b[16];
        for (int i = 0; i < 16; i++) {
            const uint32_t abits = (uint32_t)MEM_W(ctx->r4, i * 4);
            const uint32_t bbits = (uint32_t)MEM_W(ctx->r5, i * 4);
            memcpy(&a[i], &abits, sizeof(float));
            memcpy(&b[i], &bbits, sizeof(float));
        }
        wcw_interp_process_multiply(dst, (uint32_t)ctx->r5, (uint32_t)ctx->r4, a, b);
    }
    else {
        wcw_interp_invalidate_local_matrix(dst);
    }
    wcw_rt_latch_camera_multiply(rdram, (uint32_t)ctx->r4, (uint32_t)ctx->r5, (uint32_t)ctx->r6);
}

void wcw_register_entry_prologues() {
    static const recomp::mods::EntryPrologue prologues[] = {
        { func_80001E08, prologue_80001E08 },
    };
    recomp::mods::register_entry_prologues(prologues);
}
