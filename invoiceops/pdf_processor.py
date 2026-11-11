"""Reliable text extraction from digital and scanned invoice PDFs.

The processor extracts text page-by-page. Pages with insufficient embedded
text are rendered and passed through Tesseract OCR. It deliberately does not
interpret invoice fields; that belongs to the separate extraction service.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pymupdf as fitz  # PyMuPDF >= 1.24.3 (the legacy `fitz` import name is deprecated)
from PIL import Image

from .config import DEFAULT_POLICY, get_settings
from .schemas import DocumentType, ExtractionMetadata


class PDFProcessingError(Exception):
    """Base exception for expected PDF processing failures."""


class InvalidPDFError(PDFProcessingError):
    """Raised when the supplied bytes are not a readable PDF."""


class EncryptedPDFError(PDFProcessingError):
    """Raised when a PDF requires a password that was not supplied."""


class PDFLimitExceededError(PDFProcessingError):
    """Raised when a PDF exceeds configured size or page limits."""


class OCRUnavailableError(PDFProcessingError):
    """Raised when OCR is required but the Tesseract executable is missing."""


@dataclass(frozen=True)
class PDFProcessorConfig:
    """Runtime limits and OCR settings for PDF processing."""

    min_digital_text_chars_per_page: int = 30
    ocr_dpi: int = 200
    max_file_size_bytes: int = 20 * 1024 * 1024
    max_pages: int = 30
    ocr_language: str = "eng"
    tesseract_config: str = "--psm 6"
    # Path to the tesseract binary; falls back to TESSERACT_CMD, then PATH.
    tesseract_cmd: str | None = None
    # OCR pages whose mean word confidence (0-100) is below this get a warning.
    min_ocr_confidence: float = DEFAULT_POLICY.min_ocr_confidence
    # A page mostly covered by images with only a little text is treated as a
    # scan even if a text layer exists (e.g. a scanner-app footer).
    scan_image_coverage: float = 0.5
    scan_text_ceiling_chars: int = 200
    # Rebuild table rows from word positions so "Subtotal: | 25,000.00" stays
    # on one line instead of one cell per line.
    preserve_layout: bool = True

    def __post_init__(self) -> None:
        if self.min_digital_text_chars_per_page < 1:
            raise ValueError("min_digital_text_chars_per_page must be positive")
        if self.ocr_dpi < 72 or self.ocr_dpi > 600:
            raise ValueError("ocr_dpi must be between 72 and 600")
        if self.max_file_size_bytes < 1:
            raise ValueError("max_file_size_bytes must be positive")
        if self.max_pages < 1:
            raise ValueError("max_pages must be positive")


@dataclass(frozen=True)
class PageText:
    page_number: int
    text: str
    method: Literal["PDF_TEXT", "OCR", "EMPTY"]
    # Mean OCR word confidence (0-100); None for non-OCR pages.
    ocr_confidence: float | None = None

    @property
    def character_count(self) -> int:
        return len(self.text)


@dataclass(frozen=True)
class DocumentTextResult:
    file_name: str
    file_sha256: str
    document_type: DocumentType
    page_count: int
    pages: tuple[PageText, ...]
    full_text: str
    warnings: tuple[str, ...]

    @property
    def metadata(self) -> ExtractionMetadata:
        """Metadata to attach to the later structured invoice extraction."""
        confidences = [p.ocr_confidence for p in self.pages if p.ocr_confidence is not None]
        return ExtractionMetadata(
            document_type=self.document_type,
            page_count=self.page_count,
            file_sha256=self.file_sha256,
            warnings=list(self.warnings),
            ocr_min_confidence=min(confidences) if confidences else None,
        )


def clean_extracted_text(text: str) -> str:
    """Normalise PDF/OCR whitespace without flattening meaningful line breaks."""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\x0c", "\n").replace("\u00a0", " ")
    # Remove control characters other than tabs and newlines.
    text = "".join(ch for ch in text if ch in "\n\t" or ord(ch) >= 32)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\t ]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _join_row(items: list[tuple[float, float, float, str]]) -> str:
    """Join one visual row of words; wide gaps become ' | ' column separators.

    ``items`` are (x0, x1, height, text). A normal word space is ~0.3 of the
    text height; table column gaps are many times larger.
    """
    items = sorted(items, key=lambda item: item[0])
    parts = [items[0][3]]
    for previous, current in zip(items, items[1:]):
        gap = current[0] - previous[1]
        height = max(previous[2], current[2], 1.0)
        parts.append(" | " if gap > 1.5 * height else " ")
        parts.append(current[3])
    return "".join(parts)


def _group_rows(words: list[tuple[float, float, float, float, str]]) -> list[list[tuple[float, float, float, str]]]:
    """Cluster words (x0, y0, x1, y1, text) into rows by vertical centre."""
    ordered = sorted(words, key=lambda w: ((w[1] + w[3]) / 2, w[0]))
    rows: list[list[tuple[float, float, float, str]]] = []
    row_centres: list[float] = []
    for x0, y0, x1, y1, word in ordered:
        centre, height = (y0 + y1) / 2, max(y1 - y0, 1.0)
        if rows and abs(centre - row_centres[-1]) <= 0.5 * height:
            rows[-1].append((x0, x1, height, word))
            row_centres[-1] = (row_centres[-1] * (len(rows[-1]) - 1) + centre) / len(rows[-1])
        else:
            rows.append([(x0, x1, height, word)])
            row_centres.append(centre)
    return rows


class PDFProcessor:
    """Extract text from PDFs, using OCR only for pages that need it."""

    def __init__(self, config: PDFProcessorConfig | None = None) -> None:
        self.config = config or PDFProcessorConfig()

    def process(self, source: str | Path | bytes, *, file_name: str | None = None) -> DocumentTextResult:
        """Process a PDF path or in-memory PDF bytes (e.g. Streamlit upload)."""
        pdf_bytes, resolved_name = self._read_source(source, file_name=file_name)
        digest = hashlib.sha256(pdf_bytes).hexdigest()

        try:
            document = fitz.open(stream=pdf_bytes, filetype="pdf")
        except (fitz.FileDataError, ValueError, RuntimeError) as exc:
            raise InvalidPDFError(f"Could not open '{resolved_name}' as a PDF") from exc

        with document:
            if document.needs_pass:
                raise EncryptedPDFError(f"'{resolved_name}' is password-protected")
            page_count = document.page_count
            if page_count < 1:
                raise InvalidPDFError("PDF contains no pages")
            if page_count > self.config.max_pages:
                raise PDFLimitExceededError(
                    f"PDF has {page_count} pages; limit is {self.config.max_pages}"
                )

            extracted_pages: list[PageText] = []
            warnings: list[str] = []
            used_ocr = False

            for index in range(page_count):
                page = document.load_page(index)
                digital_text = self._digital_text(page)

                if not self._needs_ocr(page, digital_text):
                    extracted_pages.append(
                        PageText(page_number=index + 1, text=digital_text, method="PDF_TEXT")
                    )
                    continue

                # Page-level fallback supports mixed PDFs where only some pages
                # are scans. Keep the embedded text if OCR produces nothing.
                used_ocr = True
                try:
                    ocr_text, confidence = self._ocr_page(page)
                except OCRUnavailableError:
                    raise
                except Exception as exc:  # OCR failures become visible warnings.
                    warnings.append(f"OCR failed on page {index + 1}: {type(exc).__name__}")
                    ocr_text, confidence = "", None

                if len(ocr_text) > len(digital_text):
                    page_text = PageText(
                        page_number=index + 1, text=ocr_text, method="OCR", ocr_confidence=confidence
                    )
                    if confidence is not None and confidence < self.config.min_ocr_confidence:
                        warnings.append(
                            f"Low OCR confidence on page {index + 1}: {confidence:.0f}% "
                            f"(threshold {self.config.min_ocr_confidence:.0f}%)."
                        )
                elif digital_text:
                    page_text = PageText(page_number=index + 1, text=digital_text, method="PDF_TEXT")
                else:
                    page_text = PageText(page_number=index + 1, text="", method="EMPTY")
                    warnings.append(f"No readable text found on page {index + 1}.")
                extracted_pages.append(page_text)

            full_text = "\n\n".join(
                f"[Page {page.page_number}]\n{page.text}"
                for page in extracted_pages
                if page.text
            ).strip()

        document_type = DocumentType.SCANNED_PDF if used_ocr else DocumentType.DIGITAL_PDF
        if not full_text:
            warnings.append("No readable text was extracted from the document.")

        return DocumentTextResult(
            file_name=resolved_name,
            file_sha256=digest,
            document_type=document_type,
            page_count=page_count,
            pages=tuple(extracted_pages),
            full_text=full_text,
            warnings=tuple(warnings),
        )

    def _read_source(self, source: str | Path | bytes, *, file_name: str | None) -> tuple[bytes, str]:
        if isinstance(source, bytes):
            resolved_name = file_name or "uploaded_invoice.pdf"
            if not resolved_name.lower().endswith(".pdf"):
                raise InvalidPDFError("Uploaded file must have a .pdf filename")
            pdf_bytes = source
        else:
            path = Path(source)
            if path.suffix.lower() != ".pdf":
                raise InvalidPDFError("Input file must have a .pdf extension")
            if not path.exists() or not path.is_file():
                raise FileNotFoundError(path)
            resolved_name = file_name or path.name
            if not resolved_name.lower().endswith(".pdf"):
                raise InvalidPDFError("Filename must have a .pdf extension")
            if path.stat().st_size > self.config.max_file_size_bytes:
                raise PDFLimitExceededError(
                    f"PDF exceeds {self.config.max_file_size_bytes} byte size limit"
                )
            pdf_bytes = path.read_bytes()

        if len(pdf_bytes) > self.config.max_file_size_bytes:
            raise PDFLimitExceededError(
                f"PDF exceeds {self.config.max_file_size_bytes} byte size limit"
            )
        if b"%PDF-" not in pdf_bytes[:1024]:
            raise InvalidPDFError("File does not contain a valid PDF header")
        return pdf_bytes, Path(resolved_name).name

    def _digital_text(self, page: fitz.Page) -> str:
        if self.config.preserve_layout:
            words = [(w[0], w[1], w[2], w[3], w[4]) for w in page.get_text("words")]
            if words:
                lines = [_join_row(row) for row in _group_rows(words)]
                return clean_extracted_text("\n".join(lines))
        return clean_extracted_text(page.get_text("text"))

    def _needs_ocr(self, page: fitz.Page, digital_text: str) -> bool:
        chars = len(digital_text)
        if chars < self.config.min_digital_text_chars_per_page:
            return True
        # A scan with a small text layer (scanner footer, stamp) still needs OCR.
        if chars < self.config.scan_text_ceiling_chars:
            return self._image_coverage(page) >= self.config.scan_image_coverage
        return False

    @staticmethod
    def _image_coverage(page: fitz.Page) -> float:
        page_area = page.rect.width * page.rect.height
        if page_area <= 0:
            return 0.0
        covered = 0.0
        for info in page.get_image_info():
            box = fitz.Rect(info["bbox"]) & page.rect
            if not box.is_empty:
                covered += box.width * box.height
        return min(covered / page_area, 1.0)

    def _ocr_page(self, page: fitz.Page) -> tuple[str, float | None]:
        """OCR one page. Returns (text, mean word confidence 0-100 or None)."""
        try:
            import pytesseract
            from pytesseract.pytesseract import TesseractNotFoundError
        except ImportError as exc:  # pragma: no cover - dependency is in requirements.
            raise OCRUnavailableError(
                "OCR is required, but pytesseract is not installed. Install project requirements."
            ) from exc

        # pytesseract keeps the command in a process-global; always set it so one
        # processor's configuration can never leak into another's (Streamlit reruns).
        command = self.config.tesseract_cmd or get_settings().tesseract_cmd
        pytesseract.pytesseract.tesseract_cmd = command or "tesseract"

        scale = self.config.ocr_dpi / 72
        pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        if pixmap.n == 1:
            image = Image.frombytes("L", (pixmap.width, pixmap.height), pixmap.samples)
        elif pixmap.n == 3:
            image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
        else:
            # Explicitly convert unusual colorspaces to RGB before Pillow.
            rgb_pixmap = fitz.Pixmap(fitz.csRGB, pixmap)
            image = Image.frombytes("RGB", (rgb_pixmap.width, rgb_pixmap.height), rgb_pixmap.samples)

        try:
            data = pytesseract.image_to_data(
                image,
                lang=self.config.ocr_language,
                config=self.config.tesseract_config,
                output_type=pytesseract.Output.DICT,
            )
        except TesseractNotFoundError as exc:
            raise OCRUnavailableError(
                "Tesseract OCR is required for scanned PDFs. Install the Tesseract binary "
                "and add it to PATH, or set TESSERACT_CMD."
            ) from exc

        # Rebuild lines from word boxes: one OCR pass yields text AND confidence.
        words: list[tuple[float, float, float, float, str]] = []
        confidences: list[float] = []
        for i, raw in enumerate(data["text"]):
            word = raw.strip()
            conf = float(data["conf"][i])
            if not word or conf < 0:
                continue
            left, top = float(data["left"][i]), float(data["top"][i])
            words.append((left, top, left + float(data["width"][i]), top + float(data["height"][i]), word))
            confidences.append(conf)

        text = clean_extracted_text("\n".join(_join_row(row) for row in _group_rows(words)))
        mean_conf = sum(confidences) / len(confidences) if confidences else None
        return text, mean_conf
