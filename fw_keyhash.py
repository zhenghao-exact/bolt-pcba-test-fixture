"""Extract the MCUboot signing-key hash (IMAGE_TLV_KEYHASH) from a merged .hex.

Used by the fixture to verify, before flashing, that the staged production image
is signed with the expected key. Every board through this fixture is a remake
board that must run the custom-key firmware (FW-1049); flashing a default-key
image would ship a board that cannot accept the fleet's key-matched OTA images.
The keyhash is SHA256 of the signing public key, so it is a stable per-key
fingerprint (custom key vs the in-repo default) without needing the key itself.
"""
import struct

IMAGE_MAGIC = 0x96F3B83D          # MCUboot image header magic
TLV_INFO_MAGIC = 0x6907           # unprotected TLV area
TLV_PROT_INFO_MAGIC = 0x6908      # protected TLV area
IMAGE_TLV_KEYHASH = 0x01          # SHA256 of the signing public key


def _parse_ihex(path):
    """Parse an Intel HEX file into a {address: byte} sparse map."""
    mem = {}
    base = 0
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line[0] != ":":
                continue
            raw = bytes.fromhex(line[1:])
            count = raw[0]
            addr = (raw[1] << 8) | raw[2]
            rtype = raw[3]
            data = raw[4:4 + count]
            if rtype == 0x00:               # data
                for i, b in enumerate(data):
                    mem[base + addr + i] = b
            elif rtype == 0x04:             # extended linear address
                base = ((data[0] << 8) | data[1]) << 16
            elif rtype == 0x02:             # extended segment address
                base = ((data[0] << 8) | data[1]) << 4
            elif rtype == 0x01:             # EOF
                break
    return mem


def _read(mem, addr, n):
    out = bytearray(n)
    for i in range(n):
        b = mem.get(addr + i)
        if b is None:
            return None
        out[i] = b
    return bytes(out)


def _u16(mem, addr):
    b = _read(mem, addr, 2)
    return struct.unpack("<H", b)[0] if b else None


def _u32(mem, addr):
    b = _read(mem, addr, 4)
    return struct.unpack("<I", b)[0] if b else None


def _keyhash_at(mem, off):
    """Parse an MCUboot image header at off; return its KEYHASH hex or None."""
    if _u32(mem, off) != IMAGE_MAGIC:
        return None
    hdr_size = _u16(mem, off + 8)
    prot_tlv_size = _u16(mem, off + 10)
    img_size = _u32(mem, off + 12)
    if hdr_size is None or img_size is None:
        return None
    tlv_base = off + hdr_size + img_size
    main = tlv_base
    if prot_tlv_size:                        # skip the protected TLV area
        if _u16(mem, tlv_base) != TLV_PROT_INFO_MAGIC:
            return None
        main = tlv_base + prot_tlv_size
    if _u16(mem, main) != TLV_INFO_MAGIC:
        return None
    total = _u16(mem, main + 2)
    if not total:
        return None
    pos, end = main + 4, main + total
    while pos + 4 <= end:
        ttype = _u16(mem, pos)
        tlen = _u16(mem, pos + 2)
        if ttype is None or tlen is None:
            return None
        if ttype == IMAGE_TLV_KEYHASH:
            val = _read(mem, pos + 4, tlen)
            return val.hex().upper() if val else None
        pos += 4 + tlen
    return None


def extract_keyhash(path):
    """Return the uppercase-hex IMAGE_TLV_KEYHASH of the app image in a merged
    .hex, or None if it can't be found (unparseable / unsigned image)."""
    mem = _parse_ihex(path)
    # Scan 4-byte-aligned offsets for the image magic; return the first that
    # yields a valid KEYHASH TLV (the primary-slot app image; the bootloader
    # region has no MCUboot header).
    for a in sorted(mem.keys()):
        if a % 4 == 0 and _u32(mem, a) == IMAGE_MAGIC:
            kh = _keyhash_at(mem, a)
            if kh:
                return kh
    return None


def verify_hex_keyhash(path, expected):
    """Return (ok, actual_keyhash). ok is True only when the merged .hex is
    parseable AND its signing-key hash equals `expected` (case-insensitive).
    actual_keyhash is None when the file is missing/unparseable."""
    try:
        actual = extract_keyhash(path)
    except (OSError, ValueError):
        return False, None
    if actual is None:
        return False, None
    return actual.upper() == expected.upper(), actual
