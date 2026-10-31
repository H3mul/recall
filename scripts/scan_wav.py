"""Per-second silence check per channel of a PCM WAV: which seconds carry any non-zero sample.

Usage: python3 -I scan_wav.py FILE.wav
Stdlib only. Prints one line per second: the share of non-zero bytes in each channel (0% = all zeros).
"""
import struct
import sys


def main(path: str) -> None:
    with open(path, "rb") as f:
        riff = f.read(12)
        if riff[:4] != b"RIFF" or riff[8:] != b"WAVE":
            sys.exit("not a WAVE file")
        fmt = None
        while True:
            hdr = f.read(8)
            if len(hdr) < 8:
                sys.exit("no data chunk")
            cid, size = hdr[:4], struct.unpack("<I", hdr[4:])[0]
            if cid == b"fmt ":
                fmt = f.read(size)
            elif cid == b"data":
                break
            else:
                f.seek(size + (size & 1), 1)
        data_off = f.tell()
        file_end = f.seek(0, 2)
        channels, rate = struct.unpack("<HI", fmt[2:8])
        bits = struct.unpack("<H", fmt[14:16])[0]
        width = bits // 8
        frame = channels * width
        data = min(size, file_end - data_off)  # the header can claim more than the file holds
        print(f"{channels} ch, {rate} Hz, {bits}-bit; header says {size} B of data, file has {data} B")
        per_sec = rate * frame
        f.seek(data_off)
        for second in range(data // per_sec):
            buf = f.read(per_sec)
            shares = []
            for c in range(channels):
                chan = [buf[c * width + k::frame] for k in range(width)]
                total = sum(len(b) for b in chan)
                nonzero = sum(len(b) - b.count(0) for b in chan)
                shares.append(f"{100 * nonzero / total:5.1f}%")
            print(f"t={second:5d}s  non-zero per ch: " + " ".join(shares))


if __name__ == "__main__":
    main(sys.argv[1])
