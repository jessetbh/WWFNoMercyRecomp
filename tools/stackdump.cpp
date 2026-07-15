// stackdump.cpp — dump all thread stacks of a live process by PID using dbghelp.
// Usage: stackdump.exe <pid>
#define WIN32_LEAN_AND_MEAN
#include <Windows.h>
#include <DbgHelp.h>
#include <TlHelp32.h>
#include <cstdio>
#include <cstdlib>

#pragma comment(lib, "dbghelp.lib")

int main(int argc, char** argv) {
    if (argc < 2) { fprintf(stderr, "usage: stackdump <pid>\n"); return 1; }
    DWORD pid = (DWORD)strtoul(argv[1], nullptr, 10);

    HANDLE hProc = OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, FALSE, pid);
    if (!hProc) { fprintf(stderr, "OpenProcess failed: %lu\n", GetLastError()); return 1; }

    SymSetOptions(SYMOPT_LOAD_LINES | SYMOPT_DEFERRED_LOADS | SYMOPT_UNDNAME);
    if (!SymInitialize(hProc, nullptr, TRUE)) {
        fprintf(stderr, "SymInitialize failed: %lu\n", GetLastError()); return 1;
    }

    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0);
    if (snap == INVALID_HANDLE_VALUE) { fprintf(stderr, "snapshot failed\n"); return 1; }

    THREADENTRY32 te{}; te.dwSize = sizeof(te);
    for (BOOL ok = Thread32First(snap, &te); ok; ok = Thread32Next(snap, &te)) {
        if (te.th32OwnerProcessID != pid) continue;
        HANDLE hThread = OpenThread(THREAD_SUSPEND_RESUME | THREAD_GET_CONTEXT | THREAD_QUERY_INFORMATION, FALSE, te.th32ThreadID);
        if (!hThread) { printf("tid %lu: OpenThread failed\n", te.th32ThreadID); continue; }
        if (SuspendThread(hThread) == (DWORD)-1) { printf("tid %lu: suspend failed\n", te.th32ThreadID); CloseHandle(hThread); continue; }

        CONTEXT ctx{}; ctx.ContextFlags = CONTEXT_FULL;
        if (GetThreadContext(hThread, &ctx)) {
            printf("\n=== tid %lu ===\n", te.th32ThreadID);
            STACKFRAME64 sf{};
            sf.AddrPC.Offset = ctx.Rip;    sf.AddrPC.Mode = AddrModeFlat;
            sf.AddrFrame.Offset = ctx.Rbp; sf.AddrFrame.Mode = AddrModeFlat;
            sf.AddrStack.Offset = ctx.Rsp; sf.AddrStack.Mode = AddrModeFlat;
            for (int depth = 0; depth < 40; depth++) {
                if (!StackWalk64(IMAGE_FILE_MACHINE_AMD64, hProc, hThread, &sf, &ctx,
                                 nullptr, SymFunctionTableAccess64, SymGetModuleBase64, nullptr)) break;
                DWORD64 addr = sf.AddrPC.Offset;
                if (!addr) break;
                char symBuf[sizeof(SYMBOL_INFO) + 256]{};
                SYMBOL_INFO* sym = (SYMBOL_INFO*)symBuf;
                sym->SizeOfStruct = sizeof(SYMBOL_INFO);
                sym->MaxNameLen = 255;
                DWORD64 disp = 0;
                DWORD64 modBase = SymGetModuleBase64(hProc, addr);
                char modName[MAX_PATH] = "?";
                if (modBase) {
                    IMAGEHLP_MODULE64 mi{}; mi.SizeOfStruct = sizeof(mi);
                    if (SymGetModuleInfo64(hProc, modBase, &mi)) {
                        strncpy_s(modName, mi.ModuleName, _TRUNCATE);
                    }
                }
                if (SymFromAddr(hProc, addr, &disp, sym)) {
                    printf("  %02d %s!%s+0x%llx\n", depth, modName, sym->Name, (unsigned long long)disp);
                } else {
                    printf("  %02d %s+0x%llx (0x%llx)\n", depth, modName,
                           (unsigned long long)(modBase ? addr - modBase : addr), (unsigned long long)addr);
                }
            }
        }
        ResumeThread(hThread);
        CloseHandle(hThread);
    }
    CloseHandle(snap);
    SymCleanup(hProc);
    CloseHandle(hProc);
    return 0;
}
