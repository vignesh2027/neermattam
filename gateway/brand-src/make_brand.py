"""Brand derivatives for the gateway, from neermattam-logo-original.png.

    cd gateway/brand-src && uv run --with pillow --with numpy python make_brand.py

The backdrop is removed by growing a region from the image border across
the smooth light-blue background; the artwork's edges stop it. Outputs go to
internal/web/static/brand/: the mark alone at 64, 128 and 512 px, favicons
(ico, 32 px, 180 px apple-touch), the full logo for the About page and a
1200x630 Open Graph card. Takes about a minute.
"""
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).resolve().parent
SRC = str(HERE / "neermattam-logo-original.png")


def grow(t=2.2):
    im = Image.open(SRC).convert("RGB").filter(ImageFilter.GaussianBlur(1.2))
    a = np.asarray(im).astype(float)
    h, w, _ = a.shape
    bg = np.zeros((h, w), bool)
    bg[0, :] = bg[-1, :] = bg[:, 0] = bg[:, -1] = True
    light = a.mean(-1) > 170
    for it in range(4000):
        new = bg.copy()
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            src = np.roll(bg, (dy, dx), (0, 1))
            nb = np.roll(a, (dy, dx), (0, 1))
            diff = np.abs(a - nb).max(-1)
            new |= src & (diff < t) & light
        if (new == bg).all():
            break
        bg = new
    return bg, it



OUT = str(HERE.parent / "internal/web/static/brand") + "/"
FONT = str(HERE.parent / "internal/web/static/fonts/inter-latin-wght-normal.woff2")

im = Image.open(SRC).convert("RGB")
bg, _ = grow()
mask = Image.fromarray((~bg * 255).astype(np.uint8))
# Close hairline cracks the flood fill slipped into, then feather by a pixel.
mask = mask.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.MinFilter(5))
mask = mask.filter(ImageFilter.GaussianBlur(0.8))
rgba = im.copy()
rgba.putalpha(mask)
# The mark alone: everything from the wordmark down is cleared.
mark_src = rgba.copy()
cut = Image.new("L", (rgba.width, rgba.height - 835), 0)
mark_alpha = mark_src.getchannel("A")
mark_alpha.paste(cut, (0, 835))
mark_src.putalpha(mark_alpha)
m = np.asarray(mask) > 128


def bbox(rows):
    ys, xs = np.nonzero(m[rows])
    return xs.min(), ys.min() + rows.start, xs.max() + 1, ys.max() + 1 + rows.start


def square(box, pad):
    x0, y0, x1, y1 = box
    side = max(x1 - x0, y1 - y0)
    side = int(side * (1 + 2 * pad))
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    return int(cx - side / 2), int(cy - side / 2), int(cx + side / 2), int(cy + side / 2)


def save(img, name, **kw):
    img.save(OUT + name, optimize=True, **kw)
    print(name, img.size)


mark_box = bbox(slice(0, 830))
mark = mark_src.crop(square(mark_box, 0.03))
for px in (64, 128, 512):
    save(mark.resize((px, px), Image.LANCZOS), f"mark-{px}.png")

word_box = bbox(slice(840, 1030))
word = rgba.crop((word_box[0] - 6, word_box[1] - 6, word_box[2] + 6, word_box[3] + 6))

full_box = bbox(slice(0, 1254))
full = rgba.crop((full_box[0] - 20, full_box[1] - 20, full_box[2] + 20, full_box[3] + 20))
w = 720
save(full.resize((w, round(full.height * w / full.width)), Image.LANCZOS), "logo-full.png")
# The landing page shows the logo 240 px wide: WebP at 1x and 2x, with alpha.
for lw in (240, 480):
    lh = round(full.height * lw / full.width)
    full.resize((lw, lh), Image.LANCZOS).save(OUT + f"logo-{lw}.webp", quality=88, method=6)
    print(f"logo-{lw}.webp", (lw, lh))

# Favicons: transparent for browsers, opaque light tile for iOS.
fav = mark.resize((256, 256), Image.LANCZOS)
fav.save(OUT + "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])
print("favicon.ico")
save(mark.resize((32, 32), Image.LANCZOS), "favicon-32.png")
tile = Image.new("RGBA", (180, 180), (244, 249, 255, 255))
inner = mark.resize((150, 150), Image.LANCZOS)
tile.alpha_composite(inner, (15, 15))
save(tile.convert("RGB"), "apple-touch-icon.png")

# Open Graph card, 1200x630, light-blue theme.
W, H = 1200, 630
yy, xx = np.mgrid[0:H, 0:W]
top = np.array([244, 249, 255], float)
bottom = np.array([219, 234, 254], float)
t = (yy / H)[..., None]
grad = top * (1 - t) + bottom * t
glow = np.exp(-(((xx - 300) / 380.0) ** 2 + ((yy - 300) / 300.0) ** 2))[..., None]
grad = grad * (1 - 0.6 * glow) + np.array([255, 255, 255]) * 0.6 * glow
card = Image.fromarray(grad.clip(0, 255).astype(np.uint8), "RGB").convert("RGBA")
mk = mark.resize((380, 380), Image.LANCZOS)
card.alpha_composite(mk, (70, 125))
TX, TW = 500, 640
wd = word.resize((TW - 60, round(word.height * (TW - 60) / word.width)), Image.LANCZOS)
card.alpha_composite(wd, (TX - 6, 168))
d = ImageDraw.Draw(card)
lines = ["Street-level flood depth for Chennai,", "from one photo"]
size = 44
while True:
    f = ImageFont.truetype(FONT, size)
    try:
        f.set_variation_by_axes([600])
    except Exception:
        pass
    if max(d.textlength(l, font=f) for l in lines) <= TW or size <= 24:
        break
    size -= 1
print("tagline size", size)
y = 168 + wd.height + 34
for line in lines:
    d.text((TX, y), line, font=f, fill=(11, 27, 51))
    y += int(size * 1.32)
# The depth scale as a brand rule under the tagline.
x = TX
for c in ["#BAE6FD", "#38BDF8", "#2563EB", "#4C1D95", "#9F1239"]:
    d.rounded_rectangle((x, y + 22, x + 100, y + 34), radius=6, fill=c)
    x += 108
save(card.convert("RGB"), "og-card.png")
