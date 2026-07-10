from __future__ import annotations

import importlib
import inspect
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from kitty.agent.providers.mock import MockProvider
from kitty.agent.providers.openai_compatible import OpenAICompatibleProvider
from kitty.core.config import KittyConfig
from kitty.runtime import KittyRuntime


class DeploymentConfigError(ValueError):
    pass


def _csv_env(name: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in os.getenv(name, "").split(",") if item.strip())


@dataclass(slots=True)
class DeploymentSettings:
    environment: str = "development"
    project_root: Path = field(default_factory=lambda: Path(__file__).resolve().parents[2])
    model_provider: str = "openai_compatible"
    model_api_key: str = field(default="", repr=False)
    model_base_url: str = "https://api.openai.com/v1"
    model_name: str = ""
    model_timeout_seconds: float = 120.0
    feishu_app_id: str = ""
    feishu_app_secret: str = field(default="", repr=False)
    feishu_verification_token: str = field(default="", repr=False)
    feishu_encrypt_key: str = field(default="", repr=False)
    feishu_require_mention: bool = True
    feishu_accept_images: bool = False
    feishu_max_clock_skew_seconds: int = 300
    debug_api_token: str = field(default="", repr=False)
    delivery_max_attempts: int = 5
    delivery_retry_base_seconds: float = 1.0
    tool_modules: tuple[str, ...] = ()
    hook_paths: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> "DeploymentSettings":
        environment = os.getenv("KITTY_ENV", "development").strip().lower()
        api_key = os.getenv("LLM_API_KEY", "")
        default_provider = "openai_compatible" if api_key else "mock"
        project_root = Path(
            os.getenv("KITTY_PROJECT_ROOT", str(Path(__file__).resolve().parents[2]))
        ).expanduser().resolve()
        return cls(
            environment=environment,
            project_root=project_root,
            model_provider=os.getenv("KITTY_MODEL_PROVIDER", default_provider).strip().lower(),
            model_api_key=api_key,
            model_base_url=os.getenv("LLM_BASE_URL", "https://api.openai.com/v1"),
            model_name=os.getenv("LLM_MODEL", ""),
            model_timeout_seconds=float(os.getenv("LLM_TIMEOUT_SECONDS", "120")),
            feishu_app_id=os.getenv("FEISHU_APP_ID", ""),
            feishu_app_secret=os.getenv("FEISHU_APP_SECRET", ""),
            feishu_verification_token=os.getenv("FEISHU_VERIFICATION_TOKEN", ""),
            feishu_encrypt_key=os.getenv("FEISHU_ENCRYPT_KEY", ""),
            feishu_require_mention=os.getenv("FEISHU_REQUIRE_MENTION", "1") != "0",
            feishu_accept_images=os.getenv("FEISHU_ACCEPT_IMAGES", "0") == "1",
            feishu_max_clock_skew_seconds=int(
                os.getenv("FEISHU_MAX_CLOCK_SKEW_SECONDS", "300")
            ),
            debug_api_token=os.getenv("KITTY_DEBUG_API_TOKEN", ""),
            delivery_max_attempts=int(os.getenv("KITTY_DELIVERY_MAX_ATTEMPTS", "5")),
            delivery_retry_base_seconds=float(
                os.getenv("KITTY_DELIVERY_RETRY_BASE_SECONDS", "1")
            ),
            tool_modules=_csv_env("KITTY_TOOL_MODULES"),
            hook_paths=_csv_env("KITTY_HOOK_PATHS"),
        )

    def validate(self) -> None:
        errors: list[str] = []
        if self.environment not in {"development", "test", "production"}:
            errors.append("KITTY_ENV must be development, test, or production")
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
        if not self.project_root.is_dir():
            errors.append(f"KITTY_PROJECT_ROOT does not exist: {self.project_root}")

        if self.environment == "production":
            required = {
                "FEISHU_APP_ID": self.feishu_app_id,
                "FEISHU_APP_SECRET": self.feishu_app_secret,
                "FEISHU_VERIFICATION_TOKEN": self.feishu_verification_token,
                "FEISHU_ENCRYPT_KEY": self.feishu_encrypt_key,
                "LLM_API_KEY": self.model_api_key,
                "LLM_MODEL": self.model_name,
            }
            for name, value in required.items():
                if not value:
                    errors.append(f"missing production setting: {name}")
            if self.model_provider == "mock":
                errors.append("mock model provider is forbidden in production")

        if self.model_provider == "openai_compatible":
            if not self.model_api_key:
                errors.append("LLM_API_KEY is required for openai_compatible provider")
            if not self.model_name:
                errors.append("LLM_MODEL is required for openai_compatible provider")

        if errors:
            raise DeploymentConfigError("; ".join(errors))

    def public_summary(self) -> dict[str, object]:
        return {
            "environment": self.environment,
            "model_provider": self.model_provider,
            "model": self.model_name,
            "feishu_encryption": bool(self.feishu_encrypt_key),
            "feishu_verification": bool(self.feishu_verification_token),
            "debug_api_enabled": self.environment != "production"
            or bool(self.debug_api_token),
            "delivery_max_attempts": self.delivery_max_attempts,
            "tool_modules": list(self.tool_modules),
            "hooks": len(self.hook_paths),
        }


def build_runtime(settings: DeploymentSettings) -> KittyRuntime:
    settings.validate()
    config = KittyConfig.from_env()
    provider = (
        MockProvider()
        if settings.model_provider == "mock"
        else OpenAICompatibleProvider(
            api_key=settings.model_api_key,
            base_url=settings.model_base_url,
            model=settings.model_name,
            timeout_seconds=settings.model_timeout_seconds,
        )
    )
    runtime = KittyRuntime(
        config=config,
        provider=provider,
        project_root=settings.project_root,
    )
    _load_tool_modules(runtime, settings)
    for hook_path in settings.hook_paths:
        path = Path(hook_path).expanduser()
        runtime.load_hook(path if path.is_absolute() else settings.project_root / path)
    return runtime


def _load_tool_modules(runtime: KittyRuntime, settings: DeploymentSettings) -> None:
    root = str(settings.project_root)
    if root not in sys.path:
        sys.path.insert(0, root)
    for module_name in settings.tool_modules:
        module = importlib.import_module(module_name)
        register = getattr(module, "register_tools", None)
        if not callable(register):
            raise DeploymentConfigError(
                f"tool module {module_name!r} must expose register_tools(registry)"
            )
        result = register(runtime.tools)
        if inspect.isawaitable(result):
            raise DeploymentConfigError(
                f"tool module {module_name!r} register_tools must be synchronous"
            )
