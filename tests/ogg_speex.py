"""Minimal Ogg Speex muxer - used by the test suite to cross-check FastETS'
own Speex encoder against an independent decoder (ffmpeg).

The ETS100 server expects the "spx" attachment as the *raw* Speex frames.  To
prove that the frames FastETS produces are conformant, the tests wrap them in
a proper Ogg Speex stream and let ffmpeg decode them; the decoded audio is
compared with the original.
"""

from __future__ import annotations

import struct
from typing import List

_CRC_TABLE = []


def _crc_table() -> List[int]:
    global _CRC_TABLE
    if not _CRC_TABLE:
        for index in range(256):
            value = index << 24
            for _ in range(8):
                if value & 0x80000000:
                    value = ((value << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
                else:
                    value = (value << 1) & 0xFFFFFFFF
            _CRC_TABLE.append(value)
    return _CRC_TABLE


def ogg_crc(data: bytes) -> int:
    """The non reflected CRC-32 that Ogg uses."""
    table = _crc_table()
    crc = 0
    for byte in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ table[((crc >> 24) & 0xFF) ^ byte]
    return crc


def _lacing(sizes: List[int]) -> bytes:
    """Build the Ogg segment table for a list of packet sizes."""
    segments = bytearray()
    for size in sizes:
        while size >= 255:
            segments.append(255)
            size -= 255
        segments.append(size)
    return bytes(segments)


def _page(payload: bytes, serial: int, sequence: int, granule: int, flags: int) -> bytes:
    segments = _lacing([len(payload)])
    header = bytearray()
    header += b"OggS"
    header.append(0)  # version
    header.append(flags)
    header += struct.pack("<q", granule)
    header += struct.pack("<I", serial)
    header += struct.pack("<I", sequence)
    header += b"\x00\x00\x00\x00"  # CRC placeholder
    header.append(len(segments))
    header += segments
    page = bytes(header) + payload
    crc = ogg_crc(page)
    return page[:22] + struct.pack("<I", crc) + page[26:]


def speex_header_packet(rate: int = 16000, mode: int = 1, frame_size: int = 320) -> bytes:
    """The 80 byte Speex header packet (identical to what ffmpeg writes)."""
    return (
        b"Speex   "
        + b"1.2.1".ljust(20, b"\x00")
        + struct.pack(
            "<13i",
            1,  # speex_version_id
            80,  # header_size
            rate,
            mode,
            4,  # mode_bitstream_version
            1,  # nb_channels
            -1,  # bitrate
            frame_size,
            0,  # vbr
            1,  # frames_per_packet
            0,  # extra_headers
            0,
            0,
        )
    )


def comment_packet(vendor: str = "FastETS") -> bytes:
    raw = vendor.encode("utf-8")
    return struct.pack("<i", len(raw)) + raw + struct.pack("<i", 0)


def build_ogg_speex(frames: List[bytes], rate: int = 16000, frame_size: int = 320) -> bytes:
    """Wrap raw Speex frames into an Ogg Speek stream."""
    serial = 0x46455354
    out = bytearray()
    out += _page(speex_header_packet(rate, 1, frame_size), serial, 0, 0, 0x02)
    out += _page(comment_packet(), serial, 1, 0, 0x00)

    sequence = 2
    samples = 0
    # ffmpeg packs several frames into one page; the page may carry at most
    # 255 segments.
    chunk: List[bytes] = []
    for frame in frames:
        chunk.append(frame)
        if len(chunk) == 255:
            samples += frame_size * len(chunk)
            out += _page(b"".join(chunk), serial, sequence, samples, 0x00)
            sequence += 1
            chunk = []
    if chunk:
        samples += frame_size * len(chunk)
        out += _page(b"".join(chunk), serial, sequence, samples, 0x00)
        sequence += 1
    out += _page(b"", serial, sequence, samples, 0x04)
    return bytes(out)
