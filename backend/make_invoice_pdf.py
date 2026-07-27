"""Generate a sample purchase invoice PDF (bar/cement/tiles) to test the
Invoice tab's Document Digitization pipeline end-to-end."""
from pathlib import Path
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

OUT = Path(__file__).resolve().parent.parent / "data" / "sample_invoice.pdf"

rows = [
    ("Tata Tiscon TMT Bar 12mm Fe500D", "3.00 MT", "57,500", "1,72,500.00"),
    ("Tata Tiscon TMT Bar 16mm Fe500D", "2.00 MT", "56,500", "1,13,000.00"),
    ("UltraTech OPC 53 Cement 50kg", "100 Bags", "415", "41,500.00"),
    ("UltraTech PPC Cement 50kg", "80 Bags", "390", "31,200.00"),
    ("Kajaria Ceramic Floor Tile 2x2ft", "40 Box", "700", "28,000.00"),
]
freight = "6,500.00"
taxable = sum(float(r[3].replace(",", "")) for r in rows)
gst = round(taxable * 0.20, 2)  # blended illustrative rate
grand = taxable + gst + float(freight.replace(",", ""))

c = canvas.Canvas(str(OUT), pagesize=A4)
w, h = A4

c.setFont("Helvetica-Bold", 16)
c.drawString(20 * mm, h - 20 * mm, "SHREE BALAJI STEEL & CEMENT TRADERS")
c.setFont("Helvetica", 9)
c.drawString(20 * mm, h - 26 * mm, "GSTIN: 09ABCDE1234F1Z5   |   Ph: 98xxxxxx21")
c.drawString(20 * mm, h - 31 * mm, "Tax Invoice No: SB/2026-27/0501        Date: 26-07-2026")
c.line(20 * mm, h - 35 * mm, w - 20 * mm, h - 35 * mm)

y = h - 45 * mm
c.setFont("Helvetica-Bold", 10)
c.drawString(20 * mm, y, "Description")
c.drawString(105 * mm, y, "Qty")
c.drawString(135 * mm, y, "Rate")
c.drawString(165 * mm, y, "Amount")
y -= 6 * mm
c.line(20 * mm, y + 3 * mm, w - 20 * mm, y + 3 * mm)

c.setFont("Helvetica", 9)
for desc, qty, rate, amt in rows:
    c.drawString(20 * mm, y, desc)
    c.drawString(105 * mm, y, qty)
    c.drawRightString(150 * mm, y, rate)
    c.drawRightString(190 * mm, y, amt)
    y -= 7 * mm

y -= 2 * mm
c.drawString(20 * mm, y, "Transport / Freight (LR No. 3105)")
c.drawRightString(190 * mm, y, freight)
y -= 8 * mm
c.line(120 * mm, y + 4 * mm, w - 20 * mm, y + 4 * mm)

c.setFont("Helvetica-Bold", 9)
c.drawString(120 * mm, y, "Sub Total (taxable):")
c.drawRightString(190 * mm, y, f"{taxable:,.2f}")
y -= 6 * mm
c.setFont("Helvetica", 9)
c.drawString(120 * mm, y, "CGST+SGST/IGST:")
c.drawRightString(190 * mm, y, f"{gst:,.2f}")
y -= 8 * mm
c.setFont("Helvetica-Bold", 11)
c.setFillColorRGB(0.5, 0.05, 0.05)
c.drawString(120 * mm, y, "GRAND TOTAL:")
c.drawRightString(190 * mm, y, f"{grand:,.2f}")

c.showPage()
c.save()
print(f"wrote {OUT}")
