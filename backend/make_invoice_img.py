"""Generate a deliberately HARD sample invoice image (angled, uneven light,
handwritten correction, one smudged/illegible row) matching invoice_fixture.json
bboxes. Not a clean scan — that's the point (spec 9.1)."""
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
d.text((40, 96), "Tax Invoice No: SB/2026-27/0413        Date: 24-07-2026", font=font(16), fill=(60, 60, 60))
d.line([(40, 128), (W - 40, 128)], fill=(90, 90, 90), width=2)

# header row
hy = int(0.20 * H)
d.text((55, hy), "Description", font=font(17, True), fill=(0, 0, 0))
d.text((560, hy), "Qty", font=font(17, True), fill=(0, 0, 0))
d.text((650, hy), "Rate", font=font(17, True), fill=(0, 0, 0))
d.text((770, hy), "Amount", font=font(17, True), fill=(0, 0, 0))

rows = [
    ("Tata Tiscon TMT Bar 12mm Fe500D", "3.00 MT", "52,000", "1,56,000.00", False),
    ("Tata Tiscon TMT Bar 16mm Fe500D", "2.00 MT", "51,000", "1,02,000.00", False),
    ("UltraTech OPC 53 Cement 50kg", "100 Bags", "330", "33,000.00", False),
    ("Jindal Panther TMT 10mm Fe500D", "@2 MT", "55,000", "1,65,000.00", "hand"),
    ("~~~~  ~~~~~~  (smudged)  ~~~~", "~~", "~~", "~~~~", "smudge"),
]
y = int(0.27 * H)
step = int(0.07 * H)
for desc, qty, rate, amt, flag in rows:
    d.text((55, y), desc, font=font(16), fill=(15, 15, 15))
    if flag == "hand":
        # struck '2', handwritten '3' in margin (blue-ish)
        d.text((560, y), qty, font=font(16), fill=(15, 15, 15))
        d.line([(566, y + 10), (582, y + 10)], fill=(200, 30, 30), width=2)  # strike
        d.text((600, y - 6), "3", font=font(24, True), fill=(20, 40, 160))
        d.text((650, y), rate, font=font(16), fill=(15, 15, 15))
        d.text((770, y), amt, font=font(16), fill=(15, 15, 15))
    elif flag == "smudge":
        d.text((55, y), desc, font=font(16), fill=(120, 120, 120))
        # smudge blob
        d.ellipse([300, y - 4, 520, y + 26], fill=(200, 195, 180))
    else:
        d.text((560, y), qty, font=font(16), fill=(15, 15, 15))
        d.text((650, y), rate, font=font(16), fill=(15, 15, 15))
        d.text((770, y), amt, font=font(16), fill=(15, 15, 15))
    y += step

# freight line
fy = int(0.66 * H)
d.text((55, fy), "Transport / Freight (LR No. 2291)", font=font(16), fill=(15, 15, 15))
d.text((770, fy), "4,500.00", font=font(16), fill=(15, 15, 15))
d.line([(40, fy + 40), (W - 40, fy + 40)], fill=(90, 90, 90), width=1)
d.text((560, fy + 55), "Sub Total (taxable):", font=font(16, True), fill=(0, 0, 0))
d.text((770, fy + 55), "4,56,000.00", font=font(16, True), fill=(0, 0, 0))
d.text((560, fy + 80), "CGST+SGST/IGST:", font=font(15), fill=(0, 0, 0))
d.text((770, fy + 80), "84,540.00", font=font(15), fill=(0, 0, 0))
d.text((560, fy + 108), "GRAND TOTAL:", font=font(18, True), fill=(120, 20, 20))
d.text((770, fy + 108), "5,45,040.00", font=font(18, True), fill=(120, 20, 20))

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
