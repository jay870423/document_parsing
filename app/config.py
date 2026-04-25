import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def get_env_file_path() -> Path:
    return Path(os.environ.get("APP_ENV_FILE", ".env"))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(get_env_file_path()), env_file_encoding="utf-8", extra="ignore")

    app_name: str = "document-parsing-service"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: str = "INFO"

    ark_api_key: str = Field(default="")
    ark_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    doubao_vision_model: str = "doubao-vision-pro-32k"
    vision_max_retries: int = 3
    vision_timeout_seconds: int = 60
    vision_temperature: float = 0.1
    vision_max_tokens: int = 2048
    vision_qps: float = 1.0
    vision_prompt_template: str = (
        "请将这张图片中的寄存器描述或参数表格转换为纯文本表格，仅输出结果，不要解释。"
    )

    doubao_embedding_model: str = "doubao-embedding"
    doubao_chat_model: str = "doubao-pro-32k"
    qdrant_url: str = "http://qdrant:6333"
    qdrant_api_key: str = ""
    qdrant_collection: str = "manual_chunks"
    vector_size: int = 1024
    distance_metric: str = "Cosine"

    chunk_size: int = 800
    chunk_overlap: int = 100
    rag_answer_max_tokens: int = 1200


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
