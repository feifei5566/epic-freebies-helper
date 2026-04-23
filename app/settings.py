# -*- coding: utf-8 -*-
import os
from pathlib import Path

from hcaptcha_challenger.agent import AgentConfig
from pydantic import Field, SecretStr
from pydantic_settings import SettingsConfigDict

from extensions.llm_adapter import apply_llm_patch

# --- 核心路径定义 ---
PROJECT_ROOT = Path(__file__).parent
VOLUMES_DIR = PROJECT_ROOT.joinpath("volumes")
LOG_DIR = VOLUMES_DIR.joinpath("logs")
USER_DATA_DIR = VOLUMES_DIR.joinpath("user_data")
RUNTIME_DIR = VOLUMES_DIR.joinpath("runtime")
SCREENSHOTS_DIR = VOLUMES_DIR.joinpath("screenshots")
RECORD_DIR = VOLUMES_DIR.joinpath("record")
HCAPTCHA_DIR = VOLUMES_DIR.joinpath("hcaptcha")


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


def _default_provider() -> str:
    return _env("LLM_PROVIDER", "gemini") or "gemini"


def _default_model_for_provider(provider: str) -> str:
    if provider == "glm":
        return _env("GLM_MODEL", "glm-4.5v") or "glm-4.5v"
    return _env("GEMINI_MODEL", "gemini-2.5-pro") or "gemini-2.5-pro"


def _task_model(name: str, fallback: str) -> str:
    provider = _default_provider()
    provider_default = _default_model_for_provider(provider)
    return _env(name) or provider_default or fallback

# === 配置类定义 ===
class EpicSettings(AgentConfig):
    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True, extra="ignore")

    GEMINI_API_KEY: SecretStr | None = Field(
        default_factory=lambda: _env("GEMINI_API_KEY"),
        description="Gemini/AiHubMix API key",
    )

    GEMINI_BASE_URL: str = Field(
        default_factory=lambda: _env("GEMINI_BASE_URL", "https://aihubmix.com"),
        description="Gemini/AiHubMix base URL",
    )

    GEMINI_MODEL: str = Field(
        default_factory=lambda: _env("GEMINI_MODEL", "gemini-2.5-pro"),
        description="Gemini default model",
    )

    LLM_PROVIDER: str = Field(
        default_factory=_default_provider,
        description="Supported values: gemini, glm",
    )

    GLM_API_KEY: SecretStr | None = Field(
        default_factory=lambda: _env("GLM_API_KEY"),
        description="GLM API key",
    )

    GLM_BASE_URL: str = Field(
        default_factory=lambda: _env("GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"),
        description="GLM OpenAI-compatible base URL",
    )

    GLM_MODEL: str = Field(
        default_factory=lambda: _env("GLM_MODEL", "glm-4.5v"),
        description="GLM vision-capable default model",
    )

    EPIC_EMAIL: str = Field(default_factory=lambda: _env("EPIC_EMAIL"))
    EPIC_PASSWORD: SecretStr = Field(default_factory=lambda: _env("EPIC_PASSWORD"))
    DISABLE_BEZIER_TRAJECTORY: bool = Field(default=True)
    WAIT_FOR_CHALLENGE_VIEW_TO_RENDER_MS: int = Field(default=3000)

    CHALLENGE_CLASSIFIER_MODEL: str = Field(
        default_factory=lambda: _task_model("CHALLENGE_CLASSIFIER_MODEL", "gemini-2.5-flash")
    )
    IMAGE_CLASSIFIER_MODEL: str = Field(
        default_factory=lambda: _task_model("IMAGE_CLASSIFIER_MODEL", "gemini-2.5-pro")
    )
    SPATIAL_POINT_REASONER_MODEL: str = Field(
        default_factory=lambda: _task_model("SPATIAL_POINT_REASONER_MODEL", "gemini-2.5-pro")
    )
    SPATIAL_PATH_REASONER_MODEL: str = Field(
        default_factory=lambda: _task_model("SPATIAL_PATH_REASONER_MODEL", "gemini-2.5-pro")
    )

    cache_dir: Path = HCAPTCHA_DIR.joinpath(".cache")
    challenge_dir: Path = HCAPTCHA_DIR.joinpath(".challenge")
    captcha_response_dir: Path = HCAPTCHA_DIR.joinpath(".captcha")

    ENABLE_APSCHEDULER: bool = Field(default=True)
    TASK_TIMEOUT_SECONDS: int = Field(default=900)
    REDIS_URL: str = Field(default="redis://redis:6379/0")
    CELERY_WORKER_CONCURRENCY: int = Field(default=1)
    CELERY_TASK_TIME_LIMIT: int = Field(default=1200)
    CELERY_TASK_SOFT_TIME_LIMIT: int = Field(default=900)

    @property
    def user_data_dir(self) -> Path:
        target_ = USER_DATA_DIR.joinpath(self.EPIC_EMAIL)
        target_.mkdir(parents=True, exist_ok=True)
        return target_

settings = EpicSettings()
settings.ignore_request_questions = ["Please drag the crossing to complete the lines"]
apply_llm_patch(settings)
