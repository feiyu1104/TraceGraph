import csv
from dataclasses import dataclass
from io import BytesIO, StringIO
import json
from pathlib import Path
import re


SUPPORTED_SUFFIXES = {".csv", ".json", ".jsonl", ".md", ".pdf", ".txt"}
UNSUPPORTED_DOCUMENT_MESSAGE = "当前仅支持 TXT、Markdown、JSON、JSONL、CSV 和 PDF 文档"
_HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


class UnsupportedDocumentError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ChunkDraft:
    content: str
    locator: str


def read_text_document(path: Path) -> str:
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise UnsupportedDocumentError(UNSUPPORTED_DOCUMENT_MESSAGE)

    if path.suffix.lower() == ".pdf":
        return _read_pdf(path)

    raw = path.read_bytes()
    return read_document_bytes(path.name, raw)


def read_document_bytes(source_name: str, raw: bytes) -> str:
    suffix = Path(source_name).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise UnsupportedDocumentError(UNSUPPORTED_DOCUMENT_MESSAGE)
    if suffix == ".pdf":
        return _read_pdf(BytesIO(raw))

    text = _decode_text(raw)
    if suffix in {".json", ".jsonl"}:
        return _json_text_to_markdown(text)
    if suffix == ".csv":
        return _csv_text_to_markdown(text)
    return text


def _decode_text(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("无法使用 UTF-8 或 GB18030 解码文档")


def _json_text_to_markdown(text: str) -> str:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        values = [json.loads(line) for line in text.splitlines() if line.strip()]
        value = values
    return _json_value_to_markdown(value)


def _json_value_to_markdown(value: object, level: int = 1) -> str:
    if isinstance(value, dict):
        sections = []
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                sections.append(f"{'#' * min(level, 6)} {key}")
                sections.append(_json_value_to_markdown(item, level + 1))
            else:
                sections.append(f"{key}: {item}")
        return "\n\n".join(filter(None, sections))
    if isinstance(value, list):
        return "\n\n".join(
            f"{'#' * min(level, 6)} 记录 {index}\n\n"
            f"{_json_value_to_markdown(item, level + 1)}"
            for index, item in enumerate(value, start=1)
        )
    return str(value)


def _csv_text_to_markdown(text: str) -> str:
    rows = tuple(csv.DictReader(StringIO(text)))
    if not rows:
        raise ValueError("CSV 文档没有可入库记录")
    sections = []
    for index, row in enumerate(rows, start=1):
        sections.append(f"# 记录 {index}")
        sections.append("\n".join(f"{key}: {value}" for key, value in row.items()))
    return "\n\n".join(sections)


def _read_pdf(source: object) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as error:
        raise RuntimeError("解析 PDF 需要安装项目依赖 pypdf") from error
    reader = PdfReader(source)
    sections = []
    for index, page in enumerate(reader.pages, start=1):
        content = (page.extract_text() or "").strip()
        if content:
            sections.append(f"# 第 {index} 页\n\n{content}")
    if not sections:
        raise ValueError("PDF 没有可提取文本；扫描件需要先进行 OCR")
    return "\n\n".join(sections)


def split_text(text: str, max_chars: int) -> tuple[ChunkDraft, ...]:
    if max_chars < 20:
        raise ValueError("max_chars 不能小于 20")
    if not text.strip():
        raise ValueError("文档内容不能为空")

    headings: list[str] = []
    blocks: list[tuple[str, str]] = []
    paragraph_lines: list[str] = []

    def locator() -> str:
        return " > ".join(headings) if headings else "全文"

    def flush_paragraph() -> None:
        if paragraph_lines:
            paragraph = " ".join(line.strip() for line in paragraph_lines).strip()
            if paragraph:
                blocks.append((locator(), paragraph))
            paragraph_lines.clear()

    for raw_line in text.splitlines():
        line = raw_line.strip()
        heading = _HEADING_PATTERN.match(line)
        if heading:
            flush_paragraph()
            level = len(heading.group(1))
            del headings[level - 1 :]
            headings.append(heading.group(2))
        elif line:
            paragraph_lines.append(line)
        else:
            flush_paragraph()
    flush_paragraph()

    drafts: list[ChunkDraft] = []
    current_locator = ""
    current_content = ""

    def flush_chunk() -> None:
        nonlocal current_content
        if current_content:
            drafts.append(ChunkDraft(content=current_content, locator=current_locator))
            current_content = ""

    for block_locator, paragraph in blocks:
        if len(paragraph) > max_chars:
            flush_chunk()
            for start in range(0, len(paragraph), max_chars):
                drafts.append(
                    ChunkDraft(
                        content=paragraph[start : start + max_chars],
                        locator=block_locator,
                    )
                )
            continue

        candidate = f"{current_content}\n\n{paragraph}" if current_content else paragraph
        if current_content and (
            block_locator != current_locator or len(candidate) > max_chars
        ):
            flush_chunk()
            candidate = paragraph
        current_locator = block_locator
        current_content = candidate

    flush_chunk()
    if not drafts:
        raise ValueError("文档没有可入库正文")
    return tuple(drafts)
