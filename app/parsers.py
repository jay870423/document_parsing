from __future__ import annotations

import mimetypes
from pathlib import Path

import fitz
from docx import Document
from docx.oxml.ns import qn

from app.models import DocumentParseResult, ImageRecord, TextBlock


class DocumentParser:
    @staticmethod
    def _round_bbox_key(page: int, bbox: list[float]) -> tuple:
        if len(bbox) < 4:
            return (page, 0.0, 0.0, 0.0, 0.0)
        return (
            page,
            round(float(bbox[0]), 1),
            round(float(bbox[1]), 1),
            round(float(bbox[2]), 1),
            round(float(bbox[3]), 1),
        )

    @staticmethod
    def _render_clip_as_png(page: fitz.Page, rect: fitz.Rect, out_path: Path, zoom: float = 2.0) -> tuple[int, int]:
        clip = fitz.Rect(rect)
        if clip.is_empty or clip.width <= 0 or clip.height <= 0:
            clip = page.rect
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip, alpha=False)
        pix.save(str(out_path))
        return pix.width, pix.height

    @staticmethod
    def _docx_run_position(run, fallback_index: int) -> int:
        try:
            parent = run._element.getparent()
            if parent is None:
                return fallback_index
            return int(parent.index(run._element))
        except Exception:
            return fallback_index

    @staticmethod
    def _extract_image_bytes_or_render(
        doc: fitz.Document,
        page: fitz.Page,
        xref: int,
        rect: fitz.Rect,
        out_dir: Path,
        file_stem: str,
    ) -> tuple[str, Path, int | None, int | None, str]:
        base_image: dict = {}
        try:
            base_image = doc.extract_image(xref) or {}
        except Exception:
            base_image = {}

        ext = str(base_image.get("ext") or "").lower()
        image_bytes = base_image.get("image")
        vector_like_exts = {"wmf", "emf", "svg"}
        use_render_fallback = (not image_bytes) or (ext in vector_like_exts)

        if use_render_fallback:
            ext = "png"
            img_filename = f"{file_stem}.{ext}"
            img_path = out_dir / img_filename
            width, height = DocumentParser._render_clip_as_png(page, rect, img_path, zoom=2.0)
            return img_filename, img_path, width, height, "render_clip"

        img_filename = f"{file_stem}.{ext}"
        img_path = out_dir / img_filename
        img_path.write_bytes(image_bytes)
        return img_filename, img_path, base_image.get("width"), base_image.get("height"), "embedded"

    def parse(self, file_path: str, image_output_dir: str) -> DocumentParseResult:
        src = Path(file_path)
        out_dir = Path(image_output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        suffix = src.suffix.lower()
        if suffix == ".pdf":
            return self._parse_pdf(src, out_dir)
        if suffix in {".docx", ".doc"}:
            return self._parse_docx(src, out_dir)
        raise ValueError(f"Unsupported file type: {suffix}")

    def _parse_pdf(self, src: Path, out_dir: Path) -> DocumentParseResult:
        text_blocks: list[TextBlock] = []
        image_records: list[ImageRecord] = []
        seen_bbox_keys: set[tuple] = set()

        doc = fitz.open(src)
        try:
            for page_num, page in enumerate(doc, start=1):
                page_image_counter = 0
                blocks = page.get_text("dict").get("blocks", [])
                for block in blocks:
                    if block.get("type") != 0:
                        continue
                    for line in block.get("lines", []):
                        for span in line.get("spans", []):
                            txt = (span.get("text") or "").strip()
                            if not txt:
                                continue
                            text_blocks.append(
                                TextBlock(
                                    page=page_num,
                                    bbox=[float(v) for v in span.get("bbox", [0, 0, 0, 0])],
                                    text=txt,
                                )
                            )

                image_list = page.get_images(full=True)
                for img_info in image_list:
                    xref = img_info[0]
                    rects = page.get_image_rects(xref)
                    if not rects:
                        continue

                    for occ_idx, rect in enumerate(rects):
                        bbox_vals = [float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1)]
                        bbox_key = self._round_bbox_key(page_num, bbox_vals)
                        if bbox_key in seen_bbox_keys:
                            continue
                        seen_bbox_keys.add(bbox_key)

                        file_stem = f"page_{page_num}_img_{page_image_counter}"
                        img_name, img_path, img_width, img_height, extract_mode = self._extract_image_bytes_or_render(
                            doc=doc,
                            page=page,
                            xref=xref,
                            rect=rect,
                            out_dir=out_dir,
                            file_stem=file_stem,
                        )
                        placeholder = f"<<IMG_PLACEHOLDER_page{page_num}_idx{page_image_counter}>>"
                        image_records.append(
                            ImageRecord(
                                page=page_num,
                                bbox=bbox_vals,
                                xref=xref,
                                img_index=page_image_counter,
                                occurrence_index=occ_idx,
                                img_path=str(img_path),
                                img_filename=img_name,
                                placeholder=placeholder,
                                width=img_width,
                                height=img_height,
                                extract_mode=extract_mode,
                                source_type="pdf",
                            )
                        )
                        page_image_counter += 1

                # Some PDFs expose raster content as image blocks only.
                # Use these blocks as a fallback to avoid missing inline images.
                for block in blocks:
                    if block.get("type") != 1:
                        continue
                    bbox = block.get("bbox")
                    if not bbox or len(bbox) < 4:
                        continue
                    bbox_vals = [float(v) for v in bbox[:4]]
                    bbox_key = self._round_bbox_key(page_num, bbox_vals)
                    if bbox_key in seen_bbox_keys:
                        continue
                    seen_bbox_keys.add(bbox_key)

                    ext = str(block.get("ext") or "png").lower()
                    image_bytes = block.get("image")
                    img_name = f"page_{page_num}_img_{page_image_counter}.{ext if ext else 'png'}"
                    img_path = out_dir / img_name
                    img_width: int | None = None
                    img_height: int | None = None
                    extract_mode = "inline_block"

                    if image_bytes:
                        img_path.write_bytes(image_bytes)
                        img_width = block.get("width")
                        img_height = block.get("height")
                    else:
                        img_name = f"page_{page_num}_img_{page_image_counter}.png"
                        img_path = out_dir / img_name
                        img_width, img_height = self._render_clip_as_png(
                            page=page,
                            rect=fitz.Rect(bbox_vals),
                            out_path=img_path,
                            zoom=2.0,
                        )
                        extract_mode = "render_clip"

                    placeholder = f"<<IMG_PLACEHOLDER_page{page_num}_idx{page_image_counter}>>"
                    image_records.append(
                        ImageRecord(
                            page=page_num,
                            bbox=bbox_vals,
                            xref=None,
                            img_index=page_image_counter,
                            occurrence_index=0,
                            img_path=str(img_path),
                            img_filename=img_name,
                            placeholder=placeholder,
                            width=img_width,
                            height=img_height,
                            extract_mode=extract_mode,
                            source_type="pdf",
                        )
                    )
                    page_image_counter += 1
        finally:
            doc.close()

        return DocumentParseResult(
            source_path=str(src),
            source_type="pdf",
            text_blocks=text_blocks,
            image_records=image_records,
        )

    def _parse_docx(self, src: Path, out_dir: Path) -> DocumentParseResult:
        text_blocks: list[TextBlock] = []
        image_records: list[ImageRecord] = []

        doc = Document(str(src))
        image_counter = 0
        for p_idx, paragraph in enumerate(doc.paragraphs):
            for r_idx, run in enumerate(paragraph.runs):
                run_pos = self._docx_run_position(run, r_idx)
                content = run.text.strip()
                if content:
                    text_blocks.append(
                        TextBlock(
                            page=1,
                            bbox=[float(run_pos), float(p_idx), float(run_pos), float(p_idx)],
                            text=content,
                        )
                    )

                blips = run._element.xpath(".//a:blip")
                for blip in blips:
                    rel_id = blip.get(qn("r:embed"))
                    if not rel_id:
                        continue
                    part = doc.part.related_parts.get(rel_id)
                    if not part:
                        continue
                    ext = Path(str(part.partname)).suffix.lower().lstrip(".")
                    if not ext:
                        mime = getattr(part, "content_type", None)
                        ext = (mimetypes.guess_extension(mime or "") or ".png").lstrip(".")

                    file_name = f"docx_img_{image_counter}.{ext}"
                    img_path = out_dir / file_name
                    img_path.write_bytes(part.blob)

                    placeholder = f"<<IMG_PLACEHOLDER_page1_idx{image_counter}>>"
                    image_records.append(
                        ImageRecord(
                            page=1,
                            bbox=[float(run_pos), float(p_idx), float(run_pos), float(p_idx)],
                            img_index=image_counter,
                            occurrence_index=0,
                            img_path=str(img_path),
                            img_filename=file_name,
                            placeholder=placeholder,
                            width=None,
                            height=None,
                            extract_mode="embedded",
                            source_type="docx",
                            paragraph_index=p_idx,
                            run_index=run_pos,
                        )
                    )
                    image_counter += 1

        return DocumentParseResult(
            source_path=str(src),
            source_type="docx",
            text_blocks=text_blocks,
            image_records=image_records,
        )
