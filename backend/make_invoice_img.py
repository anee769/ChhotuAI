"""Generate a deliberately hard sample supplier invoice image.

The descriptions and units match Gupta Hardware's inventory schema. One
handwritten quantity remains uncertain so the demo still shows owner review
instead of pretending the document model is infallible.
"""
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import random
from pathlib import Path

random.seed(7)
W, H = 900, 1150
img = Image.new("RGB", (W, H), (243, 240, 230))
d = ImageDraw.Draw(img)

def font(sz, bold=False):
    for p in ["/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else
              "/System/Library/Fonts/Supplemental/Arial.ttf",
              "/System/Library/Fonts/Helvetica.ttc"]:
        try:
            return ImageFont.truetype(p, sz)
        except Exception:
            continue
    return ImageFont.load_default()

# uneven lighting gradient
for y in range(H):
    shade = int(18 * (y / H))
    d.line([(0, y), (W, y)], fill=(243 - shade, 240 - shade, 230 - shade))

d.text((40, 30), "SHREE BALAJI STEEL & CEMENT TRADERS", font=font(30, True), fill=(20, 20, 30))
d.text((40, 72), "GSTIN: 09ABCDE1234F1Z5   |   Ph: 98xxxxxx21", font=font(16), fill=(60, 60, 60))
d.text((40, 96), "Tax Invoice No: SB/2026-27/0501        Date: 30-07-2026", font=font(16), fill=(60, 60, 60))
d.line([(40, 128), (W - 40, 128)], fill=(90, 90, 90), width=2)

# header row
hy = int(0.20 * H)
d.text((55, hy), "Description", font=font(17, True), fill=(0, 0, 0))
d.text((560, hy), "Qty", font=font(17, True), fill=(0, 0, 0))
d.text((650, hy), "Rate", font=font(17, True), fill=(0, 0, 0))
d.text((770, hy), "Amount", font=font(17, True), fill=(0, 0, 0))

rows = [
    ("Tata Tiscon TMT Bar 12mm Fe500D", "3.00 tonne", "58,000", "1,74,000.00", False),
    ("Tata Tiscon TMT Bar 16mm Fe500D", "2.00 tonne", "57,000", "1,14,000.00", False),
    ("UltraTech OPC 53 Cement 50kg", "100 bori", "415", "41,500.00", False),
    ("UltraTech PPC Cement 50kg", "80 bori", "390", "39,000.00", "hand"),
    ("Kajaria Ceramic Floor Tile 2x2ft", "40 Box", "700", "28,000.00", False),
]
y = int(0.27 * H)
step = int(0.07 * H)
for desc, qty, rate, amt, flag in rows:
    d.text((55, y), desc, font=font(16), fill=(15, 15, 15))
    if flag == "hand":
        # struck '80', handwritten '100' in margin (blue-ish)
        d.text((560, y), qty, font=font(16), fill=(15, 15, 15))
        d.line([(560, y + 10), (588, y + 10)], fill=(200, 30, 30), width=2)  # strike '80'
        d.text((596, y - 8), "100", font=font(22, True), fill=(20, 40, 160))
        d.text((650, y), rate, font=font(16), fill=(15, 15, 15))
        d.text((770, y), amt, font=font(16), fill=(15, 15, 15))
    else:
        d.text((560, y), qty, font=font(16), fill=(15, 15, 15))
        d.text((650, y), rate, font=font(16), fill=(15, 15, 15))
        d.text((770, y), amt, font=font(16), fill=(15, 15, 15))
    y += step

# freight line
fy = int(0.66 * H)
d.text((55, fy), "Transport / Freight (LR No. 2291)", font=font(16), fill=(15, 15, 15))
d.text((770, fy), "6,500.00", font=font(16), fill=(15, 15, 15))
d.line([(40, fy + 40), (W - 40, fy + 40)], fill=(90, 90, 90), width=1)
d.text((560, fy + 55), "Sub Total (taxable):", font=font(16, True), fill=(0, 0, 0))
d.text((770, fy + 55), "3,96,500.00", font=font(16, True), fill=(0, 0, 0))
d.text((560, fy + 80), "CGST+SGST/IGST:", font=font(15), fill=(0, 0, 0))
d.text((770, fy + 80), "82,220.00", font=font(15), fill=(0, 0, 0))
d.text((560, fy + 108), "GRAND TOTAL:", font=font(18, True), fill=(120, 20, 20))
d.text((770, fy + 108), "4,85,220.00", font=font(18, True), fill=(120, 20, 20))

# creases / noise
for _ in range(3):
    x = random.randint(100, W - 100)
    d.line([(x, 130), (x + random.randint(-40, 40), H - 60)], fill=(220, 216, 205), width=2)
img = img.filter(ImageFilter.GaussianBlur(0.4))
# slight rotation (angled photo)
img = img.rotate(-2.2, expand=True, fillcolor=(228, 224, 214))

out = Path(__file__).resolve().parent.parent / "data" / "sample_invoice.png"
img.save(out)
print("wrote", out, img.size)
