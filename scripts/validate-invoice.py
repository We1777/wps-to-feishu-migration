#!/usr/bin/env python3
"""校验电子发票文件完整性：XML结构校验、OFD文件完整性检查"""

import os
import csv
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STAGING_DIR = PROJECT_ROOT / "03-staging"
REPORT_PATH = PROJECT_ROOT / "05-verification" / "compliance-check" / "invoice-validation-report.csv"

REQUIRED_XML_FIELDS = [
    "InvoiceCode",
    "InvoiceNumber",
    "IssueDate",
    "SellerName",
    "SellerTaxID",
    "BuyerName",
    "BuyerTaxID",
    "TotalAmount",
    "TaxAmount",
]

ALTERNATIVE_FIELD_NAMES = {
    "InvoiceCode": ["发票代码", "fpdm", "invoiceCode"],
    "InvoiceNumber": ["发票号码", "fphm", "invoiceNumber", "invoiceNo"],
    "IssueDate": ["开票日期", "kprq", "issueDate"],
    "SellerName": ["销售方名称", "xsfmc", "sellerName"],
    "SellerTaxID": ["销售方纳税人识别号", "xsfnsrsbh", "sellerTaxId"],
    "BuyerName": ["购买方名称", "gmfmc", "buyerName"],
    "BuyerTaxID": ["购买方纳税人识别号", "gmfnsrsbh", "buyerTaxId"],
    "TotalAmount": ["合计金额", "hjje", "totalAmount"],
    "TaxAmount": ["合计税额", "hjse", "taxAmount"],
}


def find_field_in_xml(root: ET.Element, field: str) -> str | None:
    names_to_try = [field] + ALTERNATIVE_FIELD_NAMES.get(field, [])
    for name in names_to_try:
        for elem in root.iter():
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if tag == name and elem.text:
                return elem.text.strip()
            if elem.get(name):
                return elem.get(name)
    return None


def validate_xml(filepath: Path) -> dict:
    result = {"文件": str(filepath.name), "格式": "XML", "状态": "通过", "缺失字段": "", "备注": ""}
    try:
        tree = ET.parse(filepath)
        root = tree.getroot()
    except ET.ParseError as e:
        result["状态"] = "失败"
        result["备注"] = f"XML解析错误: {e}"
        return result

    missing = []
    for field in REQUIRED_XML_FIELDS:
        if find_field_in_xml(root, field) is None:
            missing.append(field)

    if missing:
        result["状态"] = "警告"
        result["缺失字段"] = ", ".join(missing)
        result["备注"] = "部分必填字段未找到，可能使用不同的字段命名"

    return result


def validate_ofd(filepath: Path) -> dict:
    result = {"文件": str(filepath.name), "格式": "OFD", "状态": "通过", "缺失字段": "", "备注": ""}
    try:
        with zipfile.ZipFile(filepath, "r") as zf:
            names = zf.namelist()
            if "OFD.xml" not in names and not any("OFD.xml" in n for n in names):
                result["状态"] = "警告"
                result["备注"] = "未找到OFD.xml主文档"
            if not any("Signatures" in n or "Signs" in n for n in names):
                result["备注"] += "; 未检测到数字签名"
    except zipfile.BadZipFile:
        result["状态"] = "失败"
        result["备注"] = "非有效ZIP/OFD文件"
    except Exception as e:
        result["状态"] = "失败"
        result["备注"] = str(e)

    return result


def validate_all():
    results = []
    xml_count = 0
    ofd_count = 0

    for root, _, filenames in os.walk(STAGING_DIR):
        for fname in filenames:
            fpath = Path(root) / fname
            ext = fpath.suffix.lower()
            if ext == ".xml":
                results.append(validate_xml(fpath))
                xml_count += 1
            elif ext == ".ofd":
                results.append(validate_ofd(fpath))
                ofd_count += 1

    if not results:
        print("未在03-staging中找到XML或OFD发票文件")
        return

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(REPORT_PATH, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["文件", "格式", "状态", "缺失字段", "备注"])
        writer.writeheader()
        writer.writerows(results)

    passed = sum(1 for r in results if r["状态"] == "通过")
    warned = sum(1 for r in results if r["状态"] == "警告")
    failed = sum(1 for r in results if r["状态"] == "失败")

    print(f"发票校验完成：")
    print(f"  XML文件：{xml_count}，OFD文件：{ofd_count}")
    print(f"  通过：{passed}，警告：{warned}，失败：{failed}")
    print(f"  报告：{REPORT_PATH}")


if __name__ == "__main__":
    validate_all()
