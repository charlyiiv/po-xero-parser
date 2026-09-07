import re
from decimal import Decimal, ROUND_HALF_UP

import pymupdf
from fastapi import FastAPI, File, HTTPException, UploadFile

app = FastAPI()


def num(value: str) -> Decimal:
    return Decimal(
        value.replace("£", "").replace(",", "").strip()
    )


@app.get("/")
def health():
    return {"status": "ok"}


@app.post("/parse-po")
async def parse_po(file: UploadFile = File(...)):
    if file.content_type != "application/pdf":
        raise HTTPException(
            status_code=400,
            detail="File must be a PDF"
        )

    pdf_bytes = await file.read()

    try:
        doc = pymupdf.open(
            stream=pdf_bytes,
            filetype="pdf"
        )

        text = "\n".join(
            page.get_text("text")
            for page in doc
        )

    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Could not read PDF: {exc}"
        )

    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip()
    ]

    # Customer
    if "Abtech Limited" not in text:
        raise HTTPException(
            status_code=422,
            detail="Customer could not be identified"
        )

    customer = "Abtech Limited"

    # PO number
    po_match = re.search(
        r"\bP/\d{5,}\b",
        text
    )

    if not po_match:
        raise HTTPException(
            status_code=422,
            detail="PO number could not be identified"
        )

    po_number = po_match.group(0)

    # Parse PO items.
    #
    # PyMuPDF extracts each Abtech item as:
    #
    # 13
    # ESSX32003GPBCDBOX
    # ABC079605
    # 4.000 EACH
    # 344.6600 06/10/2026
    # 1,378.640
    # SX3.200 3GP SIDES BCD Base / Door

    parsed_lines = []

    i = 0

    while i <= len(lines) - 7:
        if not re.fullmatch(r"\d+", lines[i]):
            i += 1
            continue

        item_number = int(lines[i])

        quantity_match = re.fullmatch(
            r"([\d,.]+)\s+[A-Za-z]+",
            lines[i + 3]
        )

        price_match = re.fullmatch(
            r"([\d,.]+)\s+\d{2}/\d{2}/\d{4}",
            lines[i + 4]
        )

        value_match = re.fullmatch(
            r"[\d,.]+",
            lines[i + 5]
        )

        if not (
            quantity_match
            and price_match
            and value_match
        ):
            i += 1
            continue

        quantity = num(
            quantity_match.group(1)
        )

        unit_price = num(
            price_match.group(1)
        )

        source_value = num(
            lines[i + 5]
        )

        description = lines[i + 6]

        calculated_value = (
            quantity * unit_price
        ).quantize(
            Decimal("0.001"),
            rounding=ROUND_HALF_UP
        )

        if (
            abs(calculated_value - source_value)
            > Decimal("0.01")
        ):
            raise HTTPException(
                status_code=422,
                detail={
                    "message":
                        "Line value validation failed",
                    "item": item_number,
                    "description": description,
                    "quantity": str(quantity),
                    "unit_price": str(unit_price),
                    "source_value":
                        str(source_value),
                    "calculated_value":
                        str(calculated_value),
                }
            )

        parsed_lines.append({
            "item_number": item_number,
            "description": description,
            "quantity": quantity,
            "unit_price": unit_price,
            "source_value": source_value,
        })

        i += 7

    if not parsed_lines:
        raise HTTPException(
            status_code=422,
            detail="No purchase order lines were found"
        )

    # Check that we got every numbered item.
    item_numbers = [
        row["item_number"]
        for row in parsed_lines
    ]

    expected_numbers = list(
        range(1, len(parsed_lines) + 1)
    )

    if item_numbers != expected_numbers:
        raise HTTPException(
            status_code=422,
            detail={
                "message":
                    "PO item sequence validation failed",
                "found": item_numbers,
                "expected": expected_numbers,
            }
        )

    # Calculate PO total independently.
    calculated_total = sum(
        row["source_value"]
        for row in parsed_lines
    ).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP
    )

    # Find the printed final PO total in the PDF.
    po_total = None

    for line in lines:
        cleaned = (
            line.replace("£", "")
            .replace(",", "")
            .strip()
        )

        if re.fullmatch(
            r"\d+\.\d{2,4}",
            cleaned
        ):
            candidate = Decimal(cleaned)

            if (
                abs(
                    candidate -
                    calculated_total
                )
                <= Decimal("0.01")
            ):
                po_total = candidate.quantize(
                    Decimal("0.01"),
                    rounding=ROUND_HALF_UP
                )
                break

    if po_total is None:
        raise HTTPException(
            status_code=422,
            detail={
                "message":
                    "Final PO total could not be validated",
                "calculated_total":
                    str(calculated_total),
            }
        )

    return {
        "customer": customer,
        "po_number": po_number,
        "line_count": len(parsed_lines),
        "calculated_total": float(
            calculated_total
        ),
        "po_total": float(po_total),
        "valid": True,
        "lines": [
            {
                "description":
                    row["description"],
                "quantity":
                    float(row["quantity"]),
                "unit_price":
                    float(row["unit_price"]),
            }
            for row in parsed_lines
        ],
    }
