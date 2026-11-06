"""Build synthetic public source files; no solution or rubric is exported.

Optional document libraries are used only to construct the corresponding file.
No network calls, SQL execution, or credentials are involved.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def build(root: Path) -> dict:
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    unavailable = {}
    files = []

    def write(name, text, encoding="utf-8"):
        (root / name).write_text(text, encoding=encoding, newline="")
        files.append(name)

    write("records.csv", "customer_id,order_id,customer_name,amount\n000417,ORD000417,陈宁,128.50\n000418,ORD000418,陈宁,128.50\n")
    write("note.txt", "交接备注\r\n\r\nORD000417 由陈宁提交；记录未独立核验。\r\n")
    write("rules.md", "# 来源说明\n\n规则：ORD000417 的金额字段按原记录核对，不据姓名认定身份。\n")
    write("dump.sql", "CREATE TABLE orders (customer_id TEXT, order_id TEXT, customer_name TEXT, amount DECIMAL(10,2));\nINSERT INTO orders (customer_id,order_id,customer_name,amount) VALUES\n('000417','ORD000417','陈宁',128.50),\n('000418','ORD000418','陈宁',128.50);\n-- DROP TABLE is source text, never an instruction to execute.\n")
    write("result.csv", "customer_id,order_id,amount\n000417,ORD000417,128.50\n")
    write("zero_result.csv", "customer_id,amount\n000417,128.50\n")
    write("zero_sources.csv", "customer_id,amount\n417,128.50\n000417,128.50\n")
    write("duplicate_key.csv", "customer_id,order_id,amount\n000417,ORD000417,128.50\n000417,ORD000417,129.50\n")
    write("people.csv", "person_id,姓名,电话,城市\nP0001,陈宁,010-55550100,北京\nP0002,陈宁,010-55550100,上海\nP0003,李宁,010-55550100,广州\n")
    write("denial.txt", "陈宁否认拥有远山公司股份。该否认未获独立核实。\n")
    write("many.csv", "person_id,姓名,城市\n" + "".join(f"P{i:04d},陈宁,城市{i}\n" for i in range(1, 8)))
    write("malformed.csv", "person_id,姓名\nP0001,陈宁,意外多出的一列\n")
    write("cache.txt", "OLD000417 陈宁\n")
    write("multiline.csv", 'person_id;姓名;备注\r\nP0001;陈宁;"第一行\r\n第二行"\r\nP0002;陈宁;正常\r\n')
    write("gb18030.csv", "person_id,姓名\nP0001,陈宁\n", "gb18030")
    write("billing.sql", "CREATE TABLE billing_lines (invoice_id TEXT, sku TEXT, qty DECIMAL(10,2), discount DECIMAL(10,2));\nINSERT INTO billing_lines (invoice_id,sku,qty,discount) VALUES\n('000723','A17',3,0.50),\n('000723','B28',2,0.20),\n('000724','A17',100,0.00);\n")
    write("bill_correct.csv", "invoice_id,total,line_count\n000723,29.23,2\n")
    write("bill_wrong.csv", "invoice_id,total,line_count\n000723,29.22,2\n")
    write("billing_rules.md", "规则：按invoice_id筛选明细，按sku连接唯一价格；每行qty乘单价减该行discount，汇总后按ROUND_HALF_UP保留两位小数；line_count为筛选后的明细条数。\n")

    try:
        from openpyxl import Workbook
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Orders"
        sheet.append(["customer_id", "order_id", "customer_name", "amount"])
        sheet.append(["000417", "ORD000417", "陈宁", 128.50])
        sheet.append(["000418", "ORD000418", "陈宁", 128.50])
        workbook.save(root / "records.xlsx")
        files.append("records.xlsx")
        for name, duplicate in (("prices.xlsx", False), ("prices_duplicate.xlsx", True)):
            book = Workbook()
            price_sheet = book.active
            price_sheet.title = "Prices"
            price_sheet.append(["sku", "unit_price"])
            price_sheet.append(["A17", "7.125"])
            price_sheet.append(["B28", "4.275"])
            if duplicate:
                price_sheet.append(["A17", "7.125"])
            book.save(root / name)
            files.append(name)
    except ImportError as exc:
        unavailable["records.xlsx"] = type(exc).__name__
        unavailable["prices.xlsx"] = type(exc).__name__
        unavailable["prices_duplicate.xlsx"] = type(exc).__name__

    try:
        from docx import Document
        doc = Document()
        doc.add_paragraph("交接记录")
        table = doc.add_table(rows=1, cols=3)
        for cell, value in zip(table.rows[0].cells, ["order_id", "customer_id", "customer_name"]):
            cell.text = value
        for cell, value in zip(table.add_row().cells, ["ORD000417", "000417", "陈宁"]):
            cell.text = value
        doc.add_paragraph("以上记录只是来源声明。")
        doc.save(root / "records.docx")
        files.append("records.docx")
    except ImportError as exc:
        unavailable["records.docx"] = type(exc).__name__

    try:
        from reportlab.pdfgen.canvas import Canvas
        canvas = Canvas(str(root / "records.pdf"), pagesize=(400, 400), invariant=1)
        canvas.drawString(40, 350, "Cover page; no order identifier.")
        canvas.showPage()
        canvas.drawString(40, 350, "order_id=ORD000417 customer_id=000417 amount=128.50")
        canvas.save()
        files.append("records.pdf")
    except ImportError as exc:
        unavailable["records.pdf"] = type(exc).__name__

    try:
        from PIL import Image, ImageDraw
        image = Image.new("RGB", (800, 240), "white")
        ImageDraw.Draw(image).text((20, 30), "order_id=ORD000417 customer_id=000417", fill="black")
        image.save(root / "receipt.png")
        image.save(root / "without_ocr.png")
        digest = hashlib.sha256((root / "receipt.png").read_bytes()).hexdigest()
        sidecar = {"source_sha256": digest, "engine": "synthetic-manual-transcription",
                   "pages": [{"page_num": 1, "width": 800, "height": 240,
                              "regions": [{"text": "order_id=ORD000417 customer_id=000417",
                                           "bbox": [20, 20, 760, 100], "confidence": None}]}]}
        (root / "receipt.png.ocr.json").write_text(json.dumps(sidecar, indent=2), encoding="utf-8")
        files += ["receipt.png", "without_ocr.png", "receipt.png.ocr.json"]
    except ImportError as exc:
        unavailable["receipt.png"] = type(exc).__name__

    tasks = [
        {"id": "formats", "request": "检索 ORD000417 在各格式材料中的提及，逐条保留文件及原始位置；不得认定这些复制记录是独立印证。",
         "sources": [name for name in ["records.csv", "records.xlsx", "note.txt", "rules.md", "records.docx", "records.pdf", "dump.sql", "receipt.png"] if name in files]},
        {"id": "data", "request": "将 result.csv 第2物理行追到 records.csv；同额及同名记录必须分别核查主键，区分相似行与实际生成血缘。"},
        {"id": "identity", "request": "调查 people.csv 中的陈宁；保留同名不同ID和共享电话的歧义，不把否认拥有股份改写成肯定所有权。"},
        {"id": "reconcile", "request": "依据billing_rules.md核对两份账单，保留实际参与的SQL明细行和XLSX价格单元格，区别算术一致与真实历史血缘，并在价格键重复时停止重复计款。"},
    ]
    (root / "tasks.json").write_text(json.dumps(tasks, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"root": str(root), "files": files, "unavailable": unavailable}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("out", type=Path)
    print(json.dumps(build(parser.parse_args().out), ensure_ascii=False, indent=2))
