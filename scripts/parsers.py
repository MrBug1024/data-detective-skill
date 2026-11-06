"""
parsers.py — 多格式文件解析器
将各种文件格式解析为统一的"可寻址单元"列表
每个单元包含：文件名、行号/段落号/页码、字段名、值、字符偏移、原文片段
"""
import os
import re
import json
import hashlib
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any, Optional, Tuple
from pathlib import Path
from ingestion_support import read_text, ocr_sidecar, sql_records


@dataclass
class AddressableUnit:
    """业务可寻址单元 - 数据中的最小可定位单位"""
    unit_id: str                    # 唯一ID
    source_file: str                # 来源文件路径
    file_type: str                  # xlsx/csv/pdf/docx/txt/md
    row_num: Optional[int]          # 行号（结构化数据）
    page_num: Optional[int]         # 页码（非结构化）
    paragraph_num: Optional[int]    # 段落号
    char_offset: Optional[int]      # 字符偏移
    sheet_name: Optional[str]       # Excel sheet名
    field_names: List[str]          # 字段名列表
    field_values: List[Any]         # 字段值列表
    raw_text: str                   # 原文片段
    is_rule: bool = False           # 是否为规则/定义节点
    ocr_confidence: Optional[float] = None  # OCR置信度（扫描件）
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self):
        d = asdict(self)
        # 确保values可序列化
        d['field_values'] = [str(v) if not isinstance(v, (str, int, float, bool, type(None))) else v
                              for v in d['field_values']]
        # Formulas may cache a date/time and cell metadata must be just as
        # serializable as field_values when exported to a report.
        return json.loads(json.dumps(d, ensure_ascii=False, default=str))

    def get_field(self, name: str) -> Optional[Any]:
        """按字段名获取值（大小写不敏感）"""
        name_lower = name.lower().strip()
        for fn, fv in zip(self.field_names, self.field_values):
            if fn.lower().strip() == name_lower:
                return fv
        return None

    def get_all_values_str(self) -> List[str]:
        """获取所有字段值的字符串形式"""
        result = []
        for v in self.field_values:
            if v is not None:
                s = str(v).strip()
                if s:
                    result.append(s)
        return result


def _make_unit_id(source_file: str, row_num: Optional[int], paragraph_num: Optional[int], extra: str = "") -> str:
    key = f"{source_file}|{row_num}|{paragraph_num}|{extra}"
    return hashlib.md5(key.encode()).hexdigest()[:12]


