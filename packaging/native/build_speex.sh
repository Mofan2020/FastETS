#!/usr/bin/env bash
#
# Build the ``speexenc_raw`` helper that FastETS uses for the ETS100 "spx"
# attachment.
#
# ffmpeg removed its ``libspeex`` encoder in 6.0, so a current ffmpeg cannot
# produce Speex any more.  This script compiles a tiny command line front end
# for libspeex (BSD licensed, https://github.com/xiph/speex) and links it
# statically so that the resulting binary has no runtime dependencies.
#
#   ./packaging/native/build_speex.sh [output-directory]
#
# The output is ``fastets/bin/speexenc_raw`` (``.exe`` on Windows).
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
OUT_DIR="${1:-$ROOT/fastets/bin}"

# libspeex 1.2.1 - the release ffmpeg's libspeex encoder is built on.  The
# archive is pinned to the commit so that the download cannot change.
SPEEX_COMMIT="5dceaaf3e23ee7fd17c80cb5f02a838fd6c18e01"
SPEEX_URL="https://github.com/xiph/speex/archive/${SPEEX_COMMIT}.tar.gz"
SPEEX_SHA256="21d479397e1d838e7ab9d74229f215c13c73aa8514fc68adfe959277b9c30a27"

if [ -n "${CC:-}" ]; then
    :
elif command -v cc >/dev/null 2>&1; then
    CC="cc"
elif command -v gcc >/dev/null 2>&1; then
    CC="gcc"
elif command -v clang >/dev/null 2>&1; then
    CC="clang"
elif [ -x /usr/bin/gcc ]; then
    CC=/usr/bin/gcc
else
    echo "[speex] no C compiler found (set CC)" >&2
    exit 1
fi

case "$(uname -s 2>/dev/null || echo Windows)" in
    MINGW*|MSYS*|CYGWIN*) EXE_SUFFIX=".exe" ;;
    *) EXE_SUFFIX="" ;;
esac

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

DOWNLOADED=""
if [ -n "${SPEEX_TARBALL:-}" ]; then
    # Offline build: use an already downloaded archive.
    cp "$SPEEX_TARBALL" "$WORK/speex.tar.gz"
    DOWNLOADED=1
fi
if [ -z "$DOWNLOADED" ] && command -v curl >/dev/null 2>&1; then
    if curl -fsSL --retry 3 -o "$WORK/speex.tar.gz" "$SPEEX_URL"; then
        DOWNLOADED=1
    fi
fi
if [ -z "$DOWNLOADED" ] && command -v wget >/dev/null 2>&1; then
    if wget -q -O "$WORK/speex.tar.gz" "$SPEEX_URL"; then
        DOWNLOADED=1
    fi
fi
if [ -z "$DOWNLOADED" ]; then
    # Last resort: Python ships its own certificate bundle.
    for candidate in python3 /usr/bin/python3 python; do
        if command -v "$candidate" >/dev/null 2>&1 || [ -x "$candidate" ]; then
            if "$candidate" "$HERE/fetch_speex.py" "$SPEEX_URL" "$WORK/speex.tar.gz"; then
                DOWNLOADED=1
                break
            fi
        fi
    done
fi
if [ -z "$DOWNLOADED" ]; then
    echo "[speex] could not download $SPEEX_URL" >&2
    exit 1
fi
echo "[speex] libspeex ${SPEEX_COMMIT} archive ready"

if command -v sha256sum >/dev/null 2>&1; then
    ACTUAL="$(sha256sum "$WORK/speex.tar.gz" | cut -d' ' -f1)"
elif command -v shasum >/dev/null 2>&1; then
    ACTUAL="$(shasum -a 256 "$WORK/speex.tar.gz" | cut -d' ' -f1)"
else
    ACTUAL=""
fi
if [ -n "$ACTUAL" ] && [ "$ACTUAL" != "$SPEEX_SHA256" ]; then
    echo "[speex] checksum mismatch" >&2
    echo "  expected $SPEEX_SHA256" >&2
    echo "  actual   $ACTUAL" >&2
    exit 1
fi

tar xzf "$WORK/speex.tar.gz" -C "$WORK"
SRC="$(find "$WORK" -maxdepth 1 -type d -name 'speex-*' | head -1)"
if [ -z "$SRC" ]; then
    echo "[speex] unexpected tarball layout" >&2
    exit 1
fi

mkdir -p "$OUT_DIR"
BUILD="$WORK/build"
mkdir -p "$BUILD"

# The same defines ffmpeg uses when it builds libspeex: floating point
# arithmetic and the KISS FFT backend.
CFLAGS="-O2 -fPIC -DFLOATING_POINT -DEXPORT= -DUSE_KISS_FFT"
OBJECTS=""

for source in "$SRC"/libspeex/*.c; do
    name="$(basename "$source")"
    case "$name" in
        testenc*) continue ;;
    esac
    # shellcheck disable=SC2086
    "$CC" $CFLAGS -I "$SRC/include" -I "$SRC/libspeex" -c "$source" -o "$BUILD/${name%.c}.o"
    OBJECTS="$OBJECTS $BUILD/${name%.c}.o"
done

# shellcheck disable=SC2086
"$CC" $CFLAGS -I "$SRC/include" -I "$SRC/libspeex" \
    -c "$HERE/speexenc_raw.c" -o "$BUILD/speexenc_raw.o"

TARGET="$OUT_DIR/speexenc_raw${EXE_SUFFIX}"
LINK_FLAGS="-O2"
case "$EXE_SUFFIX" in
    .exe) LINK_FLAGS="$LINK_FLAGS -static" ;;
esac
# shellcheck disable=SC2086
"$CC" $LINK_FLAGS -o "$TARGET" $OBJECTS "$BUILD/speexenc_raw.o" -lm

echo "[speex] built $TARGET"
"$TARGET" --selftest
echo "[speex] selftest passed"
