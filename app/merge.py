from __future__ import annotations

from app.models import ImageRecord, TextBlock


def sort_text_blocks_by_position(blocks: list[TextBlock]) -> list[TextBlock]:
    return sorted(
        blocks,
        key=lambda b: (
            b.page,
            b.bbox[1] if len(b.bbox) > 1 else 0,
            b.bbox[0] if b.bbox else 0,
        ),
    )


def merge_text_and_images(
    sorted_text_blocks: list[TextBlock],
    image_records: list[ImageRecord],
    image_to_text_map: dict[str, str],
) -> str:
    all_blocks: list[dict] = []
    for block in sorted_text_blocks:
        all_blocks.append({"page": block.page, "bbox": block.bbox, "text": block.text, "is_image": False})

    for rec in image_records:
        converted = image_to_text_map.get(rec.placeholder, f"[图片未识别: {rec.placeholder}]")
        all_blocks.append({"page": rec.page, "bbox": rec.bbox, "text": converted, "is_image": True})

    all_blocks.sort(
        key=lambda b: (
            b["page"],
            b["bbox"][1] if len(b["bbox"]) > 1 else 0,
            b["bbox"][0] if b["bbox"] else 0,
        )
    )

    merged: list[str] = []
    current_page = -1
    for block in all_blocks:
        if block["page"] != current_page:
            merged.append(f"\n\n========== 第 {block['page']} 页 ==========\n\n")
            current_page = block["page"]
        merged.append(block["text"])
        merged.append("\n" if block["is_image"] else " ")
    return "".join(merged).strip()