class ExcelParser:
    """Read physical rows, formulas, caches and cell addresses without calculation."""

    def parse(self, filepath: str) -> List[AddressableUnit]:
        self.diagnostics = []
        if Path(filepath).suffix.lower() == '.xls':
            return self._parse_xls(filepath)
        import openpyxl
        units = []
        formulas = openpyxl.load_workbook(filepath, read_only=True, data_only=False)
        cached = openpyxl.load_workbook(filepath, read_only=True, data_only=True)
        try:
            for sheet in formulas:
                value_sheet = cached[sheet.title]
                rows = sheet.iter_rows()
                headers = next(rows, ())
                if not headers:
                    continue
                # Ignore only unused trailing columns; internal empty headers
                # must not silently acquire another field's values.
                last = max((i + 1 for i, c in enumerate(headers) if c.value is not None), default=0)
                if not last:
                    if sheet.max_row > 1:
                        self.diagnostics.append({'code': 'xlsx_missing_header', 'severity': 'warning', 'sheet': sheet.title,
                                                 'message': '工作表首行没有字段名；未猜测表头，需定向预处理'})
                    continue
                names = [str(c.value).strip() if c.value is not None else '' for c in headers[:last]]
                _validate_headers(names, 'Excel')
                cached_rows = value_sheet.iter_rows(min_row=2)
                for row_num, (row, cache_row) in enumerate(zip(rows, cached_rows), 2):
                    if all(c.value is None for c in row):
                        continue
                    if any(c.value is not None for c in row[last:]):
                        raise ValueError(f'Excel {sheet.title} 第{row_num}行存在无表头数据')
                    values, cell_info = [], {}
                    for column_num, (name, cell, cache) in enumerate(zip(names, row[:last], cache_row[:last]), 1):
                        from openpyxl.utils import get_column_letter
                        address = getattr(cell, 'coordinate', f'{get_column_letter(column_num)}{row_num}')
                        info = {'address': address, 'number_format': cell.number_format}
                        value = cache.value if cell.data_type == 'f' else cell.value
                        if cell.data_type == 'f':
                            info.update(formula=cell.value, cached_value=cache.value, formula_evaluated=False)
                            if cache.value is None:
                                self.diagnostics.append({'code': 'xlsx_missing_formula_cache', 'severity': 'warning',
                                                         'sheet': sheet.title, 'cell': address,
                                                         'message': '公式无缓存值，本工具不计算Excel公式，须由可信计算引擎重算'})
                        # A numeric cell with a pure zero format is an explicit
                        # displayed identifier; retain its underlying number.
                        if type(value) in (int, float) and value >= 0 and float(value).is_integer() and re.fullmatch('0{2,}', cell.number_format or ''):
                            info['underlying_value'] = value
                            value = str(int(value)).zfill(len(cell.number_format))
                            info['display_value'] = value
                        values.append(None if value == '' else value)
                        cell_info[name] = info
                    raw = ' | '.join(f'{name}={value}' if value is not None else f'{name}=[{cell_info[name]["formula"]}; 无缓存值]'
                                     for name, value in zip(names, values) if value is not None or 'formula' in cell_info[name])
                    units.append(AddressableUnit(
                        _make_unit_id(filepath, row_num, None, sheet.title), filepath, 'xlsx', row_num,
                        None, None, None, sheet.title, names, values, raw,
                        metadata={'cells': cell_info, 'location_kind': 'excel_physical_row',
                                  'hidden_row': bool(sheet.row_dimensions[row_num].hidden) if hasattr(sheet, 'row_dimensions') else None}))
        finally:
            formulas.close()
            cached.close()
        return units

    def _parse_xls(self, filepath):
        import pandas as pd
        units = []
        def read_sheets():
            with pd.ExcelFile(filepath) as xl:
                for name in xl.sheet_names:
                    yield name, xl.parse(name, dtype=object, keep_default_na=False)

        for sheet_name, df in read_sheets():
            col_names = [str(c).strip() for c in df.columns.tolist()]
            _validate_headers(col_names, 'Excel')

            for row_num, row in enumerate(df.itertuples(index=False, name=None), start=2):
                values = [None if pd.isna(v) or v == '' else v for v in row]
                raw = " | ".join(f"{col_names[i]}={values[i]}" for i in range(len(col_names)) if values[i] is not None)

                unit = AddressableUnit(
                    unit_id=_make_unit_id(filepath, row_num, None, sheet_name),
                    source_file=filepath,
                    file_type=Path(filepath).suffix.lower().lstrip('.'),
                    row_num=row_num,
                    page_num=None,
                    paragraph_num=None,
                    char_offset=None,
                    sheet_name=sheet_name,
                    field_names=col_names,
                    field_values=values,
                    raw_text=raw,
                )
                units.append(unit)

        return units


