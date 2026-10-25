# Native helper: `speexenc_raw`

`speexenc_raw` turns mono 16 bit PCM into the **raw Speex frames** that the
ETS100 server expects next to the mp3 of every answer (the `spx` attachment).

## Why a helper is needed

ffmpeg shipped the `libspeex` encoder up to version 5.x and removed it in
ffmpeg 6.0. Every current ffmpeg - including the static build FastETS bundles -
can no longer produce Speex, so every submission would fail.

The helper is a ~150 line command line front end for
[libspeex](https://github.com/xiph/speex) (BSD licensed, see
`LICENSE.libspeex`) that is statically linked against it.

## Byte compatibility

The helper mirrors ffmpeg's `libspeex` encoder exactly:

| aspect            | ffmpeg `libspeex` encoder      | `speexenc_raw`              |
|-------------------|--------------------------------|-----------------------------|
| encoder input     | `AV_SAMPLE_FMT_S16`            | mono S16LE PCM              |
| encoder call      | `speex_encode_int()`           | `speex_encode_int()`        |
| quality           | `cbr_quality` = 8              | `--quality 8`               |
| complexity        | 3                              | `--complexity 3`            |
| VBR               | off (constant bit rate)        | `--vbr 0`                   |
| samples per frame | 320 (16 kHz wideband)          | 320                         |
| last frame        | zero padded                    | zero padded                 |
| frame extraction  | `speex_bits_write()` (no rewind)| same                        |

The produced frames were verified to be **byte identical** to the ones
ffmpeg 5.x produces for the same input: a 17 second recording (852 frames,
59640 bytes) hashes to the same SHA-256 as the frames ffmpeg 5.1's
`libspeex` encoder writes into its Ogg container.  `tests/selftest.py`
repeats that comparison (`test_speex_matches_ffmpeg`) whenever an ffmpeg that
still ships the `libspeex` encoder is installed.

Two details matter for that:

* the tail of the input is zero padded into one more frame, exactly like
  ffmpeg's audio FIFO does, instead of being dropped;
* libspeex is compiled **without auto vectorisation**
  (`-fno-vectorize -fno-slp-vectorize`).  At `-O2` clang vectorises some of
  libspeex's floating point loops, which changes the rounding and therefore
  the encoded bits; ffmpeg's own build does not vectorise them.

The comparison was made on macOS arm64.  On x86_64 the fused multiply-add
instructions make a few of the quantiser decisions differ, so the frames are
equivalent but not bit identical there - the stream stays a valid Speex
stream of the same quality either way.

## Building

```bash
./packaging/native/build_speex.sh            # -> fastets/bin/speexenc_raw
SPEEX_TARBALL=/path/to/speex.tar.gz ./packaging/native/build_speex.sh  # offline
```

The script downloads libspeex 1.2.1 (pinned to a commit, checksum verified),
compiles it and links the helper statically. `speexenc_raw --selftest` encodes
and decodes a sine wave and reports the signal to noise ratio; the build fails
if that check does not pass.
