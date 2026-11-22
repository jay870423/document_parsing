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
    # All relational data lives in the doc-only MySQL database.  The legacy
    # SQLite file is read exclusively by the offline migration script.
    mysql_host: str = "localhost"
    mysql_port: int = 3306
    mysql_user: str = "document_parsing_app"
    mysql_password: str = ""
    mysql_database: str = "document_parsing"
    mysql_unix_socket: str = "/var/run/mysqld/mysqld.sock"
    mysql_connect_timeout: int = 10
    mysql_read_timeout: int = 30
    mysql_write_timeout: int = 30
    sqlite_source_path: str = "data/app.db"
    bootstrap_admin_email: str = "admin@doc.zhouyuaninfo.com.cn"
    bootstrap_admin_password: str = "Admin@123456"
    app_sms_enabled: bool = False
    app_sms_provider: str = "tencent"
    app_sms_secret_id: str = ""
    app_sms_secret_key: str = ""
    app_sms_sdk_app_id: str = ""
    app_sms_sign_name: str = ""
    app_sms_template_id: str = ""
    app_sms_code_expire_seconds: int = 300
    app_sms_daily_phone_limit: int = 10
    app_sms_hourly_ip_limit: int = 30
    app_sms_wrong_lock_seconds: int = 900

    ark_api_key: str = Field(default="")
    ark_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    # MiniMax-M3 serves text answers and native-image descriptions through the
    # Anthropic Messages protocol. Dense embedding remains on the existing Ark client.
    ai_provider: str = "minimax"
    anthropic_api_key: str = Field(default="")
    anthropic_base_url: str = "https://api.minimaxi.com/anthropic"
    minimax_model: str = "MiniMax-M3"
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

    # --- wewe-rss integration (upstream collector adapter) ---
    wewe_enabled: bool = False
    wewe_base_url: str = ""
    wewe_auth_code: str = ""
    wewe_timeout_seconds: int = 20
    wewe_max_retries: int = 3
    wewe_page_limit: int = 30
    wewe_max_articles_per_source: int = 50
    wewe_max_images_per_article: int = 20
    wewe_max_image_bytes: int = 5_000_000
    wewe_max_download_bytes: int = 5_000_000
    wewe_collect_enabled: bool = False
    wewe_collect_interval_minutes: int = 60
    wewe_task_lease_seconds: int = 120
    wewe_image_collection: str = "wewe_image_chunks"
    wewe_image_embedding_model: str = "doubao-embedding-vision"
    wewe_image_vector_size: int = 1024
    wewe_image_vector_enabled: bool = False
    wewe_image_vector_weight: float = 0.3
    wewe_artifact_dir: str = "artifacts/wewe"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
