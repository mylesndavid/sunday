"""Generate extension icons (gradient rounded square + half-circle agent mark)."""
import numpy as np
from PIL import Image, ImageDraw

C1 = (255, 0, 122)   # accent
C2 = (124, 92, 255)  # accent-2
OUT = "icons"

def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))

def make(size):
    S = size * 4  # supersample
    # diagonal gradient
    xs = np.linspace(0, 1, S)
    gx, gy = np.meshgrid(xs, xs)
    t = (gx + gy) / 2.0
    arr = np.zeros((S, S, 4), dtype=np.uint8)
    for i in range(3):
        arr[..., i] = (C1[i] + (C2[i] - C1[i]) * t).astype(np.uint8)
    arr[..., 3] = 255
    img = Image.fromarray(arr, "RGBA")

    # rounded corners mask
    mask = Image.new("L", (S, S), 0)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.22), fill=255)
    img.putalpha(mask)

    # agent mark: half-filled circle (matches ◑)
    d = ImageDraw.Draw(img)
    r = int(S * 0.30)
    cx = cy = S // 2
    bbox = [cx - r, cy - r, cx + r, cy + r]
    d.ellipse(bbox, fill=(255, 255, 255, 255))           # full white disc
    d.pieslice(bbox, 90, 270, fill=(255, 255, 255, 70))  # dim the left half
    # subtle ring
    d.ellipse(bbox, outline=(255, 255, 255, 230), width=max(2, int(S * 0.012)))

    img = img.resize((size, size), Image.LANCZOS)
    img.save(f"{OUT}/icon{size}.png")
    print("wrote", f"{OUT}/icon{size}.png")

for s in (16, 48, 128):
    make(s)