class CSVParser:
    """Parse CSV with the standard library, preserving literal identifiers."""

    def parse(self, filepath: str) -> List[AddressableUnit]:
        import csv
        import io
        self.diagnostics = []
        units = []
        content, encoding = read_text(filepath)
        if '\x00' in content:
            raise ValueError('CSV含NUL，编码可能错误或并非文本表格')
        delimiter = '\t' if Path(filepath).suffix.lower() == '.tsv' else ','
        try:
            delimiter = csv.Sniffer().sniff(content[:65536], delimiters=',;\t|').delimiter
        except csv.Error:
            # A single-column CSV is valid; strict parsing below still rejects
            # malformed quoting and field counts.
            pass
        offsets, offset = [], 0
        for line in content.splitlines(keepends=True):
            offsets.append(offset)
            offset += len(line)
        with io.StringIO(content, newline='') as handle:
            reader = csv.reader(handle, delimiter=delimiter, strict=True)
            col_names = [c.strip() for c in next(reader, [])]
            if not col_names:
                return []
            _validate_headers(col_names, 'CSV')
            record_num = 1
            while True:
                row_num = reader.line_num + 1
                try:
                    row = next(reader)
                except StopIteration:
                    break
                if not row:
                    continue
                record_num += 1
                if len(row) > len(col_names):
                    raise ValueError(f'CSV第{row_num}条记录多于表头列数')
                values = [None if v == '' else v for v in row] + [None] * (len(col_names) - len(row))
                raw = " | ".join(f"{col_names[i]}={values[i]}" for i in range(len(col_names)) if values[i] is not None)
                unit = AddressableUnit(
                    unit_id=_make_unit_id(filepath, row_num, None),
                    source_file=filepath,
                    file_type=Path(filepath).suffix.lower().lstrip('.'),
                    row_num=row_num,
                    page_num=None,
                    paragraph_num=None,
                    char_offset=offsets[row_num - 1],
                    sheet_name=None,
                    field_names=col_names,
                    field_values=values,
                    raw_text=raw,
                    metadata={'encoding': encoding, 'delimiter': delimiter, 'record_num': record_num,
                              'line_start': row_num, 'line_end': reader.line_num,
                              'raw_record': content[offsets[row_num - 1]:offsets[reader.line_num] if reader.line_num < len(offsets) else len(content)]},
                )
                units.append(unit)

        return units


class PDFParser:
    """解析 .pdf 文件，按页和段落切分"""

    def parse(self, filepath: str) -> List[AddressableUnit]:
        self.diagnostics = []
        try:
            import pdfplumber
        except ImportError:
            raise RuntimeError("缺少pdfplumber，无法读取PDF；安装依赖或先预处理") from None

        units = []
        with pdfplumber.open(filepath) as pdf:
            regions, issues = ocr_sidecar(filepath, len(pdf.pages))
            self.diagnostics.extend(issues)
            ocr_pages = {region['page_num'] for region in regions}
            for page_num, page in enumerate(pdf.pages, start=1):
                text = page.extract_text() or ""
                if page_num in ocr_pages:
                    units.extend(_ocr_units(filepath, [r for r in regions if r['page_num'] == page_num], 'pdf'))
                elif page.images:
                    self.diagnostics.append({'code': 'pdf_images_not_transcribed', 'severity': 'warning', 'page_num': page_num,
                                             'message': '此PDF页含图像；数字文本提取不能证明覆盖图中内容，需OCR/视觉复核'})
                if not text.strip():
                    if page_num not in ocr_pages:
                        self.diagnostics.append({'code': 'pdf_no_text', 'severity': 'warning', 'page_num': page_num,
                                                 'message': '此PDF页无可提取文本，可能为空白或扫描页；未完成该页检索'})
                    continue
                paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()]

                char_offset = 0
                for para_num, para_text in enumerate(paragraphs, start=1):
                    # 检测是否为规则/定义节点
                    is_rule = _is_rule_text(para_text)

                    char_offset = text.find(para_text, char_offset)
                    unit = AddressableUnit(
                        unit_id=_make_unit_id(filepath, None, para_num, str(page_num)),
                        source_file=filepath,
                        file_type="pdf",
                        row_num=None,
                        page_num=page_num,
                        paragraph_num=para_num,
                        char_offset=char_offset,
                        sheet_name=None,
                        field_names=["content"],
                        field_values=[para_text],
                        raw_text=para_text,
                        is_rule=is_rule,
                        metadata={'offset_scope': 'page_extracted_text', 'page_size_points': [float(page.width), float(page.height)],
                                  'extraction': 'pdfplumber_text', 'table_structure_recovered': False},
                    )
                    units.append(unit)
                    char_offset += len(para_text)

        return units


