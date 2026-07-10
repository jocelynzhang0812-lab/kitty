from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from kitty.agent.providers.mock import MockProvider
from kitty.agent.providers.openai_compatible import OpenAICompatibleProvider
from kitty.core.config import KittyConfig
from kitty.runtime import KittyRuntime


class DeploymentConfigError(ValueError):
    pass


@dataclass(slots=True)
class DeploymentSettings:
    environment: str = "development"
    agent_mode: str = "csbot"
    project_root: Path = field(default_factory=lambda: Path(__file__).resolve().parents[2])
    model_provider: str = "openai_compatible"
    model_api_key: str = field(default="", repr=False)
    model_base_url: str = "https://api.moonshot.cn/v1"
    model_name: str = "kimi-latest"
    model_timeout_seconds: float = 120.0
    model_max_retries: int = 2
    cs_tool_timeout_seconds: float = 30.0
    cs_feedback_timeout_seconds: float = 15.0
    feishu_app_id: str = ""
    feishu_app_secret: str = field(default="", repr=False)
    feishu_verification_token: str = field(default="", repr=False)
    feishu_encrypt_key: str = field(default="", repr=False)
    feishu_require_mention: bool = True
    feishu_max_clock_skew_seconds: int = 300
    debug_api_token: str = field(default="", repr=False)
    delivery_max_attempts: int = 5
    delivery_retry_base_seconds: float = 1.0
    bitable_app_token: str = field(default="", repr=False)
    bitable_table_id: str = field(default="", repr=False)
    internal_debug_chat_id: str = ""
    feedback_bot_user_id: str = ""
    load_feedback_hook: bool = False

    @classmethod
    def from_env(cls) -> "DeploymentSettings":
        environment = os.getenv("KITTY_ENV", "development").strip().lower()
        api_key = os.getenv("LLM_API_KEY") or os.getenv("KIMI_API_KEY", "")
        default_agent_mode = "csbot" if environment == "production" or api_key else "generic"
        default_provider = "openai_compatible" if api_key else "mock"
        project_root = Path(
            os.getenv("KITTY_PROJECT_ROOT", str(Path(__file__).resolve().parents[2]))
        ).expanduser().resolve()
        return cls(
            environment=environment,
            agent_mode=os.getenv("KITTY_AGENT_MODE", default_agent_mode).strip().lower(),
            project_root=project_root,
            model_provider=os.getenv("KITTY_MODEL_PROVIDER", default_provider).strip().lower(),
            model_api_key=api_key,
            model_base_url=os.getenv("LLM_BASE_URL")
            or os.getenv("KIMI_BASE_URL", "https://api.moonshot.cn/v1"),
            model_name=os.getenv("LLM_MODEL") or os.getenv("KIMI_MODEL", "kimi-latest"),
            model_timeout_seconds=float(os.getenv("LLM_TIMEOUT_SECONDS", "120")),
            model_max_retries=int(os.getenv("LLM_MAX_RETRIES", "2")),
            cs_tool_timeout_seconds=float(os.getenv("CS_TOOL_TIMEOUT_SECONDS", "30")),
            cs_feedback_timeout_seconds=float(
                os.getenv("CS_FEEDBACK_TIMEOUT_SECONDS", "15")
            ),
            feishu_app_id=os.getenv("FEISHU_APP_ID", ""),
            feishu_app_secret=os.getenv("FEISHU_APP_SECRET", ""),
            feishu_verification_token=os.getenv("FEISHU_VERIFICATION_TOKEN", ""),
            feishu_encrypt_key=os.getenv("FEISHU_ENCRYPT_KEY", ""),
            feishu_require_mention=os.getenv("FEISHU_REQUIRE_MENTION", "1") != "0",
            feishu_max_clock_skew_seconds=int(
                os.getenv("FEISHU_MAX_CLOCK_SKEW_SECONDS", "300")
            ),
            debug_api_token=os.getenv("KITTY_DEBUG_API_TOKEN", ""),
            delivery_max_attempts=int(os.getenv("KITTY_DELIVERY_MAX_ATTEMPTS", "5")),
            delivery_retry_base_seconds=float(
                os.getenv("KITTY_DELIVERY_RETRY_BASE_SECONDS", "1")
            ),
            bitable_app_token=os.getenv("BITABLE_APP_TOKEN", ""),
            bitable_table_id=os.getenv("BITABLE_TABLE_ID", ""),
            internal_debug_chat_id=os.getenv("INTERNAL_DEBUG_CHAT_ID", ""),
            feedback_bot_user_id=os.getenv("FEEDBACK_BOT_USER_ID", ""),
            load_feedback_hook=os.getenv("KITTY_CSBOT_FEEDBACK_HOOK", "0") == "1",
        )

    def validate(self) -> None:
        errors: list[str] = []
        if self.environment not in {"development", "test", "production"}:
            errors.append("KITTY_ENV must be development, test, or production")
        if self.agent_mode not in {"csbot", "generic"}:
            errors.append("KITTY_AGENT_MODE must be csbot or generic")
        if self.model_provider not in {"mock", "openai_compatible"}:
            errors.append("KITTY_MODEL_PROVIDER must be mock or openai_compatible")
        if self.feishu_max_clock_skew_seconds < 0:
            errors.append("FEISHU_MAX_CLOCK_SKEW_SECONDS cannot be negative")
        if self.delivery_max_attempts < 1:
            errors.append("KITTY_DELIVERY_MAX_ATTEMPTS must be at least 1")
        if self.delivery_retry_base_seconds <= 0:
            errors.append("KITTY_DELIVERY_RETRY_BASE_SECONDS must be positive")
        if self.model_timeout_seconds <= 0:
            errors.append("LLM_TIMEOUT_SECONDS must be positive")
        if self.model_max_retries < 0:
            errors.append("LLM_MAX_RETRIES cannot be negative")
        if self.cs_tool_timeout_seconds <= 0:
            errors.append("CS_TOOL_TIMEOUT_SECONDS must be positive")
        if self.cs_feedback_timeout_seconds <= 0:
            errors.append("CS_FEEDBACK_TIMEOUT_SECONDS must be positive")
        if not self.project_root.is_dir():
            errors.append(f"KITTY_PROJECT_ROOT does not exist: {self.project_root}")

        if self.environment == "production":
            required = {
                "FEISHU_APP_ID": self.feishu_app_id,
                "FEISHU_APP_SECRET": self.feishu_app_secret,
                "FEISHU_VERIFICATION_TOKEN": self.feishu_verification_token,
                "FEISHU_ENCRYPT_KEY": self.feishu_encrypt_key,
                "LLM_API_KEY or KIMI_API_KEY": self.model_api_key,
                "LLM_MODEL or KIMI_MODEL": self.model_name,
            }
            if self.agent_mode == "csbot":
                required.update(
                    {
                        "BITABLE_APP_TOKEN": self.bitable_app_token,
                        "BITABLE_TABLE_ID": self.bitable_table_id,
                        "INTERNAL_DEBUG_CHAT_ID": self.internal_debug_chat_id,
                        "FEEDBACK_BOT_USER_ID": self.feedback_bot_user_id,
                    }
                )
            for name, value in required.items():
                if not value:
                    errors.append(f"missing production setting: {name}")
            if self.model_provider == "mock":
                errors.append("mock model provider is forbidden in production")

        if self.model_provider == "openai_compatible":
            if not self.model_api_key:
                errors.append("model API key is required for openai_compatible provider")
            if not self.model_name:
                errors.append("model name is required for openai_compatible provider")

        if errors:
            raise DeploymentConfigError("; ".join(errors))

    def public_summary(self) -> dict[str, object]:
        return {
            "environment": self.environment,
            "agent_mode": self.agent_mode,
            "model_provider": self.model_provider,
            "model": self.model_name,
            "feishu_encryption": bool(self.feishu_encrypt_key),
            "feishu_verification": bool(self.feishu_verification_token),
            "bitable_configured": bool(self.bitable_app_token and self.bitable_table_id),
            "debug_api_enabled": self.environment != "production"
            or bool(self.debug_api_token),
            "delivery_max_attempts": self.delivery_max_attempts,
        }


def build_runtime(settings: DeploymentSettings) -> KittyRuntime:
    settings.validate()
    config = KittyConfig.from_env()

    if settings.agent_mode == "csbot":
        from kitty.integrations.csbot import CSBotTurnHandler

        handler = CSBotTurnHandler(settings.project_root)
        runtime = KittyRuntime(
            config=config,
            project_root=settings.project_root,
            turn_handler=handler,
        )
    else:
        provider = (
            MockProvider()
            if settings.model_provider == "mock"
            else OpenAICompatibleProvider(
                api_key=settings.model_api_key,
                base_url=settings.model_base_url,
                model=settings.model_name,
            )
        )
        runtime = KittyRuntime(
            config=config,
            provider=provider,
            project_root=settings.project_root,
        )

    if settings.load_feedback_hook:
        runtime.load_hook(settings.project_root / "csbot" / "hooks" / "feedback_hook.py")
    return runtime
