from __future__ import annotations

import json
from pathlib import Path

from app.config import Settings
from app.merge import merge_text_and_images, sort_text_blocks_by_position
from app.parsers import DocumentParser
from app.rag import DoubaoChatClient, DoubaoEmbeddingClient, QdrantIndexer, split_text_with_metadata
from app.vision import DoubaoVisionClient


class DocumentPipeline:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.parser = DocumentParser()
        self.vision = DoubaoVisionClient(settings)
        self.embedding = DoubaoEmbeddingClient(settings)
        self.chat = DoubaoChatClient(settings)
        self.indexer = QdrantIndexer(settings)

    def process(
        self,
        file_path: str,
        output_dir: str,
        run_vision: bool = True,
        ingest_vector: bool = False,
        rebuild_index: bool = False,
    ) -> dict:
        out_dir = Path(output_dir)
        images_dir = out_dir / "images"
        out_dir.mkdir(parents=True, exist_ok=True)
        images_dir.mkdir(parents=True, exist_ok=True)

        parsed = self.parser.parse(file_path=file_path, image_output_dir=str(images_dir))

        image_to_text: dict[str, str] = {}
        if run_vision and parsed.image_records:
            for img in parsed.image_records:
                result = self.vision.analyze_image(
                    image_path=img.img_path,
                    prompt=self.settings.vision_prompt_template,
                )
                if result:
                    image_to_text[img.placeholder] = result

        sorted_text_blocks = sort_text_blocks_by_position(parsed.text_blocks)
        merged_text = merge_text_and_images(sorted_text_blocks, parsed.image_records, image_to_text)

        (out_dir / "text_blocks.json").write_text(
            json.dumps([b.model_dump() for b in parsed.text_blocks], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (out_dir / "image_records.json").write_text(
            json.dumps([b.model_dump() for b in parsed.image_records], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (out_dir / "image_structured_text.json").write_text(
            json.dumps(image_to_text, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        merged_path = out_dir / "full_manual_text.md"
        merged_path.write_text(merged_text, encoding="utf-8")

        vector_count = 0
        deleted_chunks_before_rebuild = 0
        if ingest_vector and merged_text.strip():
            source_filename = Path(file_path).name
            if rebuild_index:
                deleted_chunks_before_rebuild = self.indexer.remove_source_chunks(source_filename)

            chunk_records = split_text_with_metadata(
                merged_text,
                self.settings.chunk_size,
                self.settings.chunk_overlap,
            )
            chunks = [str(item.get("text", "")) for item in chunk_records if str(item.get("text", "")).strip()]
            if chunks:
                vectors = self.embedding.embed_texts(chunks)
                self.indexer.upsert_chunks(
                    vectors=vectors,
                    chunks=chunks,
                    metadata={
                        "source_file": str(file_path),
                        "source_filename": source_filename,
                        "source_manual": source_filename,
                        "source_type": parsed.source_type,
                        "merged_output": str(merged_path),
                    },
                    chunk_metadatas=[
                        {
                            "page_start": item.get("page_start"),
                            "page_end": item.get("page_end"),
                            "chapter": item.get("chapter"),
                        }
                        for item in chunk_records
                        if str(item.get("text", "")).strip()
                    ],
                )
                vector_count = len(chunks)

        return {
            "source_file": file_path,
            "source_filename": Path(file_path).name,
            "source_type": parsed.source_type,
            "image_count": len(parsed.image_records),
            "text_block_count": len(parsed.text_blocks),
            "merged_output": str(merged_path),
            "vector_count": vector_count,
            "rebuild_index": rebuild_index,
            "deleted_chunks_before_rebuild": deleted_chunks_before_rebuild,
        }