class DocxParser:
    """Read paragraphs and table rows in body order, with OOXML locations."""

    def parse(self, filepath: str) -> List[AddressableUnit]:
        self.diagnostics = []
        try:
            from docx import Document
        except ImportError:
            raise RuntimeError("缺少python-docx，无法读取DOCX；安装依赖或先预处理") from None

        units = []
        doc = Document(filepath)
        from docx.text.paragraph import Paragraph
        from docx.table import Table
        from docx.oxml.ns import qn
        para_num = table_num = 0
        for block_num, child in enumerate(doc.element.body, 1):
            if child.tag == qn('w:p'):
                para_num += 1
                paragraph = Paragraph(child, doc)
                text = paragraph.text.strip()
                if text:
                    units.append(AddressableUnit(
                        _make_unit_id(filepath, None, para_num), filepath, 'docx', None, None,
                        para_num, None, None, ['content'], [text], text, _is_rule_text(text),
                        metadata={'style': paragraph.style.name if paragraph.style else '',
                                  'body_block': block_num, 'xml_path': f'/word/document.xml/body/p[{para_num}]'}))
            elif child.tag == qn('w:tbl'):
                table_num += 1
                table = Table(child, doc)
                all_rows = [[cell.text.strip() for cell in row.cells] for row in table.rows]
                if not all_rows:
                    continue
                header = all_rows[0]
                has_header = bool(header) and all(header) and len({x.casefold() for x in header}) == len(header)
                names = header if has_header else [f'column{i}' for i in range(1, len(header) + 1)]
                # Keep every table row, including its potential header, so a
                # label-only table cannot disappear through header guessing.
                for row_num, values in enumerate(all_rows, 1):
                    if not any(values):
                        continue
                    row_names = names if len(names) == len(values) else [f'column{i}' for i in range(1, len(values) + 1)]
                    raw = ' | '.join(f'{name}={value}' for name, value in zip(row_names, values) if value)
                    units.append(AddressableUnit(
                        _make_unit_id(filepath, row_num, None, f'table{table_num}'), filepath, 'docx', row_num,
                        None, None, None, f'table{table_num}', row_names, values, raw,
                        metadata={'body_block': block_num, 'table_num': table_num, 'table_row': row_num,
                                  'table': f'table{table_num}', 'structured': not (has_header and row_num == 1),
                                  'header_inferred': has_header, 'is_header_row': has_header and row_num == 1,
                                  'xml_path': f'/word/document.xml/body/tbl[{table_num}]/tr[{row_num}]',
                                  'cells': {name: {'table': table_num, 'row': row_num, 'column': i}
                                            for i, name in enumerate(row_names, 1)}}))
                if child.xpath('.//w:tbl'):
                    self.diagnostics.append({'code': 'docx_nested_table', 'severity': 'warning',
                                             'table_num': table_num, 'message': '嵌套表格未作为独立表恢复，须定向复核'})
        # DOCX page numbers depend on layout and are deliberately not invented.
        if doc.element.xpath('.//w:txbxContent'):
            self.diagnostics.append({'code': 'docx_textbox', 'severity': 'warning', 'message': '文档含文本框，主体提取未覆盖其内容'})
        for section in doc.sections:
            if any(p.text.strip() for part in (section.header, section.footer) for p in part.paragraphs):
                self.diagnostics.append({'code': 'docx_header_footer', 'severity': 'warning', 'message': '页眉页脚未加入主体单元，须按需复核'})
                break
        return units


