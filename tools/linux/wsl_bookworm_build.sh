#!/usr/bin/env bash
# Build the Linux / Steam Deck package from Windows via WSL, inside a Debian 12 (bookworm) chroot.
# bookworm = glibc 2.36, old enough for SteamOS 3.x; the WSL Debian image itself may be newer (trixie).
#
# From Windows:  wsl -d Debian -u root -- bash /mnt/c/.../tools/linux/wsl_bookworm_build.sh
# Output:        /srv/bookworm/root/nomercy-linux/dist/NoMercyRecompiled-linux.tar.gz
# Options:       STAGE=setup|build|all (default all), JOBS=<n>, KEEP_GOING=1, WORK=<dir in the chroot>, INCLUDE_ROM=1
set -euo pipefail
R=/srv/bookworm
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STAGE="${STAGE:-all}"

if [ ! -x "$R/bin/bash" ]; then
    apt-get update -qq && apt-get install -y -qq debootstrap
    debootstrap --variant=minbase bookworm "$R" http://deb.debian.org/debian
fi
for m in proc sys dev dev/pts; do mountpoint -q "$R/$m" || mount --bind "/$m" "$R/$m"; done
cp -L /etc/resolv.conf "$R/etc/resolv.conf"
# Expose the Windows repo inside the chroot at the same path.
mkdir -p "$R/src-win"
mountpoint -q "$R/src-win" && umount "$R/src-win"
mount --bind "$SRC" "$R/src-win"

if [ "$STAGE" = setup ] || [ "$STAGE" = all ]; then
    chroot "$R" /bin/bash -c '
        set -e
        apt-get update -qq
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq clang-16 lld-16 cmake ninja-build pkg-config \
            make git ca-certificates python3 file zip libsdl2-dev libgtk-3-dev libx11-dev libxrandr-dev \
            libfreetype-dev libvulkan-dev patchelf
        for t in clang clang++ ld.lld; do ln -sf /usr/bin/$t-16 /usr/local/bin/$t; done
        clang --version | head -1; cmake --version | head -1; ldd --version | head -1'
fi

if [ "$STAGE" = build ] || [ "$STAGE" = all ]; then
    chroot "$R" /bin/bash -c "cd /root && SKIP_DEPS=1 KEEP_GOING=${KEEP_GOING:-} INCLUDE_ROM=${INCLUDE_ROM:-0} JOBS=${JOBS:-} WORK=${WORK:-/root/nomercy-linux} bash /src-win/tools/linux/build_linux.sh"
fi
