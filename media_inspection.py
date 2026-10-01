"""Deterministic, dependency-free structural decoding for generated still images.

Technical QA uses the decoded container metadata here instead of trusting
provider-declared MIME types or dimensions.
"""

import struct
import zlib


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
_PNG_BIT_DEPTHS = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
_ADAM7 = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))
_JPEG_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


class ImageDecodeError(ValueError):
    pass


def sniff_image_mime(data):
    if not isinstance(data, (bytes, bytearray)):
        return None
    if data.startswith(PNG_SIGNATURE):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _png_row_bytes(width, bit_depth, channels):
    return (width * bit_depth * channels + 7) // 8


def _decode_png(data):
    offset, header, idat, ended = 8, None, [], False
    while offset + 12 <= len(data):
        length = int.from_bytes(data[offset:offset + 4], "big")
        kind = data[offset + 4:offset + 8]
        chunk = data[offset + 8:offset + 8 + length]
        checksum = int.from_bytes(data[offset + 8 + length:offset + 12 + length], "big")
        if len(chunk) != length or (zlib.crc32(kind + chunk) & 0xFFFFFFFF) != checksum:
            raise ImageDecodeError("PNG chunk is truncated or corrupt.")
        if kind == b"IHDR":
            if length != 13:
                raise ImageDecodeError("PNG IHDR has an invalid length.")
            header = struct.unpack(">IIBBBBB", chunk)
        elif kind == b"IDAT":
            idat.append(chunk)
        elif kind == b"IEND":
            ended = True
            break
        offset += 12 + length
    if not header or not idat or not ended:
        raise ImageDecodeError("PNG is missing required chunks.")
    width, height, bit_depth, color_type, compression, filtering, interlace = header
    if not width or not height or color_type not in _PNG_CHANNELS or bit_depth not in _PNG_BIT_DEPTHS[color_type]:
        raise ImageDecodeError("PNG header declares an invalid image profile.")
    if compression != 0 or filtering != 0 or interlace not in (0, 1):
        raise ImageDecodeError("PNG header declares unsupported compression, filtering, or interlacing.")
    try:
        decoded = zlib.decompress(b"".join(idat))
    except zlib.error as error:
        raise ImageDecodeError("PNG pixel data cannot be decompressed.") from error
    channels = _PNG_CHANNELS[color_type]
    if interlace == 0:
        passes = ((width, height),)
    else:
        passes = tuple(
            ((width - x0 + dx - 1) // dx, (height - y0 + dy - 1) // dy) for x0, y0, dx, dy in _ADAM7
        )
    position = 0
    for pass_width, pass_height in passes:
        if not pass_width or not pass_height:
            continue
        stride = 1 + _png_row_bytes(pass_width, bit_depth, channels)
        for row in range(pass_height):
            if position + row * stride >= len(decoded) or decoded[position + row * stride] > 4:
                raise ImageDecodeError("PNG scanline uses an invalid filter or is truncated.")
        position += stride * pass_height
    if position != len(decoded):
        raise ImageDecodeError("PNG pixel payload has an invalid length.")
    return {"format": "PNG", "mime_type": "image/png", "width": width, "height": height,
            "bit_depth": bit_depth, "color_type": color_type, "interlaced": bool(interlace)}


def _decode_jpeg(data):
    if not data.endswith(b"\xff\xd9"):
        raise ImageDecodeError("JPEG is truncated: end-of-image marker is missing.")
    offset, width, height, components, scan_found = 2, None, None, None, False
    while offset + 4 <= len(data):
        if data[offset] != 0xFF:
            raise ImageDecodeError("JPEG segment marker is corrupt.")
        marker = data[offset + 1]
        if marker == 0xFF:
            offset += 1
            continue
        if marker in (0x01,) or 0xD0 <= marker <= 0xD7:
            offset += 2
            continue
        length = int.from_bytes(data[offset + 2:offset + 4], "big")
        if length < 2 or offset + 2 + length > len(data):
            raise ImageDecodeError("JPEG segment is truncated.")
        if marker in _JPEG_SOF:
            if length < 8:
                raise ImageDecodeError("JPEG frame header is truncated.")
            height, width = struct.unpack(">HH", data[offset + 5:offset + 9])
            components = data[offset + 9]
        if marker == 0xDA:
            scan_found = True
            break
        offset += 2 + length
    if not width or not height or not components or not scan_found:
        raise ImageDecodeError("JPEG is missing a valid frame header or scan data.")
    return {"format": "JPEG", "mime_type": "image/jpeg", "width": width, "height": height, "components": components}


def _decode_webp(data):
    if len(data) < 30 or int.from_bytes(data[4:8], "little") + 8 != len(data):
        raise ImageDecodeError("WebP RIFF container length does not match the file size.")
    kind = data[12:16]
    body = data[20:]
    if kind == b"VP8 ":
        if body[3:6] != b"\x9d\x01\x2a":
            raise ImageDecodeError("WebP VP8 frame signature is invalid.")
        width = int.from_bytes(body[6:8], "little") & 0x3FFF
        height = int.from_bytes(body[8:10], "little") & 0x3FFF
    elif kind == b"VP8L":
        if body[0] != 0x2F:
            raise ImageDecodeError("WebP lossless signature is invalid.")
        bits = int.from_bytes(body[1:5], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
    elif kind == b"VP8X":
        width = int.from_bytes(body[4:7], "little") + 1
        height = int.from_bytes(body[7:10], "little") + 1
    else:
        raise ImageDecodeError("WebP bitstream chunk is unsupported.")
    if not width or not height:
        raise ImageDecodeError("WebP declares invalid dimensions.")
    return {"format": "WEBP", "mime_type": "image/webp", "width": width, "height": height}


def inspect_image(data):
    """Return decoded format/dimensions or raise ImageDecodeError."""
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise ImageDecodeError("Image bytes are empty.")
    mime_type = sniff_image_mime(data)
    if mime_type == "image/png":
        details = _decode_png(bytes(data))
        details["decoder"] = "structural+pixel-payload"
    elif mime_type == "image/jpeg":
        details = _decode_jpeg(bytes(data))
        details["decoder"] = "structural"
    elif mime_type == "image/webp":
        details = _decode_webp(bytes(data))
        details["decoder"] = "structural"
    else:
        raise ImageDecodeError("Bytes are not a supported PNG, JPEG, or WebP image.")
    return details


_MP4_CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"dinf", b"udta"}


def _mp4_boxes(data, start, end):
    """Yield (type, payload_start, box_end) for boxes in data[start:end]; raise on inconsistent sizes."""
    offset = start
    while offset < end:
        if offset + 8 > end:
            raise ImageDecodeError("MP4 box header is truncated.")
        size = int.from_bytes(data[offset:offset + 4], "big")
        kind = data[offset + 4:offset + 8]
        header = 8
        if size == 1:
            if offset + 16 > end:
                raise ImageDecodeError("MP4 large box header is truncated.")
            size = int.from_bytes(data[offset + 8:offset + 16], "big")
            header = 16
        elif size == 0:
            size = end - offset
        if size < header or offset + size > end:
            raise ImageDecodeError(f"MP4 box {kind!r} has an invalid size.")
        yield kind, offset + header, offset + size
        offset += size


def _full_box_times(data, payload):
    """Return (timescale, duration) from an mvhd/mdhd payload."""
    version = data[payload]
    if version == 1:
        timescale = int.from_bytes(data[payload + 20:payload + 24], "big")
        duration = int.from_bytes(data[payload + 24:payload + 32], "big")
    else:
        timescale = int.from_bytes(data[payload + 12:payload + 16], "big")
        duration = int.from_bytes(data[payload + 16:payload + 20], "big")
    return timescale, duration


def inspect_video(data):
    """Structurally decode an MP4 (ISO BMFF) file: duration, dimensions, frame rate, codec, audio presence."""
    if not isinstance(data, (bytes, bytearray)) or len(data) < 16:
        raise ImageDecodeError("Video bytes are empty or truncated.")
    data = bytes(data)
    top = list(_mp4_boxes(data, 0, len(data)))
    if not top or top[0][0] != b"ftyp":
        raise ImageDecodeError("File is not an MP4 container (missing leading ftyp box).")
    kinds = [kind for kind, _, _ in top]
    if b"moov" not in kinds:
        raise ImageDecodeError("MP4 is missing its moov metadata box.")
    media_bytes = sum(end - start for kind, start, end in top if kind == b"mdat")
    if media_bytes <= 0:
        raise ImageDecodeError("MP4 has no media data.")
    major_brand = data[top[0][1]:top[0][1] + 4].decode("latin-1")
    moov = next((start, end) for kind, start, end in top if kind == b"moov")
    movie_timescale = movie_duration = None
    tracks = []
    for kind, start, end in _mp4_boxes(data, *moov):
        if kind == b"mvhd":
            movie_timescale, movie_duration = _full_box_times(data, start)
        elif kind == b"trak":
            track = {"handler": None, "width": None, "height": None, "timescale": None, "duration": None,
                     "codec": None, "samples": None}
            stack = [(start, end)]
            while stack:
                box_start, box_end = stack.pop()
                for inner, payload, inner_end in _mp4_boxes(data, box_start, box_end):
                    if inner in _MP4_CONTAINERS:
                        stack.append((payload, inner_end))
                    elif inner == b"tkhd":
                        track["width"] = int.from_bytes(data[inner_end - 8:inner_end - 4], "big") >> 16
                        track["height"] = int.from_bytes(data[inner_end - 4:inner_end], "big") >> 16
                    elif inner == b"mdhd":
                        track["timescale"], track["duration"] = _full_box_times(data, payload)
                    elif inner == b"hdlr":
                        track["handler"] = data[payload + 8:payload + 12]
                    elif inner == b"stsd" and inner_end - payload >= 16:
                        track["codec"] = data[payload + 12:payload + 16].decode("latin-1")
                    elif inner == b"stts":
                        count = int.from_bytes(data[payload + 4:payload + 8], "big")
                        entries = data[payload + 8:payload + 8 + count * 8]
                        if len(entries) != count * 8:
                            raise ImageDecodeError("MP4 sample table is truncated.")
                        track["samples"] = sum(int.from_bytes(entries[i:i + 4], "big") for i in range(0, len(entries), 8))
            tracks.append(track)
    video = next((track for track in tracks if track["handler"] == b"vide"), None)
    if not video:
        raise ImageDecodeError("MP4 has no video track.")
    if not video["width"] or not video["height"]:
        raise ImageDecodeError("MP4 video track declares no dimensions.")
    if not movie_timescale or not movie_duration:
        raise ImageDecodeError("MP4 declares a zero or missing duration.")
    duration = movie_duration / movie_timescale
    frame_rate = None
    if video["samples"] and video["timescale"] and video["duration"]:
        frame_rate = round(video["samples"] / (video["duration"] / video["timescale"]), 3)
    return {
        "format": "MP4", "mime_type": "video/mp4", "major_brand": major_brand, "width": video["width"],
        "height": video["height"], "duration_seconds": round(duration, 3), "frame_rate": frame_rate,
        "codec": video["codec"], "has_audio": any(track["handler"] == b"soun" for track in tracks),
        "media_bytes": media_bytes, "decoder": "iso-bmff-structural",
    }
