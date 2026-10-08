#!/usr/bin/env bash
# WWF No Mercy: Recompiled -- Linux build + package (2026-09-26).
#
# Run from the Linux side of a dual-boot PC, pointing at this repo on the Windows drive:
#   bash /path/to/WWFNoMercyRecomp/tools/linux/build_linux.sh
#
# What it does:
#   1. installs build dependencies (apt / dnf / pacman / zypper; asks for sudo)
#   2. copies the source to ~/nomercy-linux/src (NTFS mounts are often read-only or lose the
#      execute bit that the vendored shader compiler needs; git history and build dirs are skipped)
#   3. configures + builds with clang/Ninja in ~/nomercy-linux/build
#   4. packages a self-contained folder + tarball in ~/nomercy-linux/dist (bundles libSDL2 so the
#      same folder can be copied to a Steam Deck)
#
# Options (environment): SKIP_DEPS=1  KEEP_GOING=1 (report all compile errors)  JOBS=<n>  WORK=<dir, default ~/nomercy-linux>
#                         INCLUDE_ROM=1 (personal use only: copy ./nomercy.z64 into the package; never share such a package)
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="${WORK:-$HOME/nomercy-linux}"
JOBS="${JOBS:-$(nproc)}"
say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

[ -f "$SRC/CMakeLists.txt" ] && [ -d "$SRC/RecompiledFuncs" ] || die "run this from inside the WWFNoMercyRecomp repo (couldn't find CMakeLists.txt + RecompiledFuncs under $SRC)"

# ---------------------------------------------------------------- 1. dependencies
if [ "${SKIP_DEPS:-0}" != "1" ]; then
    say "Installing build dependencies"
    if command -v apt-get >/dev/null; then
        sudo apt-get update
        sudo apt-get install -y clang lld cmake ninja-build pkg-config make \
            libsdl2-dev libgtk-3-dev libx11-dev libxrandr-dev libfreetype-dev libvulkan-dev patchelf
    elif command -v dnf >/dev/null; then
        sudo dnf install -y clang lld cmake ninja-build pkgconf-pkg-config make \
            SDL2-devel gtk3-devel libX11-devel libXrandr-devel freetype-devel vulkan-loader-devel patchelf
    elif command -v pacman >/dev/null; then
        sudo pacman -S --needed --noconfirm clang lld cmake ninja pkgconf make \
            sdl2 gtk3 libx11 libxrandr freetype2 vulkan-icd-loader vulkan-headers patchelf
    elif command -v zypper >/dev/null; then
        sudo zypper install -y clang lld cmake ninja pkg-config make \
            SDL2-devel gtk3-devel libX11-devel libXrandr-devel freetype2-devel vulkan-devel patchelf
    else
        die "unknown package manager -- install clang, cmake, ninja, SDL2/GTK3/X11/Xrandr/freetype/vulkan dev packages, then rerun with SKIP_DEPS=1"
    fi
fi

# ---------------------------------------------------------------- 2. copy source
say "Copying source to $WORK/src"
mkdir -p "$WORK/src"
tar -C "$SRC" \
    --exclude='./.git' --exclude='*/.git' --exclude='./build-*' --exclude='./state_snapshots' \
    --exclude='./disasm' --exclude='*.exe' --exclude='*.pdb' \
    -cf - . | tar -C "$WORK/src" -xf -
chmod +x "$WORK/src/lib/rt64/src/contrib/dxc/bin/"*/dxc-linux 2>/dev/null || true

# ---------------------------------------------------------------- 3. build
say "Configuring (clang, Release)"
cmake -S "$WORK/src" -B "$WORK/build" -G Ninja \
    -DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++ \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_EXE_LINKER_FLAGS="-fuse-ld=lld"

say "Building NoMercyRecompiled with $JOBS jobs (the recompiled game is ~38 MB of C -- this takes a while)"
cmake --build "$WORK/build" --target NoMercyRecompiled -j "$JOBS" ${KEEP_GOING:+-- -k 0}

# ---------------------------------------------------------------- 4. package
DIST="$WORK/dist/NoMercyRecompiled-linux"
say "Packaging into $DIST"
rm -rf "$DIST"; mkdir -p "$DIST/lib/sdl2-fallback"
cp "$WORK/build/NoMercyRecompiled" "$DIST/"
cp -r "$WORK/build/assets" "$DIST/"
cp "$WORK/src/recompcontrollerdb.txt" "$DIST/"
# The ROM is never packaged unless asked for (INCLUDE_ROM=1, personal use only).
if [ "${INCLUDE_ROM:-0}" = "1" ] && [ -f "$SRC/nomercy.z64" ]; then cp "$SRC/nomercy.z64" "$DIST/"; fi
# Bundle SDL2 as a FALLBACK only: this Debian build hard-links libsamplerate/libdecor/libXss etc., which
# a Deck may lack, so play.sh uses the system SDL2 (SteamOS/Bazzite ship one) whenever it exists.
sdl="$(ldd "$DIST/NoMercyRecompiled" | awk '/libSDL2-2\.0\.so/{print $3}')"
[ -n "$sdl" ] && cp -L "$sdl" "$DIST/lib/sdl2-fallback/"

