#!/usr/bin/env python3
"""Stream an XLSX sheet and keep exact-key matches with original cell addresses.

This does not build a whole-workbook index or calculate Excel formulas. Numeric
values retain their XML decimal text. Date cells additionally expose iso_value.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import sys
import time
import tempfile
import xml.etree.ElementTree as ET
from xml.parsers import expat
from zipfile import ZipFile, BadZipFile

try:
    from lxml import etree as _fast_xml
except ImportError:
    _fast_xml = None

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
M = "{" + MAIN + "}"
REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
ADDRESS = re.compile(r"([A-Z]+)([1-9][0-9]*)\Z")
READER_VERSION = "xlsx-query-v4-validated-frames-canonical-content-cache"


class _UnsupportedFrames(ValueError):
    """Valid XML outside the deliberately limited fast-framing layout."""


def _reject_declaration(*args):
    raise ValueError("DTD and entity declarations are not supported in XLSX XML")


def _xml_validator():
    parser = expat.ParserCreate(namespace_separator="|")
    parser.StartDoctypeDeclHandler = _reject_declaration
    parser.EntityDeclHandler = _reject_declaration
    parser.ExternalEntityRefHandler = _reject_declaration
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    return parser


class _ValidatedXMLStream:
    def __init__(self, stream):
        self.stream = stream
        self.validator = _xml_validator()
        self.finished = False

    def read(self, size=-1):
        data = self.stream.read(size)
        if not self.finished:
            try:
                self.validator.Parse(data, not data)
            except expat.ExpatError as exc:
                raise ValueError(f"Invalid XLSX XML: {exc}") from exc
            if not data:
                self.finished = True
        return data


def _metadata_xml(data):
    validator = _xml_validator()
    try:
        validator.Parse(data, True)
    except expat.ExpatError as exc:
        raise ValueError(f"Invalid workbook metadata XML: {exc}") from exc
    return ET.fromstring(data)


_ROW_FRAME = re.compile(rb'<row(?=[\s/>])(?P<attrs>(?:\s+[A-Za-z_][\w:.-]*\s*=\s*(?:"[^"]*"|\'[^\']*\'))*\s*)(?:/>|>.*?</row\s*>)', re.DOTALL)
_PREFIXED_TAG = re.compile(rb'</?[A-Za-z_][\w.-]*:')
_ATTRIBUTE = rb'[A-Za-z_][\w:.-]*\s*=\s*(?:"[^"]*"|\'[^\']*\')'
_ROW_NUMBER = re.compile(rb'(?:^|\s)r\s*=\s*(?:"([1-9][0-9]*)"|\'([1-9][0-9]*)\')')


def _framed_rows(stream):
    """Frame canonical rows while Expat validates every decompressed byte.

    This is a lexical optimization for a restricted XML serialization, not an
    XML parser. Comments, CDATA, prefixes, local namespace declarations and
    non-UTF-8 serializations select the compatible XML reader instead. No
    partial or unvalidated scan can become a successful result or cache entry.
    """
    validator = _xml_validator()
    buffer = b""
    tail = b""
    root_open = None
    in_sheet_data = False
    closed_sheet_data = False
    while True:
        chunk = stream.read(1024 * 1024)
        try:
            validator.Parse(chunk, not chunk)
        except expat.ExpatError as exc:
            raise ValueError(f"Invalid worksheet XML: {exc}") from exc
        detection = tail + chunk
        if not closed_sheet_data:
            # Extensions after sheetData do not participate in row framing.
            # They still pass through Expat, without forcing a late rescan.
            data_end = re.search(rb'</sheetData\s*>|<sheetData\s*/>', detection)
            layout_scope = detection[:data_end.end()] if data_end is not None else detection
            if (b'\x00' in layout_scope or b'<!--' in layout_scope or b'<![CDATA[' in layout_scope
                    or _PREFIXED_TAG.search(layout_scope) or re.search(rb'<\?(?!xml(?:\s|\?))', layout_scope)):
                raise _UnsupportedFrames("Worksheet uses prefixes, comments, CDATA, or a non-UTF-8 serialization")
        tail = detection[-256:]
        buffer += chunk
        if root_open is None:
            opening = re.search(rb'<worksheet(?=[\s>])(?:\s+' + _ATTRIBUTE + rb')*\s*>', buffer)
            if opening is None:
                if not chunk or len(buffer) > 1024 * 1024:
                    raise _UnsupportedFrames("Unsupported worksheet root serialization")
                continue
            root_open = opening.group()
            encoding = re.search(rb'<\?xml[^>]*encoding\s*=\s*["\']([^"\']+)', buffer[:opening.start()])
            if encoding is not None and encoding.group(1).lower() not in (b'utf-8', b'utf8', b'us-ascii'):
                raise _UnsupportedFrames("Fast framing requires UTF-8 worksheet XML")
            try:
                probe = ET.fromstring(root_open + b'</worksheet>')
            except ET.ParseError as exc:
                raise _UnsupportedFrames("Unsupported worksheet root namespace") from exc
            if probe.tag != M + 'worksheet':
                raise _UnsupportedFrames("Unsupported worksheet namespace")
            buffer = buffer[opening.end():]
        # The root namespace declarations are sufficient only if they remain
        # unchanged within the worksheet. Attributes such as x14ac:dyDescent
        # are supported through the original root declarations.
        data_end = re.search(rb'</sheetData\s*>|<sheetData\s*/>', buffer)
        namespace_scope = buffer[:data_end.end()] if data_end is not None else buffer
        if not closed_sheet_data and re.search(rb'\sxmlns(?:\:[\w.-]+)?\s*=', namespace_scope):
            raise _UnsupportedFrames("Local namespace declarations require the compatible XML reader")
        if not in_sheet_data and not closed_sheet_data:
            start = re.search(rb'<sheetData\s*>', buffer)
            empty = re.search(rb'<sheetData\s*/>', buffer)
            if empty is not None and (start is None or empty.start() < start.start()):
                buffer = buffer[empty.end():]
                closed_sheet_data = True
            elif start is not None:
                buffer = buffer[start.end():]
                in_sheet_data = True
            elif len(buffer) > 16 * 1024 * 1024:
                raise _UnsupportedFrames("Unsupported worksheet preamble")
        if in_sheet_data:
            consumed = 0
            while True:
                consumed = re.compile(rb'\s*').match(buffer, consumed).end()
                frame = _ROW_FRAME.match(buffer, consumed)
                if frame is None:
                    closing = re.compile(rb'</sheetData\s*>').match(buffer, consumed)
                    if closing is not None:
                        consumed = closing.end()
                        in_sheet_data = False
                        closed_sheet_data = True
                    else:
                        pending = buffer[consumed:]
                        if pending and not (pending.startswith(b'<row') or b'<row'.startswith(pending)
                                            or b'</sheetData>'.startswith(pending)):
                            raise _UnsupportedFrames("Unexpected sheetData serialization")
                    break
                raw = frame.group()
                if raw.count(b'<row') > 1:
                    raise _UnsupportedFrames("Nested row elements require the compatible XML reader")
                attrs = frame.group('attrs')
                number = _ROW_NUMBER.search(attrs)
                if number is None:
                    raise _UnsupportedFrames("Rows without explicit physical addresses require the compatible reader")
                row_number = int(number.group(1) or number.group(2))
                yield row_number, raw, root_open
                consumed = frame.end()
            buffer = buffer[consumed:]
        elif closed_sheet_data:
            buffer = b""
        if len(buffer) > 16 * 1024 * 1024:
            raise _UnsupportedFrames("A single row exceeds the fast-reader frame budget")
        if not chunk:
            if in_sheet_data or not closed_sheet_data:
                raise _UnsupportedFrames("Worksheet row framing did not cover a complete sheetData")
            break


def _parse_frame(raw, root_open):
    try:
        content = root_open + raw + b'</worksheet>'
        root = (_fast_xml.fromstring(content, parser=_fast_xml.XMLParser(resolve_entities=False, no_network=True))
                if _fast_xml is not None else ET.fromstring(content))
    except (ET.ParseError, ValueError) as exc:
        raise _UnsupportedFrames("Row fragment requires the compatible XML reader") from exc
    if len(root) != 1 or root[0].tag != M + 'row':
        raise _UnsupportedFrames("Unexpected row namespace or structure")
    return root[0]


def _prefilter_spec(column, allowed, strings):
    # Only shared-string cells can be rejected without decoding a row. Numeric,
    # inline-string, formula and missing cells are decoded by the normal path.
    target = column.encode('ascii') + rb'[1-9][0-9]*'
    selector = re.compile(rb'<c(?:\s+' + _ATTRIBUTE + rb')*?\s+r\s*=\s*(?:"' + target + rb'"|\'' + target + rb'\')(?:\s+' + _ATTRIBUTE + rb')*\s*(?:/>|>.*?</c\s*>)', re.DOTALL)
    allowed_indices = {str(index).encode('ascii') for index, value in enumerate(strings)
                       if _exact(value) in allowed}
    return selector, allowed_indices, len(strings), column


def _can_reject_frame(raw, specification, row_num):
    selector, allowed_indices, string_count, column = specification
    found = selector.search(raw)
    if found is None:
        return False
    # A duplicate or entity-encoded address must be inspected by the ordinary
    # cell lookup, which raises for duplicate referenced addresses. Rejecting
    # based on the first occurrence could silently hide a later matching cell.
    expected_address = f'{column}{row_num}'.encode('ascii')
    # Count complete quoted values in C rather than running another cell regex
    # through the rest of every rejected row. Whitespace around r= is irrelevant
    # and another field containing the same quoted value only causes a safe,
    # conservative full decode. AD7 cannot be mistaken for A7.
    if raw.count(b'"' + expected_address + b'"') + raw.count(b"'" + expected_address + b"'") > 1:
        return False
    if b'&' in raw and re.search(rb'\sr\s*=\s*(?:"[^"]*&|\'[^\']*&)', raw):
        return False
    previous = raw[:found.start()]
    previous_tag = previous[previous.rfind(b'<'):].strip()
    if not (previous_tag.startswith(b'<row') or previous_tag.startswith(b'</c')
            or (previous_tag.startswith(b'<c') and previous_tag.endswith(b'/>'))):
        return False
    cell = found.group()
    opening = re.match(rb'<c(?:\s+' + _ATTRIBUTE + rb')*\s*/?>', cell)
    if opening is None or re.search(rb'(?:^|\s)t\s*=\s*(?:"s"|\'s\')(?=\s|/|>)', opening.group()) is None:
        return False
    actual_address = re.search(rb'\sr\s*=\s*(?:"([A-Z]+[1-9][0-9]*)"|\'([A-Z]+[1-9][0-9]*)\')', opening.group())
    if actual_address is None or (actual_address.group(1) or actual_address.group(2)) != expected_address:
        return False
    if b'<f' in cell:
        return False
    value = re.search(rb'<v\s*>([0-9]+)</v\s*>', cell)
    if value is None:
        return False
    index = int(value.group(1))
    if not 0 <= index < string_count:
        return False
    canonical = str(index).encode('ascii')
    return canonical not in allowed_indices


def _text(element):
    if element is None:
        return ""
    # Phonetic annotations are not part of the displayed cell text.
    parts = []
    for child in element:
        if child.tag == M + "t":
            parts.append(child.text or "")
        elif child.tag == M + "r":
            parts.extend(node.text or "" for node in child if node.tag == M + "t")
    return "".join(parts)


def _elements(stream, tag):
    """Yield complete elements, deleting already consumed XML siblings."""
    stream = _ValidatedXMLStream(stream)
    if _fast_xml is not None:
        parser = _fast_xml.iterparse(stream, events=("end",), tag=tag,
                                     resolve_entities=False, no_network=True,
                                     huge_tree=True)
        for _, element in parser:
            yield element
            parent = element.getparent()
            element.clear()
            if parent is not None:
                while element.getprevious() is not None:
                    del parent[0]
    else:
        stack = []
        for event, element in ET.iterparse(stream, events=("start", "end")):
            if event == "start":
                stack.append(element)
            else:
                if element.tag == tag:
                    yield element
                    element.clear()
                    if len(stack) > 1:
                        stack[-2].remove(element)
                stack.pop()


def _relationships(archive):
    workbook = _metadata_xml(archive.read("xl/workbook.xml"))
    rels = _metadata_xml(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {}
    for rel in rels:
        if rel.attrib.get("TargetMode") == "External":
            continue
        target = rel.attrib.get("Target", "")
        path = posixpath.normpath(target.lstrip("/") if target.startswith("/")
                                 else posixpath.join("xl", target))
        if not path.startswith("xl/") or ".." in path.split("/"):
            raise ValueError("Workbook relationship is outside the XLSX workbook")
        targets[rel.attrib["Id"]] = path
    sheets = []
    for element in workbook.find(M + "sheets"):
        relation = element.attrib.get(REL + "id")
        if relation not in targets:
            raise ValueError("Worksheet relationship is missing or external")
        sheets.append((element.attrib["name"], targets[relation]))
    properties = workbook.find(M + "workbookPr")
    epoch_1904 = properties is not None and properties.attrib.get("date1904") in ("1", "true")
    return sheets, epoch_1904


def _styles(archive):
    formats = {}
    if "xl/styles.xml" not in archive.namelist():
        return [None], formats
    root = _metadata_xml(archive.read("xl/styles.xml"))
    custom = root.find(M + "numFmts")
    if custom is not None:
        formats.update({int(item.attrib["numFmtId"]): item.attrib["formatCode"]
                        for item in custom})
    xfs = root.find(M + "cellXfs")
    return ([int(item.attrib.get("numFmtId", "0")) for item in xfs]
            if xfs is not None else [0]), formats


def _shared_strings(archive):
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    strings = []
    with archive.open("xl/sharedStrings.xml") as stream:
        for element in _elements(stream, M + "si"):
            strings.append(_text(element))
    return strings


def _cell(element, strings, style_ids, custom_formats, epoch_1904):
    address = element.attrib.get("r", "")
    if not ADDRESS.fullmatch(address):
        raise ValueError(f"Invalid or missing cell address: {address!r}")
    kind = element.attrib.get("t", "n")
    value_node = element.find(M + "v")
    raw = value_node.text if value_node is not None else None
    formula_node = element.find(M + "f")
    formula = None if formula_node is None else "=" + (formula_node.text or "")
    style_id = int(element.attrib.get("s", "0"))
    if not 0 <= style_id < len(style_ids):
        raise ValueError(f"Invalid style reference at {address}: {style_id}")
    if kind == "s":
        try:
            index = int(raw)
            if index < 0:
                raise ValueError("negative shared-string index")
            value = strings[index]
        except (ValueError, TypeError, IndexError, KeyError):
            raise ValueError(f"Invalid shared-string reference at {address}") from None
    elif kind == "inlineStr":
        value = _text(element.find(M + "is"))
    elif kind == "b":
        if raw not in ("0", "1", None):
            raise ValueError(f"Invalid boolean value at {address}")
        value = None if raw is None else raw == "1"
    else:
        value = raw
    result = {"address": address, "value": value, "raw_value": raw,
              "data_type": kind, "style_id": style_id, "formula": formula,
              "formula_cached": formula is not None and raw is not None}
    format_id = style_ids[style_id]
    if kind != "n" or raw is None:
        # Text/boolean/error content is independent of Excel number formats.
        return result
    if format_id in (None, 0):
        result["number_format"] = "General"
        return result
    try:
        from openpyxl.styles.numbers import BUILTIN_FORMATS, is_date_format
        from openpyxl.utils.datetime import from_excel, MAC_EPOCH, WINDOWS_EPOCH
    except ImportError:
        if format_id not in (None, 0):
            result["format_interpretation"] = "unavailable_without_openpyxl"
        return result
    number_format = custom_formats.get(format_id, BUILTIN_FORMATS.get(format_id, "General"))
    result["number_format"] = number_format
    if kind == "n" and raw is not None:
        if re.fullmatch(r"0{2,}", number_format):
            from decimal import Decimal
            number = Decimal(raw)
            if number >= 0 and number == number.to_integral_value():
                result["value"] = str(int(number)).zfill(len(number_format))
                result["display_identifier"] = True
        elif is_date_format(number_format):
            try:
                date = from_excel(float(raw), MAC_EPOCH if epoch_1904 else WINDOWS_EPOCH)
                result["iso_value"] = date.isoformat()
                result["excel_epoch"] = "1904" if epoch_1904 else "1900"
            except (ValueError, OverflowError):
                result["date_conversion"] = "unavailable"
    return result


def _column(address):
    return address.rstrip("0123456789")


def inspect_xlsx(file, header_rows=5):
    """Read dimensions and only the first physical rows, without a full scan.

    Sample values stay in the returned result; the CLI prints only a file path
    and sheet counts, never the sample records. This is schema discovery, not
    proof that every subsequent row can be ingested.
    """
    if type(header_rows) is not int or not 1 <= header_rows <= 100:
        raise ValueError("header_rows must be an integer between 1 and 100")
    started = time.perf_counter()
    file = Path(file).expanduser().resolve()
    before = file.stat()
    samples = []
    needed = set()
    with ZipFile(file) as archive:
        sheets, epoch_1904 = _relationships(archive)
        style_ids, custom_formats = _styles(archive)
        for name, sheet_path in sheets:
            dimension = None
            rows = []
            with archive.open(sheet_path) as stream:
                for _, element in ET.iterparse(stream, events=("end",)):
                    if element.tag == M + "dimension":
                        dimension = element.attrib.get("ref")
                    elif element.tag == M + "row":
                        number = int(element.attrib.get("r", "0"))
                        if number > header_rows:
                            break
                        if number < 1:
                            raise ValueError("Invalid worksheet physical row number")
                        for cell in element:
                            if cell.tag == M + "c" and cell.attrib.get("t") == "s":
                                node = cell.find(M + "v")
                                try:
                                    index = int(node.text)
                                except (AttributeError, TypeError, ValueError):
                                    raise ValueError("Invalid shared-string reference in sample") from None
                                if index < 0:
                                    raise ValueError("Invalid shared-string reference in sample")
                                needed.add(index)
                        rows.append(element)
                        if number == header_rows:
                            break
            samples.append({"sheet": name, "dimension": dimension, "elements": rows})
        strings = {}
        if needed:
            if "xl/sharedStrings.xml" not in archive.namelist():
                raise ValueError("Sample references a missing sharedStrings.xml")
            with archive.open("xl/sharedStrings.xml") as stream:
                for index, element in enumerate(_elements(stream, M + "si")):
                    if index in needed:
                        strings[index] = _text(element)
                        if len(strings) == len(needed):
                            break
            if len(strings) != len(needed):
                raise ValueError("Sample references an unavailable shared string")
        result_sheets = []
        for sample in samples:
            rows = []
            for element in sample["elements"]:
                number = int(element.attrib["r"])
                cells = {column: _cell(cell, strings, style_ids, custom_formats, epoch_1904)
                         for column, cell in _row_cells(element, number).items()}
                rows.append({"row_num": number,
                             "values": {column: cell["value"] for column, cell in cells.items()},
                             "cells": cells})
            result_sheets.append({"sheet": sample["sheet"], "dimension": sample["dimension"],
                                  "rows": rows})
    after = file.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("Source file changed while inspecting")
    return {"file": str(file), "operation": "inspect", "header_rows": header_rows,
            "sheets": result_sheets, "scan_complete": False,
            "coverage": {"scope": "first_physical_rows_only", "max_physical_row": header_rows,
                         "shared_strings_retained": len(strings)},
            "elapsed_seconds": round(time.perf_counter() - started, 3)}


def _row_cells(element, row_num):
    """Validate all cell addresses for a returned row or the chosen header."""
    cells = {}
    row_text = str(row_num)
    for cell in element:
        if cell.tag != M + "c":
            continue
        address = cell.attrib.get("r", "")
        match = ADDRESS.fullmatch(address)
        if match is None or match.group(2) != row_text:
            raise ValueError(f"Invalid cell address in physical row {row_num}")
        column = match.group(1)
        if column in cells:
            raise ValueError(f"Duplicate cell address at {address}")
        cells[column] = cell
    return cells


def _exact(value):
    # Never coerce string identifiers to numbers or discard their leading zeros.
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _query_xlsx_core(file, *, sheet=None, where=None, columns=None, header_row=1,
                     limit=None, progress=None, reader="compatible"):
    """Return exact matches after scanning the selected sheet completely.

    ``where`` maps column labels to allowed exact values; fields use AND and
    values use OR. ``limit`` bounds retained rows, never the scan. Values are
    strings (including exact numeric XML text), booleans or null. Physical row
    numbers and original cell addresses are retained even with a later header.
    ``progress`` receives scanned row counts every 50,000 nonempty rows.
    """
    started = time.perf_counter()
    file = Path(file).expanduser().resolve()
    before = file.stat()
    if type(header_row) is not int or header_row < 1:
        raise ValueError("header_row must be a positive physical row number")
    if limit is not None and (type(limit) is not int or limit < 0):
        raise ValueError("limit must be a nonnegative integer or None")
    where = {} if where is None else where
    if not isinstance(where, dict) or any(not isinstance(k, str) or not k for k in where):
        raise ValueError("where must map nonempty column labels to lists of exact values")
    if any(not isinstance(values, list) or not values for values in where.values()):
        raise ValueError("Each where value must be a nonempty list")
    if any(isinstance(value, (list, dict)) for values in where.values() for value in values):
        raise ValueError("where lists may contain only scalar exact values")
    allowed = {key: {_exact(value) for value in values} for key, values in where.items()}
    if columns is not None and (not isinstance(columns, (list, tuple)) or not columns
                                or any(not isinstance(c, str) or not c for c in columns)
                                or len(set(columns)) != len(columns)):
        raise ValueError("columns must contain distinct nonempty column labels")
    rows = []
    issues = []
    issue_counts = {}
    scanned = matched = missing_filter_cache = 0
    header = None
    # Evaluate unselected rows in native XML code. Iterating every one of 60M
    # cells in Python defeats the purpose of a targeted query.
    if _fast_xml is not None:
        find_cell = _fast_xml.XPath("m:c[@r=$address]", namespaces={"m": MAIN})
        has_data = _fast_xml.XPath("boolean(m:c[m:v or m:is or m:f])", namespaces={"m": MAIN})

        def locate(element, address):
            found = find_cell(element, address=address)
            if len(found) > 1:
                raise ValueError(f"Duplicate cell address at {address}")
            return found[0] if found else None
    else:
        def has_data(element):
            return any(c.find(M + "v") is not None or c.find(M + "is") is not None
                       or c.find(M + "f") is not None for c in element if c.tag == M + "c")

        def locate(element, address):
            found = element.findall(M + f"c[@r='{address}']")
            if len(found) > 1:
                raise ValueError(f"Duplicate cell address at {address}")
            return found[0] if found else None

    def issue(code, row_num, address):
        issue_counts[code] = issue_counts.get(code, 0) + 1
        if len(issues) < 100:
            issues.append({"code": code, "row_num": row_num, "address": address})

    with ZipFile(file) as archive:
        sheets, epoch_1904 = _relationships(archive)
        if sheet is None:
            if len(sheets) != 1:
                raise ValueError("Multiple worksheets: specify sheet explicitly: " + ", ".join(s[0] for s in sheets))
            sheet, sheet_path = sheets[0]
        else:
            selected = [entry for entry in sheets if entry[0] == sheet]
            if not selected:
                raise ValueError(f"Worksheet not found: {sheet}")
            _, sheet_path = selected[0]
        strings = _shared_strings(archive)
        style_ids, custom_formats = _styles(archive)
        with archive.open(sheet_path) as stream:
            last_row = 0
            row_iterator = _framed_rows(stream) if reader == "fast" else _elements(stream, M + "row")
            prefilter = None
            for item in row_iterator:
                if reader == "fast":
                    row_num, raw_frame, root_open = item
                    element = None
                else:
                    element = item
                    row_num = int(element.attrib.get("r", "0"))
                if row_num <= last_row:
                    raise ValueError("Worksheet physical row numbers must increase")
                last_row = row_num
                if row_num < header_row:
                    continue
                if row_num == header_row:
                    if element is None:
                        element = _parse_frame(raw_frame, root_open)
                    cells = _row_cells(element, row_num)
                    header = {}
                    for column, cell in cells.items():
                        value = _cell(cell, strings, style_ids, custom_formats, epoch_1904)["value"]
                        if value is None or not str(value).strip():
                            raise ValueError(f"Empty header at {cell.attrib['r']}; select the actual header_row")
                        label = str(value).strip()
                        if label in header:
                            raise ValueError(f"Duplicate header label: {label}")
                        header[label] = column
                    if not header:
                        raise ValueError("The selected header row is empty")
                    requested = list(header) if columns is None else list(columns)
                    header_columns = set(header.values())
                    unknown = (set(allowed) | set(requested)) - set(header)
                    if unknown:
                        raise ValueError("Unknown column labels: " + ", ".join(sorted(unknown)))
                    if reader == "fast" and allowed:
                        label = next(iter(allowed))
                        prefilter = _prefilter_spec(header[label], allowed[label], strings)
                    continue
                if header is None:
                    raise ValueError(f"Header physical row {header_row} was not found")
                # An explicit blank cell without a value/formula is not a record.
                nonempty = (re.search(rb'<(?:v|is|f)(?=[\s/>])', raw_frame) is not None
                            if element is None else has_data(element))
                if not nonempty:
                    continue
                scanned += 1
                if progress is not None and scanned % 50000 == 0:
                    progress(scanned)
                if element is None:
                    if prefilter is not None and _can_reject_frame(raw_frame, prefilter, row_num):
                        continue
                    element = _parse_frame(raw_frame, root_open)
                decoded = {}

                def read(label):
                    if label in decoded:
                        return decoded[label]
                    col = header[label]
                    address = f"{col}{row_num}"
                    source_cell = locate(element, address)
                    if source_cell is None:
                        value = {"address": f"{col}{row_num}", "value": None,
                                 "raw_value": None, "data_type": "blank", "style_id": 0,
                                 "formula": None, "formula_cached": False}
                    else:
                        value = _cell(source_cell, strings, style_ids, custom_formats, epoch_1904)
                    decoded[label] = value
                    if value["formula"] is not None and not value["formula_cached"]:
                        issue("formula_missing_cache", row_num, value["address"])
                    if value["data_type"] == "e":
                        issue("excel_error_cell", row_num, value["address"])
                    return value

                accepts = True
                for label, values in allowed.items():
                    cell = read(label)
                    if (cell["formula"] is not None and not cell["formula_cached"]) or cell["data_type"] == "e":
                        missing_filter_cache += 1
                        accepts = False
                        break
                    if _exact(cell["value"]) not in values:
                        accepts = False
                        break
                if not accepts:
                    continue
                matched += 1
                if limit is None or len(rows) < limit:
                    cells = _row_cells(element, row_num)
                    if any(column not in header_columns and (c.find(M + "v") is not None or c.find(M + "is") is not None
                                                           or c.find(M + "f") is not None)
                           for column, c in cells.items()):
                        raise ValueError(f"Data without a header in physical row {row_num}")
                    result_cells = {label: read(label) for label in requested}
                    rows.append({"row_num": row_num,
                                 "values": {label: c["value"] for label, c in result_cells.items()},
                                 "cells": result_cells})
        if header is None:
            raise ValueError(f"Header physical row {header_row} was not found")
    after = file.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("Source file changed while scanning; repeat against a stable snapshot")
    digest = hashlib.sha256()
    with file.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    final = file.stat()
    if (before.st_size, before.st_mtime_ns) != (final.st_size, final.st_mtime_ns):
        raise ValueError("Source file changed while hashing; repeat against a stable snapshot")
    return {"file": str(file), "sheet": sheet, "header_row": header_row,
            "source_sha256": digest.hexdigest(), "columns": requested,
            "where": where, "rows": rows,
            "coverage": {"scanned_rows": scanned, "matched_rows": matched,
                         "returned_rows": len(rows), "truncated": len(rows) < matched,
                         "scan_complete": True,
                         "filter_evaluation_complete": missing_filter_cache == 0,
                         "unavailable_filter_rows": missing_filter_cache,
                         "structure_validation_scope": "header_filter_cells_and_returned_rows",
                         "xml_well_formedness": "complete_worksheet",
                         "issue_counts": issue_counts, "issue_details_truncated": sum(issue_counts.values()) > len(issues)},
            "issues": issues, "elapsed_seconds": round(time.perf_counter() - started, 3),
            "reader": "validated_row_frames" if reader == "fast" else "compatible_xml",
            "date_epoch": "1904" if epoch_1904 else "1900"}


def _source_digest(file):
    before = file.stat()
    digest = hashlib.sha256()
    with file.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    after = file.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("Source file changed while hashing")
    return digest.hexdigest()


def query_xlsx(file, *, sheet=None, where=None, columns=None, header_row=1,
               limit=None, progress=None, reader="auto", cache_dir=None):
    """Scan one worksheet once for an entire list of exact keys.

    ``where={field: [key1, key2, ...]}`` batches all keys into one scan. Fields
    use AND and values use OR. The optional content cache avoids XML
    decompression on repeated queries while hashing source bytes on every call.
    Reader auto uses validated frame prefiltering for filtered queries and
    automatically retries supported XML with the compatible reader when needed.
    """
    started = time.perf_counter()
    file = Path(file).expanduser().resolve()
    if reader not in ("auto", "fast", "compatible"):
        raise ValueError("reader must be auto, fast, or compatible")
    # Validate arguments before reading a cache entry, including false-y inputs.
    if where is not None and (not isinstance(where, dict) or any(
        not isinstance(key, str) or not key or not isinstance(values, list) or not values
        or any(isinstance(value, (dict, list)) for value in values)
        for key, values in where.items()
    )):
        raise ValueError("where must map nonempty column labels to nonempty lists of scalar exact values")
    if columns is not None and (not isinstance(columns, (list, tuple)) or not columns
                               or any(not isinstance(c, str) or not c for c in columns)
                               or len(set(columns)) != len(columns)):
        raise ValueError("columns must contain distinct nonempty column labels")
    if type(header_row) is not int or header_row < 1 or (limit is not None and (type(limit) is not int or limit < 0)):
        raise ValueError("Invalid header_row or limit")
    columns = list(columns) if columns is not None else None
    digest = _source_digest(file)
    # OR values have set semantics. Reordering/deduplicating a result table's
    # keys must not turn the same source query into another full XML scan.
    signature_where = {name: sorted({_exact(value) for value in values},
                                   key=lambda value: (value is not None, str(value)))
                       for name, values in (where or {}).items()}
    signature = {"file": os.path.normcase(str(file)), "source_sha256": digest,
                 "sheet": sheet, "header_row": header_row, "where": signature_where,
                 "columns": columns, "limit": limit, "reader": reader,
                 "reader_version": READER_VERSION,
                 "validation": "complete_xml_no_dtd_header_filter_returned_cells"}
    key = hashlib.sha256(json.dumps(signature, sort_keys=True, ensure_ascii=False, allow_nan=False,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()
    cache_file = None
    if cache_dir is not None:
        cache_file = Path(cache_dir).expanduser().resolve() / (key + '.json')
        if cache_file.is_file():
            try:
                cached = json.loads(cache_file.read_text(encoding='utf-8'))
                if cached.get('signature') == signature and isinstance(cached.get('result'), dict):
                    result = cached['result']
                    if result.get('source_sha256') == digest and result.get('coverage', {}).get('scan_complete') is True:
                        result['elapsed_seconds'] = round(time.perf_counter() - started, 3)
                        result['cache'] = {"enabled": True, "hit": True, "key": key,
                                           "path": str(cache_file), "reader_version": READER_VERSION}
                        result['where'] = where or {}
                        return result
            except (OSError, ValueError, TypeError):
                pass
    selected_reader = "fast" if reader == "fast" or (reader == "auto" and where) else "compatible"
    fallback_reason = None
    try:
        result = _query_xlsx_core(file, sheet=sheet, where=where, columns=columns,
                                  header_row=header_row, limit=limit, progress=progress,
                                  reader=selected_reader)
    except _UnsupportedFrames as exc:
        fallback_reason = str(exc)
        result = _query_xlsx_core(file, sheet=sheet, where=where, columns=columns,
                                  header_row=header_row, limit=limit, progress=progress,
                                  reader="compatible")
    if result['source_sha256'] != digest:
        raise ValueError("Source file changed during query; no result was cached")
    if fallback_reason is not None:
        result['reader_fallback_reason'] = fallback_reason
    result['cold_scan_elapsed_seconds'] = result['elapsed_seconds']
    result['elapsed_seconds'] = round(time.perf_counter() - started, 3)
    result['cache'] = {"enabled": cache_file is not None, "hit": False, "key": key,
                       "path": str(cache_file) if cache_file is not None else None,
                       "reader_version": READER_VERSION}
    if cache_file is not None:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=cache_file.parent,
                                             prefix='.' + key + '-', suffix='.tmp', delete=False) as stream:
                temporary = Path(stream.name)
                json.dump({"signature": signature, "result": result}, stream, ensure_ascii=False)
                stream.write('\n')
            os.replace(temporary, cache_file)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    # Include atomic cache serialization in this invocation's wall time. The
    # persisted copy's elapsed value is replaced on every future cache hit.
    result['elapsed_seconds'] = round(time.perf_counter() - started, 3)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--inspect", action="store_true", help="Inspect sheet dimensions and only the first physical rows")
    parser.add_argument("--header-rows", type=int, default=5, help="Physical sample-row limit for --inspect (default 5)")
    parser.add_argument("--sheet")
    parser.add_argument("--where-json", type=Path, help="JSON object mapping fields to lists of exact values")
    parser.add_argument("--columns", nargs="+")
    parser.add_argument("--header-row", type=int, default=1)
    parser.add_argument("--limit", type=int, help="Maximum returned rows; the whole sheet is still scanned")
    parser.add_argument("--reader", choices=("auto", "fast", "compatible"), default="auto")
    parser.add_argument("--cache-dir", type=Path, default=Path('.dd_cache/xlsx_query'))
    parser.add_argument("--no-cache", action="store_true", help="Disable the content-keyed query cache")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.inspect and (args.where_json is not None or args.columns is not None
                         or args.limit is not None or args.sheet is not None or args.header_row != 1):
        parser.error("--inspect samples all sheets; use --header-rows, without query filters")
    try:
        where = json.loads(args.where_json.read_text(encoding="utf-8-sig")) if args.where_json else None
        result = inspect_xlsx(args.file, header_rows=args.header_rows) if args.inspect else query_xlsx(args.file, sheet=args.sheet, where=where,
                            columns=args.columns, header_row=args.header_row, limit=args.limit,
                            reader=args.reader, cache_dir=None if args.no_cache else args.cache_dir,
                            progress=lambda count: print(f"Scanned {count} rows", file=sys.stderr, flush=True))
        output = args.out.expanduser().resolve()
        if output == args.file.expanduser().resolve() or (args.where_json and output == args.where_json.expanduser().resolve()):
            raise ValueError("Output must not overwrite the workbook or filter input")
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.suffix.lower() == ".jsonl":
            with output.open("w", encoding="utf-8", newline="\n") as handle:
                metadata = {k: v for k, v in result.items() if k != "rows"}
                handle.write(json.dumps({"metadata": metadata}, ensure_ascii=False) + "\n")
                for row in result.get("rows", []):
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        else:
            output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError, KeyError, BadZipFile, ET.ParseError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"out": str(output), "coverage": result["coverage"],
                      "elapsed_seconds": result["elapsed_seconds"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
