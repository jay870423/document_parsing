from __future__ import annotations

from pydantic import BaseModel, Field


class TextBlock(BaseModel):
    page: int
    bbox: list[float] = Field(default_factory=list)
    text: str


class ImageRecord(BaseModel):
    page: int
    bbox: list[float] = Field(default_factory=list)
    xref: int | None = None
    img_index: int
    occurrence_index: int | None = None
    img_path: str
    img_filename: str | None = None
    placeholder: str
    width: int | None = None
    height: int | None = None
    extract_mode: str | None = None
    source_type: str = "pdf"
    paragraph_index: int | None = None
    run_index: int | None = None


class DocumentParseResult(BaseModel):
    source_path: str
    source_type: str
    text_blocks: list[TextBlock] = Field(default_factory=list)
    image_records: list[ImageRecord] = Field(default_factory=list)