cat > "$DIST/play.sh" <<'EOF'
#!/usr/bin/env bash
# Play normally. The 60Hz settings are built in. Put your ROM next to this script as nomercy.z64
# to skip the ROM picker, or choose it in the launcher.
cd "$(dirname "$0")"
# System SDL2 if present, else the bundled copy.
/sbin/ldconfig -p 2>/dev/null | grep -q "libSDL2-2.0.so.0 " || export LD_LIBRARY_PATH="$PWD/lib/sdl2-fallback${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
[ -f nomercy.z64 ] && export WCW_AUTOBOOT="$PWD/nomercy.z64"
exec ./NoMercyRecompiled "$@"
EOF
chmod +x "$DIST/play.sh" "$DIST/NoMercyRecompiled"
cat > "$DIST/README_STEAM_DECK.txt" <<'EOF'
WWF No Mercy: Recompiled (60Hz Edition) -- Steam Deck / Linux

You need your own WWF No Mercy (USA) ROM. It is not included.

SETUP (in Desktop Mode)
1. Extract this zip somewhere permanent, e.g. ~/Games/NoMercyRecompiled-linux
2. Copy your ROM into that folder, next to play.sh, and rename it to: nomercy.z64
   (the game then boots straight in; no ROM picker needed)
3. Open Steam -> Games -> "Add a Non-Steam Game to My Library..." -> Browse.
   Set the file type filter to "All Files", select play.sh and click "Add Selected Programs".
4. Do NOT start the game from Desktop Mode. In Desktop Mode, Steam turns the Deck's
   controls into a keyboard and mouse, and the game will clash with that.

PLAY (in Gaming Mode)
5. Switch to Gaming Mode (desktop shortcut "Return to Gaming Mode").
6. Find play.sh in your Library (under Non-Steam) and launch it.
   You can rename it and give it artwork in its Steam properties.
7. If the buttons don't respond correctly: Steam button -> Controller Settings ->
   choose the "Gamepad" layout template.

CONTROLS (Xbox-style layout)
A = A, X = B, B = C-Down, Y = C-Up, L1/R1 = C-Left/C-Right, L2/R2 = L/R, L3 (left stick click) = Z,
Menu = Start, D-pad or left stick = move, right stick = N64 analog stick, View = settings menu.
Tip: in Controller Settings, bind a back grip to "Left Stick Click" for an easier Z.

60Hz settings are built in. If the game won't start, run ./play.sh from Konsole in Desktop Mode
and note the last lines it prints.
EOF

commit="$(cat "$SRC/.git/HEAD" 2>/dev/null || true)"
{
    echo "WWF No Mercy: Recompiled -- Linux build"
    echo "Built $(date -u +%Y-%m-%dT%H:%MZ) on $(. /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-unknown}")"
    echo "glibc: $(ldd --version | head -1)"
    echo
    echo "Libraries the binary needs (a Steam Deck needs these present; SDL2 comes from the system, with lib/sdl2-fallback used only if it has none):"
    ldd "$DIST/NoMercyRecompiled" | awk '{print "  " $1}'
} > "$DIST/BUILD_INFO.txt"

tar -C "$WORK/dist" -czf "$WORK/dist/NoMercyRecompiled-linux.tar.gz" NoMercyRecompiled-linux
# Zip for easy transfer to a Deck (Info-ZIP keeps the execute bits).
if command -v zip >/dev/null; then
    rm -f "$WORK/dist/NoMercyRecompiled-SteamDeck.zip"
    (cd "$WORK/dist" && zip -q -r -9 NoMercyRecompiled-SteamDeck.zip NoMercyRecompiled-linux)
fi
say "Done."
echo "  Run here:        $DIST/play.sh"
echo "  Steam Deck:      copy $WORK/dist/NoMercyRecompiled-SteamDeck.zip across, extract, see README_STEAM_DECK.txt"
echo "                   (add play.sh to Steam as a non-Steam game and ALWAYS launch it from Steam)"
echo "  Build details:   $DIST/BUILD_INFO.txt"
