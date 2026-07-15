// memdump.cpp — locate recomp rdram (512MB region) in a live process and dump words.
// Usage: memdump.exe <pid> <vram_hex> <word_count> [<vram_hex> <word_count> ...]
#define WIN32_LEAN_AND_MEAN
#include <Windows.h>
#include <cstdio>
#include <cstdlib>
#include <cstdint>

int main(int argc, char** argv) {
    if (argc < 4) { fprintf(stderr, "usage: memdump <pid> <vram_hex> <words> ...\n"); return 1; }
    DWORD pid = (DWORD)strtoul(argv[1], nullptr, 10);
    HANDLE hProc = OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, FALSE, pid);
    if (!hProc) { fprintf(stderr, "OpenProcess failed: %lu\n", GetLastError()); return 1; }

    // Find the rdram allocation: a committed private region of size >= 0x1F000000.
    uint8_t* base = nullptr;
    MEMORY_BASIC_INFORMATION mbi;
    for (uint8_t* p = nullptr; VirtualQueryEx(hProc, p, &mbi, sizeof(mbi)); p = (uint8_t*)mbi.BaseAddress + mbi.RegionSize) {
        if (mbi.State == MEM_COMMIT && mbi.Type == MEM_PRIVATE && mbi.RegionSize >= 0x1F000000ULL) {
            // candidate; verify by reading the N64 entrypoint area (vram 0x80000400 = phys 0x400):
            // first instruction of the AKI entry stub is a lui (0x3C..). Words are stored as
            // host-endian u32 (byteswapped per word), so check plausibility loosely.
            uint32_t w = 0; SIZE_T got = 0;
            uint8_t* regionBase = (uint8_t*)mbi.AllocationBase;
            if (ReadProcessMemory(hProc, regionBase + 0x400, &w, 4, &got) && got == 4 && w != 0) {
                base = regionBase;
                printf("rdram candidate at %p (region size 0x%llx), word@0x400=0x%08X\n",
                       regionBase, (unsigned long long)mbi.RegionSize, w);
                break;
            }
        }
    }
    if (!base) { fprintf(stderr, "rdram region not found\n"); return 1; }

    // scan mode: "scan <start_hex> <end_hex>" prints zero/nonzero run boundaries.
    if (argc >= 5 && strcmp(argv[2], "scan") == 0) {
        uint32_t start = (uint32_t)strtoul(argv[3], nullptr, 16) & 0x1FFFFFFF;
        uint32_t end = (uint32_t)strtoul(argv[4], nullptr, 16) & 0x1FFFFFFF;
        bool inZero = false; uint32_t runStart = start;
        for (uint32_t off = start; off < end; off += 4) {
            uint32_t w = 0xDEAD; SIZE_T got = 0;
            ReadProcessMemory(hProc, base + off, &w, 4, &got);
            bool z = (w == 0);
            if (off == start) { inZero = z; runStart = off; continue; }
            if (z != inZero) {
                printf("  0x%08X..0x%08X %s (0x%X bytes)\n", 0x80000000 + runStart, 0x80000000 + off,
                       inZero ? "ZERO" : "data", off - runStart);
                inZero = z; runStart = off;
            }
        }
        printf("  0x%08X..0x%08X %s (0x%X bytes)\n", 0x80000000 + runStart, 0x80000000 + end,
               inZero ? "ZERO" : "data", end - runStart);
        CloseHandle(hProc);
        return 0;
    }

    for (int i = 2; i + 1 < argc; i += 2) {
        uint32_t vram = (uint32_t)strtoul(argv[i], nullptr, 16);
        int count = atoi(argv[i + 1]);
        uint32_t phys = vram & 0x1FFFFFFF;
        printf("\n-- vram 0x%08X (phys 0x%08X) --\n", vram, phys);
        for (int k = 0; k < count; k++) {
            uint32_t w = 0; SIZE_T got = 0;
            if (!ReadProcessMemory(hProc, base + phys + 4ULL * k, &w, 4, &got) || got != 4) {
                printf("  0x%08X: <read failed>\n", vram + 4 * k);
                break;
            }
            printf("  0x%08X: 0x%08X\n", vram + 4 * k, w);
        }
    }
    CloseHandle(hProc);
    return 0;
}
