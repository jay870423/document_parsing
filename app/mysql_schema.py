"""MySQL 8 / InnoDB / utf8mb4 schema for doc relational data."""

BILLING_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS users (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        email VARCHAR(320) NOT NULL UNIQUE,
        phone VARCHAR(32) NULL UNIQUE,
        password_salt VARCHAR(128) NOT NULL,
        password_hash VARCHAR(128) NOT NULL,
        is_admin TINYINT(1) NOT NULL DEFAULT 0,
        is_active TINYINT(1) NOT NULL DEFAULT 1,
        created_at VARCHAR(40) NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        user_id BIGINT NOT NULL,
        token_hash CHAR(64) NOT NULL UNIQUE,
        expires_at VARCHAR(40) NOT NULL,
        created_at VARCHAR(40) NOT NULL,
        INDEX idx_sessions_user (user_id, expires_at),
        CONSTRAINT fk_sessions_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS api_keys (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        user_id BIGINT NOT NULL,
        name VARCHAR(255) NOT NULL,
        key_prefix VARCHAR(32) NOT NULL,
        key_hash CHAR(64) NOT NULL UNIQUE,
        raw_key VARCHAR(255) NULL,
        is_active TINYINT(1) NOT NULL DEFAULT 1,
        created_at VARCHAR(40) NOT NULL,
        last_used_at VARCHAR(40) NULL,
        INDEX idx_api_keys_user (user_id, is_active, created_at),
        CONSTRAINT fk_api_keys_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS plans (
        code VARCHAR(64) PRIMARY KEY,
        name VARCHAR(255) NOT NULL,
        billing_period VARCHAR(32) NOT NULL,
        price_cents BIGINT NOT NULL,
        included_calls BIGINT NOT NULL,
        is_active TINYINT(1) NOT NULL DEFAULT 1,
        sort_order BIGINT NOT NULL DEFAULT 0
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS subscriptions (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        user_id BIGINT NOT NULL,
        plan_code VARCHAR(64) NOT NULL,
        status VARCHAR(32) NOT NULL,
        starts_at VARCHAR(40) NOT NULL,
        ends_at VARCHAR(40) NOT NULL,
        call_quota BIGINT NOT NULL,
        used_calls BIGINT NOT NULL DEFAULT 0,
        created_at VARCHAR(40) NOT NULL,
        INDEX idx_subscriptions_user (user_id, status, ends_at),
        CONSTRAINT fk_subscriptions_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
        CONSTRAINT fk_subscriptions_plan FOREIGN KEY (plan_code) REFERENCES plans(code)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS call_reservations (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        reservation_id CHAR(32) NOT NULL UNIQUE,
        user_id BIGINT NOT NULL,
        subscription_id BIGINT NOT NULL,
        call_count BIGINT NOT NULL,
        state VARCHAR(16) NOT NULL DEFAULT 'reserved',
        created_at VARCHAR(40) NOT NULL,
        settled_at VARCHAR(40) NULL,
        INDEX idx_call_reservations_state_created (state, created_at),
        INDEX idx_call_reservations_user (user_id, created_at),
        CONSTRAINT fk_call_reservations_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
        CONSTRAINT fk_call_reservations_subscription FOREIGN KEY (subscription_id) REFERENCES subscriptions(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS usage_events (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        user_id BIGINT NOT NULL,
        api_key_id BIGINT NULL,
        endpoint VARCHAR(500) NOT NULL,
        method VARCHAR(20) NOT NULL,
        call_count BIGINT NOT NULL DEFAULT 1,
        input_tokens BIGINT NOT NULL DEFAULT 0,
        output_tokens BIGINT NOT NULL DEFAULT 0,
        status_code BIGINT NOT NULL,
        latency_ms BIGINT NOT NULL DEFAULT 0,
        meta_json MEDIUMTEXT NOT NULL,
        created_at VARCHAR(40) NOT NULL,
        INDEX idx_usage_user_created (user_id, created_at),
        INDEX idx_usage_endpoint (endpoint(190), created_at),
        CONSTRAINT fk_usage_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_bases (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        user_id BIGINT NOT NULL,
        name VARCHAR(255) NOT NULL,
        description TEXT NOT NULL,
        is_default TINYINT(1) NOT NULL DEFAULT 0,
        is_active TINYINT(1) NOT NULL DEFAULT 1,
        created_at VARCHAR(40) NOT NULL,
        updated_at VARCHAR(40) NOT NULL,
        active_default_user_id BIGINT GENERATED ALWAYS AS
            (CASE WHEN is_default=1 AND is_active=1 THEN user_id ELSE NULL END) STORED,
        UNIQUE KEY uq_knowledge_bases_active_default (active_default_user_id),
        INDEX idx_knowledge_bases_user (user_id, is_active, created_at),
        CONSTRAINT fk_knowledge_bases_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE RESTRICT
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_documents (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        user_id BIGINT NOT NULL,
        knowledge_base_id BIGINT NOT NULL,
        run_id VARCHAR(128) NOT NULL,
        source_filename VARCHAR(255) NOT NULL,
        source_type VARCHAR(40) NOT NULL DEFAULT '',
        status VARCHAR(40) NOT NULL,
        vector_count BIGINT NOT NULL DEFAULT 0,
        text_block_count BIGINT NOT NULL DEFAULT 0,
        image_count BIGINT NOT NULL DEFAULT 0,
        artifact_path VARCHAR(1000) NOT NULL DEFAULT '',
        created_at VARCHAR(40) NOT NULL,
        updated_at VARCHAR(40) NOT NULL,
        INDEX idx_knowledge_documents_kb (user_id, knowledge_base_id, created_at),
        CONSTRAINT fk_knowledge_documents_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
        CONSTRAINT fk_knowledge_documents_kb FOREIGN KEY (knowledge_base_id) REFERENCES knowledge_bases(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS rag_query_logs (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        user_id BIGINT NOT NULL,
        api_key_id BIGINT NULL,
        knowledge_base_id BIGINT NOT NULL,
        `query` TEXT NOT NULL,
        answer MEDIUMTEXT NOT NULL,
        retrieval_mode VARCHAR(40) NOT NULL,
        top_k BIGINT NOT NULL,
        source_filename VARCHAR(255) NULL,
        hit_count BIGINT NOT NULL DEFAULT 0,
        citations_json MEDIUMTEXT NOT NULL,
        feedback_rating VARCHAR(20) NULL,
        feedback_note TEXT NULL,
        feedback_at VARCHAR(40) NULL,
        created_at VARCHAR(40) NOT NULL,
        INDEX idx_rag_query_logs_user (user_id, knowledge_base_id, created_at),
        CONSTRAINT fk_rag_query_logs_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
        CONSTRAINT fk_rag_query_logs_kb FOREIGN KEY (knowledge_base_id) REFERENCES knowledge_bases(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS slider_challenges (
        id VARCHAR(128) PRIMARY KEY,
        target_hash VARCHAR(128) NOT NULL,
        expires_at VARCHAR(40) NOT NULL,
        used_at VARCHAR(40) NULL,
        ip_address VARCHAR(80) NOT NULL,
        phone VARCHAR(32) NULL,
        created_at VARCHAR(40) NOT NULL,
        INDEX idx_slider_expires (expires_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS sms_verification_codes (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        phone VARCHAR(32) NOT NULL,
        code_salt VARCHAR(128) NOT NULL,
        code_hash VARCHAR(128) NOT NULL,
        purpose VARCHAR(40) NOT NULL,
        expires_at VARCHAR(40) NOT NULL,
        used_at VARCHAR(40) NULL,
        attempts BIGINT NOT NULL DEFAULT 0,
        locked_until VARCHAR(40) NULL,
        ip_address VARCHAR(80) NOT NULL,
        provider_message_id VARCHAR(255) NULL,
        created_at VARCHAR(40) NOT NULL,
        INDEX idx_sms_phone_created (phone, purpose, created_at),
        INDEX idx_sms_ip_created (ip_address, created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS agreement_consents (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        user_id BIGINT NOT NULL,
        action VARCHAR(40) NOT NULL,
        terms_version VARCHAR(40) NOT NULL,
        privacy_version VARCHAR(40) NOT NULL,
        document_hash VARCHAR(128) NOT NULL,
        ip_address VARCHAR(80) NOT NULL,
        user_agent VARCHAR(500) NOT NULL,
        accepted_at VARCHAR(40) NOT NULL,
        INDEX idx_agreement_user (user_id, accepted_at),
        CONSTRAINT fk_agreement_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS purchase_intents (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        user_id BIGINT NOT NULL,
        plan_code VARCHAR(64) NOT NULL,
        status VARCHAR(32) NOT NULL DEFAULT 'pending',
        contact_note VARCHAR(500) NOT NULL DEFAULT '',
        created_at VARCHAR(40) NOT NULL,
        INDEX idx_purchase_intents_user (user_id, created_at),
        CONSTRAINT fk_purchase_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
        CONSTRAINT fk_purchase_plan FOREIGN KEY (plan_code) REFERENCES plans(code)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
)

WEWE_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS wewe_sources (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        user_id BIGINT NOT NULL,
        knowledge_base_id BIGINT NOT NULL,
        source_type VARCHAR(32) NOT NULL,
        upstream_feed_id VARCHAR(255) NOT NULL DEFAULT '',
        identity_hash CHAR(64) NOT NULL UNIQUE,
        name VARCHAR(500) NOT NULL,
        intro TEXT NOT NULL,
        cover_url MEDIUMTEXT NOT NULL,
        target_url MEDIUMTEXT NOT NULL,
        subscribed TINYINT(1) NOT NULL DEFAULT 1,
        status VARCHAR(32) NOT NULL DEFAULT 'active',
        last_sync_at VARCHAR(40) NULL,
        last_error TEXT NOT NULL DEFAULT (''),
        created_at VARCHAR(40) NOT NULL,
        updated_at VARCHAR(40) NOT NULL,
        INDEX idx_wewe_sources_user (user_id, knowledge_base_id, created_at),
        INDEX idx_wewe_sources_subscribed (subscribed, status),
        CONSTRAINT fk_wewe_sources_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
        CONSTRAINT fk_wewe_sources_kb FOREIGN KEY (knowledge_base_id) REFERENCES knowledge_bases(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS wewe_articles (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        user_id BIGINT NOT NULL,
        source_id BIGINT NOT NULL,
        knowledge_base_id BIGINT NOT NULL,
        upstream_article_id VARCHAR(512) NOT NULL,
        title TEXT NOT NULL,
        url MEDIUMTEXT NOT NULL,
        author VARCHAR(500) NOT NULL DEFAULT '',
        published_at VARCHAR(80) NOT NULL DEFAULT '',
        cover_url MEDIUMTEXT NOT NULL,
        content_html MEDIUMTEXT NOT NULL,
        text_content MEDIUMTEXT NOT NULL,
        status VARCHAR(32) NOT NULL DEFAULT 'pending',
        error TEXT NOT NULL DEFAULT (''),
        chunk_count BIGINT NOT NULL DEFAULT 0,
        image_count BIGINT NOT NULL DEFAULT 0,
        vector_version VARCHAR(64) NULL,
        indexed_at VARCHAR(40) NULL,
        created_at VARCHAR(40) NOT NULL,
        updated_at VARCHAR(40) NOT NULL,
        UNIQUE KEY uq_wewe_article_identity (user_id, source_id, upstream_article_id),
        INDEX idx_wewe_articles_user (user_id, source_id, published_at),
        INDEX idx_wewe_articles_kb (user_id, knowledge_base_id, created_at),
        CONSTRAINT fk_wewe_articles_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
        CONSTRAINT fk_wewe_articles_source FOREIGN KEY (source_id) REFERENCES wewe_sources(id) ON DELETE CASCADE,
        CONSTRAINT fk_wewe_articles_kb FOREIGN KEY (knowledge_base_id) REFERENCES knowledge_bases(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS wewe_images (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        article_id BIGINT NOT NULL,
        user_id BIGINT NOT NULL,
        url MEDIUMTEXT NOT NULL,
        url_hash CHAR(64) NOT NULL,
        local_path VARCHAR(1000) NOT NULL DEFAULT '',
        mime_type VARCHAR(100) NOT NULL DEFAULT '',
        byte_size BIGINT NOT NULL DEFAULT 0,
        description MEDIUMTEXT NOT NULL,
        status VARCHAR(32) NOT NULL DEFAULT 'pending',
        error TEXT NOT NULL DEFAULT (''),
        vector_version VARCHAR(64) NULL,
        created_at VARCHAR(40) NOT NULL,
        updated_at VARCHAR(40) NOT NULL,
        UNIQUE KEY uq_wewe_image_url (article_id, url_hash),
        INDEX idx_wewe_images_user (user_id, status),
        CONSTRAINT fk_wewe_images_article FOREIGN KEY (article_id) REFERENCES wewe_articles(id) ON DELETE CASCADE,
        CONSTRAINT fk_wewe_images_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS wewe_collect_tasks (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        user_id BIGINT NOT NULL,
        source_id BIGINT NULL,
        kind VARCHAR(32) NOT NULL DEFAULT 'manual',
        status VARCHAR(32) NOT NULL DEFAULT 'running',
        worker_id VARCHAR(128) NOT NULL DEFAULT '',
        heartbeat_at VARCHAR(40) NULL,
        lease_expires_at VARCHAR(40) NULL,
        article_count BIGINT NOT NULL DEFAULT 0,
        image_count BIGINT NOT NULL DEFAULT 0,
        indexed_count BIGINT NOT NULL DEFAULT 0,
        error TEXT NOT NULL DEFAULT (''),
        started_at VARCHAR(40) NOT NULL,
        finished_at VARCHAR(40) NULL,
        created_at VARCHAR(40) NOT NULL,
        INDEX idx_wewe_tasks_user (user_id, created_at),
        INDEX idx_wewe_tasks_status (status, created_at),
        CONSTRAINT fk_wewe_tasks_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
        CONSTRAINT fk_wewe_tasks_source FOREIGN KEY (source_id) REFERENCES wewe_sources(id) ON DELETE SET NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS wewe_collect_locks (
        source_id BIGINT PRIMARY KEY,
        task_id BIGINT NOT NULL UNIQUE,
        created_at VARCHAR(40) NOT NULL,
        CONSTRAINT fk_wewe_lock_source FOREIGN KEY (source_id) REFERENCES wewe_sources(id) ON DELETE CASCADE,
        CONSTRAINT fk_wewe_lock_task FOREIGN KEY (task_id) REFERENCES wewe_collect_tasks(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS wewe_account_owners (
        user_id BIGINT NOT NULL,
        upstream_account_id VARCHAR(255) NOT NULL,
        created_at VARCHAR(40) NOT NULL,
        PRIMARY KEY (user_id, upstream_account_id),
        INDEX idx_wewe_account_owner_upstream (upstream_account_id),
        CONSTRAINT fk_wewe_account_owner_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS wewe_feed_owners (
        user_id BIGINT NOT NULL,
        upstream_feed_id VARCHAR(255) NOT NULL,
        created_at VARCHAR(40) NOT NULL,
        PRIMARY KEY (user_id, upstream_feed_id),
        INDEX idx_wewe_feed_owner_upstream (upstream_feed_id),
        CONSTRAINT fk_wewe_feed_owner_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
)