class TextParser:
    """解析 .txt / .md 文件，按段落/行切分"""

    def parse(self, filepath: str) -> List[AddressableUnit]:
        self.diagnostics = []
        units = []
        filename = os.path.basename(filepath)
        file_ext = Path(filepath).suffix.lower()

        content, encoding = read_text(filepath)

        # Retain exact offsets across variable blank lines and CRLF. A running
        # line count avoids rescanning the entire prefix for every paragraph.
        line_num = 1
        for para_num, match in enumerate(re.finditer(r'([\s\S]*?)(?:\r?\n[ \t]*\r?\n|\Z)', content), start=1):
            para_text = match.group(1)
            text = para_text.strip()
            leading = para_text[:len(para_text) - len(para_text.lstrip())]
            first_line = line_num + leading.count('\n')
            line_num += match.group().count('\n')
            if not text:
                continue
            char_offset = match.start(1) + len(leading)

            is_rule = _is_rule_text(text)

            unit = AddressableUnit(
                unit_id=_make_unit_id(filepath, first_line, para_num),
                source_file=filepath,
                file_type="md" if file_ext == ".md" else "txt",
                row_num=first_line,
                page_num=None,
                paragraph_num=para_num,
                char_offset=char_offset,
                sheet_name=None,
                field_names=["content"],
                field_values=[text],
                raw_text=text,
                is_rule=is_rule,
                metadata={'encoding': encoding, 'offset_scope': 'decoded_source_text',
                          'line_end': first_line + text.count('\n')},
            )
            units.append(unit)

        return units


def _validate_headers(names, kind):
    if any(not c for c in names) or len({c.casefold() for c in names}) != len(names):
        raise ValueError(f'{kind}字段名必须非空且唯一，避免字段归属歧义')


def _ocr_units(filepath, regions, file_type):
    units = []
    for region in regions:
        units.append(AddressableUnit(
            _make_unit_id(filepath, None, region['region_num'], f'ocr-page{region["page_num"]}'),
            filepath, file_type, None, region['page_num'], region['region_num'], None, None,
            ['content'], [region['text']], region['text'], _is_rule_text(region['text']),
            region['confidence'], metadata={'extraction': 'ocr_sidecar', 'bbox': region['bbox'],
                                          'bbox_units': 'rendered_image_pixels', 'image_size': region['image_size'],
                                          'source_sha256': region['source_sha256'],
                                          'ocr_sidecar': region['sidecar'], 'ocr_engine': region['engine'],
                                          'ocr_verified': False}))
    return units


class ImageParser:
    """Accept source-bound OCR results, keeping unprocessed images explicit."""
    def parse(self, filepath):
        regions, self.diagnostics = ocr_sidecar(filepath)
        if regions:
            from PIL import Image
            with Image.open(filepath) as image:
                for region in regions:
                    page_index = region['page_num'] - 1
                    if page_index >= getattr(image, 'n_frames', 1):
                        raise ValueError('OCR sidecar 页号超出图像帧数')
                    image.seek(page_index)
                    if list(image.size) != region['image_size']:
                        raise ValueError('OCR sidecar width/height与原图像素尺寸不符')
        if not regions:
            self.diagnostics.append({'code': 'image_needs_ocr', 'severity': 'error',
                                     'message': '图像尚未OCR/视觉转录；需提供绑定原图SHA256、页号和bbox的 .ocr.json'})
        return _ocr_units(filepath, regions, Path(filepath).suffix.lower().lstrip('.'))


class LegacyWordParser:
    def parse(self, filepath):
        self.diagnostics = [{'code': 'legacy_word_requires_conversion', 'severity': 'error',
                             'message': '旧版二进制DOC须先由可信Word/LibreOffice转换为DOCX；本工具不猜测文本或执行宏'}]
        return []


