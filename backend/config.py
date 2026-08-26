"""
RecoveryOS Configuration.

Loads environment variables and provides typed configuration
for all system components.
"""

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv(override=False)


@dataclass(frozen=True)
class Config:
    """Immutable application configuration."""

    # Gemini
    google_api_key: str = field(
        default_factory=lambda: os.environ.get("GOOGLE_API_KEY", "")
    )
    gemini_model: str = field(
        default_factory=lambda: os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
    )

    # Persistence
    persistence_backend: str = field(
        default_factory=lambda: os.environ.get("PERSISTENCE_BACKEND", "in_memory")
    )
    firestore_emulator_host: str = field(
        default_factory=lambda: os.environ.get("FIRESTORE_EMULATOR_HOST", "")
    )
    firestore_database: str = field(
        default_factory=lambda: os.environ.get(
            "FIRESTORE_DATABASE",
            "recoveryosdb" if os.environ.get("ENVIRONMENT") == "production" else "(default)",
        )
    )
    google_cloud_project: str = field(
        default_factory=lambda: os.environ.get(
            "GOOGLE_CLOUD_PROJECT", "recoveryos-local"
        )
    )

    # Pub/Sub
    pubsub_emulator_host: str = field(
        default_factory=lambda: os.environ.get("PUBSUB_EMULATOR_HOST", "")
    )
    event_publisher_backend: str = field(
        default_factory=lambda: os.environ.get("EVENT_PUBLISHER_BACKEND", "in_memory")
    )
    pubsub_topic_workflow_execution: str = field(
        default_factory=lambda: os.environ.get("PUBSUB_TOPIC", "recoveryos-workflow-execution")
    )

    # Server
    host: str = field(
        default_factory=lambda: os.environ.get("HOST", "0.0.0.0")
    )
    port: int = field(
        default_factory=lambda: int(os.environ.get("PORT", "8000"))
    )

    # Environment
    environment: str = field(
        default_factory=lambda: os.environ.get("ENVIRONMENT", "development")
    )

    # Security & Auth
    jwt_secret_key: str = field(
        default_factory=lambda: os.environ.get(
            "JWT_SECRET_KEY", "recoveryos-prod-default-secret-change-in-env-32bytes"
        )
    )
    jwt_algorithm: str = field(
        default_factory=lambda: os.environ.get("JWT_ALGORITHM", "HS256")
    )
    jwt_expiration_minutes: int = field(
        default_factory=lambda: int(os.environ.get("JWT_EXPIRATION_MINUTES", "60"))
    )

    # Gemini Runtime Resilience & Rate Limiting
    gemini_min_interval_seconds: float = field(
        default_factory=lambda: float(os.environ.get("GEMINI_MIN_INTERVAL_SECONDS", "6.5"))
    )
    gemini_max_retries: int = field(
        default_factory=lambda: int(os.environ.get("GEMINI_MAX_RETRIES", "3"))
    )
    gemini_initial_backoff_seconds: float = field(
        default_factory=lambda: float(os.environ.get("GEMINI_INITIAL_BACKOFF_SECONDS", "2.0"))
    )
    gemini_max_backoff_seconds: float = field(
        default_factory=lambda: float(os.environ.get("GEMINI_MAX_BACKOFF_SECONDS", "30.0"))
    )
    gemini_request_timeout_seconds: float = field(
        default_factory=lambda: float(os.environ.get("GEMINI_REQUEST_TIMEOUT_SECONDS", "30.0"))
    )
    gemini_circuit_failure_threshold: int = field(
        default_factory=lambda: int(os.environ.get("GEMINI_CIRCUIT_FAILURE_THRESHOLD", "5"))
    )
    gemini_circuit_cooldown_seconds: float = field(
        default_factory=lambda: float(os.environ.get("GEMINI_CIRCUIT_COOLDOWN_SECONDS", "30.0"))
    )

    # CORS Settings
    cors_allow_origins: list[str] = field(
        default_factory=lambda: [o.strip() for o in os.environ.get("CORS_ALLOW_ORIGINS", "*").split(",") if o.strip()]
    )

    @property
    def is_development(self) -> bool:
        return self.environment == "development"

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def use_firestore_emulator(self) -> bool:
        return bool(self.firestore_emulator_host)

    @property
    def use_pubsub_emulator(self) -> bool:
        return bool(self.pubsub_emulator_host)

    def validate_production_config(self) -> None:
        """
        Validates that production environment configuration is fail-closed.
        Raises ValueError if insecure defaults are present in production.
        """
        if not self.is_production:
            return

        if not self.jwt_secret_key or self.jwt_secret_key.startswith("recoveryos-prod-default") or len(self.jwt_secret_key) < 32:
            raise ValueError(
                "Production configuration error: JWT_SECRET_KEY must be an explicit, high-entropy secret >= 32 characters in production."
            )

        if "*" in self.cors_allow_origins:
            raise ValueError(
                "Production configuration error: Wildcard CORS ('*') is prohibited in production. Specify exact domains in CORS_ALLOW_ORIGINS."
            )

        if self.persistence_backend not in ("in_memory", "firestore"):
            raise ValueError(
                f"Production configuration error: Unsupported PERSISTENCE_BACKEND '{self.persistence_backend}'."
            )

        if self.event_publisher_backend not in ("in_memory", "pubsub"):
            raise ValueError(
                f"Production configuration error: Unsupported EVENT_PUBLISHER_BACKEND '{self.event_publisher_backend}'."
            )

        if self.event_publisher_backend == "pubsub" and not self.google_cloud_project:
            raise ValueError(
                "Production configuration error: GOOGLE_CLOUD_PROJECT is required when EVENT_PUBLISHER_BACKEND is 'pubsub'."
            )


# Singleton config instance
config = Config()
