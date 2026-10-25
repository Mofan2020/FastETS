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
| frame extraction  | `speex_bits_write()` (no rewind)| same                        |

The produced frames were verified to be **byte identical** to the ones
ffmpeg 5.x produces for the same input.

## Building

```bash
./packaging/native/build_speex.sh            # -> fastets/bin/speexenc_raw
SPEEX_TARBALL=/path/to/speex.tar.gz ./packaging/native/build_speex.sh  # offline
```

The script downloads libspeex 1.2.1 (pinned to a commit, checksum verified),
compiles it and links the helper statically. `speexenc_raw --selftest` encodes
and decodes a sine wave and reports the signal to noise ratio; the build fails
if that check does not pass.