class SQLParser:
    """Extract literal INSERT VALUES and schema/text; never connect or execute."""
    def parse(self, filepath):
        content, encoding = read_text(filepath)
        records, self.diagnostics = sql_records(content)
        units = []
        for record in records:
            row = record['kind'] == 'row'
            fields = record.get('columns', ['content'])
            values = record.get('values', [record['statement']])
            raw = record['tuple_sql'] if row else record['statement']
            metadata = {'encoding': encoding, 'extraction': 'sql_static', 'executed': False,
                        'table': record.get('table'), 'structured': row,
                        'statement_num': record['statement_num'], 'line_end': record['line_end'],
                        'offset_scope': 'decoded_source_text',
                        'statement_span': [record['statement_offset'], record['statement_end']]}
            for key in ('tuple_num', 'column_origin', 'unparsed_fields'):
                if key in record:
                    metadata[key] = record[key]
            units.append(AddressableUnit(
                _make_unit_id(filepath, record['row_num'], None, f'sql{record["statement_num"]}:{record.get("tuple_num", 0)}'),
                filepath, 'sql', record['row_num'], None, None, record['char_offset'], record.get('table'),
                fields, values, raw, is_rule=not row, metadata=metadata))
        return units


def _is_rule_text(text: str) -> bool:
    """判断文本是否为规则/定义节点"""
    rule_patterns = [
        r'规则[：:]', r'定义[：:]', r'计算[：:]', r'公式[：:]',
        r'rule[:\s]', r'define[:\s]', r'formula[:\s]', r'calculate[:\s]',
        r'if\s+.+then', r'当.+时', r'凡是.+则',
        r'=\s*SUM', r'=\s*AVG', r'total\s*=',
    ]
    text_lower = text.lower()
    for pattern in rule_patterns:
        if re.search(pattern, text_lower, re.IGNORECASE):
            return True
    return False


class FileParser:
    """统一文件解析入口"""

    PARSERS = {
        '.xlsx': ExcelParser,
        '.xls': ExcelParser,
        '.csv': CSVParser,
        '.tsv': CSVParser,
        '.pdf': PDFParser,
        '.docx': DocxParser,
        '.doc': LegacyWordParser,
        '.txt': TextParser,
        '.md': TextParser,
        '.sql': SQLParser,
        **{ext: ImageParser for ext in ('.png', '.jpg', '.jpeg', '.tif', '.tiff', '.bmp', '.webp')},
    }

    def parse(self, filepath: str) -> List[AddressableUnit]:
        self.last_diagnostics = []
        filepath = os.path.abspath(filepath)
        ext = Path(filepath).suffix.lower()
        parser_cls = self.PARSERS.get(ext)
        if parser_cls is None:
            print(f"[WARN] Unsupported file type: {ext}, skipping {filepath}")
            self.last_diagnostics = [{'code': 'unsupported_format', 'severity': 'error',
                                      'message': f'未支持格式 {ext}，未检索该文件内容'}]
            return []

        try:
            parser = parser_cls()
            units = parser.parse(filepath)
            self.last_diagnostics = getattr(parser, 'diagnostics', [])
            if not units and not self.last_diagnostics:
                self.last_diagnostics = [{'code': 'empty_source', 'severity': 'warning',
                                          'message': '来源未提取到任何可检索单元；不能据此作否定判断'}]
            print(f"[INFO] Parsed {filepath}: {len(units)} units")
            return units
        except Exception as e:
            # Parsing failures must not become a successful 'no evidence' report
            # or a cached empty index. Unsupported formats are handled above.
            raise RuntimeError(f"无法解析来源 {filepath} ({type(e).__name__})；请检查文件和依赖") from e

    def parse_result_row(self, filepath: str, row_num: int, sheet_name: Optional[str] = None) -> Optional[AddressableUnit]:
        """解析结果表的特定行（1-based）"""
        if row_num < 2:
            raise ValueError('结果行号从 2 开始（第 1 行为表头）')
        units = self.parse(filepath)
        candidates = [u for u in units if u.row_num == row_num and (sheet_name is None or u.sheet_name == sheet_name)]
        if len(candidates) > 1:
            raise ValueError('多个工作表包含该行，请通过 --sheet / sheet_name 指定结果工作表')
        return candidates[0] if candidates else None
