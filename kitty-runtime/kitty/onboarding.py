from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import os
import secrets
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from kitty.config_file import write_env_file
from kitty.deployment import DeploymentConfigError, DeploymentSettings
from kitty.hooks.bus import HookBus
from kitty.hooks.loader import load_hook
from kitty.tools.registry import ToolRegistry


JsonPoster = Callable[[str, dict[str, str], dict[str, Any], float], dict[str, Any]]


@dataclass(slots=True, frozen=True)
class DoctorCheck:
    key: str
    label: str
    ok: bool
    detail: str
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class OnboardingService:
    def __init__(
        self,
        *,
        project_root: str | Path,
        output_path: str | Path,
        json_poster: JsonPoster | None = None,
        request_timeout_seconds: float = 15.0,
    ):
        self.project_root = Path(project_root).expanduser().resolve()
        self.output_path = Path(output_path).expanduser().resolve()
        self.json_poster = json_poster or _post_json
        self.request_timeout_seconds = request_timeout_seconds

    def environment_values(self, payload: Mapping[str, Any]) -> dict[str, str]:
        bot_name = str(payload.get("bot_name") or "").strip()
        purpose = str(payload.get("purpose") or "").strip()
        prompt = str(payload.get("system_prompt") or "").strip()
        if not prompt and bot_name and purpose:
            prompt = (
                f"You are {bot_name}, a Feishu assistant. "
                f"Your purpose is: {purpose}. "
                "Be concise, helpful, and honest. Never invent tool results."
            )
        public_url = str(payload.get("public_base_url") or "").strip().rstrip("/")
        values = {
            "KITTY_ENV": "production",
            "KITTY_BOT_NAME": bot_name,
            "KITTY_MODEL_PROVIDER": "openai_compatible",
            "KITTY_PROJECT_ROOT": str(self.project_root),
            "KITTY_STATE_DIR": str(
                payload.get("state_dir") or self.project_root / ".kitty"
            ).strip(),
            "KITTY_SYSTEM_PROMPT": prompt,
            "KITTY_PUBLIC_BASE_URL": public_url,
            "KITTY_TOOL_MODULES": str(payload.get("tool_modules") or "").strip(),
            "KITTY_HOOK_PATHS": str(payload.get("hook_paths") or "").strip(),
            "LLM_API_KEY": str(payload.get("model_api_key") or "").strip(),
            "LLM_BASE_URL": str(payload.get("model_base_url") or "").strip().rstrip("/"),
            "LLM_MODEL": str(payload.get("model_name") or "").strip(),
            "LLM_TIMEOUT_SECONDS": "120",
            "FEISHU_APP_ID": str(payload.get("feishu_app_id") or "").strip(),
            "FEISHU_APP_SECRET": str(payload.get("feishu_app_secret") or "").strip(),
            "FEISHU_VERIFICATION_TOKEN": str(
                payload.get("feishu_verification_token") or ""
            ).strip(),
            "FEISHU_ENCRYPT_KEY": str(payload.get("feishu_encrypt_key") or "").strip(),
            "FEISHU_REQUIRE_MENTION": "1"
            if _as_bool(payload.get("require_mention"), True)
            else "0",
            "FEISHU_ACCEPT_IMAGES": "1"
            if _as_bool(payload.get("accept_images"), False)
            else "0",
            "FEISHU_MAX_CLOCK_SKEW_SECONDS": "300",
            "KITTY_DELIVERY_MAX_ATTEMPTS": "5",
            "KITTY_DELIVERY_RETRY_BASE_SECONDS": "1",
        }
        return values

    def payload_from_environment(self, values: Mapping[str, str]) -> dict[str, Any]:
        return {
            "bot_name": values.get("KITTY_BOT_NAME", "Kitty Bot"),
            "state_dir": values.get("KITTY_STATE_DIR", ""),
            "purpose": "",
            "system_prompt": values.get("KITTY_SYSTEM_PROMPT", ""),
            "public_base_url": values.get("KITTY_PUBLIC_BASE_URL", ""),
            "tool_modules": values.get("KITTY_TOOL_MODULES", ""),
            "hook_paths": values.get("KITTY_HOOK_PATHS", ""),
            "model_api_key": values.get("LLM_API_KEY", ""),
            "model_base_url": values.get("LLM_BASE_URL", ""),
            "model_name": values.get("LLM_MODEL", ""),
            "feishu_app_id": values.get("FEISHU_APP_ID", ""),
            "feishu_app_secret": values.get("FEISHU_APP_SECRET", ""),
            "feishu_verification_token": values.get("FEISHU_VERIFICATION_TOKEN", ""),
            "feishu_encrypt_key": values.get("FEISHU_ENCRYPT_KEY", ""),
            "require_mention": values.get("FEISHU_REQUIRE_MENTION", "1") != "0",
            "accept_images": values.get("FEISHU_ACCEPT_IMAGES", "0") == "1",
        }

    def validate(self, payload: Mapping[str, Any]) -> list[str]:
        values = self.environment_values(payload)
        errors: list[str] = []
        required = {
            "机器人名称": values["KITTY_BOT_NAME"],
            "机器人职责": values["KITTY_SYSTEM_PROMPT"],
            "模型 API Key": values["LLM_API_KEY"],
            "模型 Base URL": values["LLM_BASE_URL"],
            "模型名称": values["LLM_MODEL"],
            "飞书 App ID": values["FEISHU_APP_ID"],
            "飞书 App Secret": values["FEISHU_APP_SECRET"],
            "Verification Token": values["FEISHU_VERIFICATION_TOKEN"],
            "Encrypt Key": values["FEISHU_ENCRYPT_KEY"],
            "公网 HTTPS 地址": values["KITTY_PUBLIC_BASE_URL"],
        }
        errors.extend(f"缺少{label}" for label, value in required.items() if not value)
        model_url = values["LLM_BASE_URL"]
        if model_url and not _is_http_url(model_url):
            errors.append("模型 Base URL 必须以 http:// 或 https:// 开头")
        public_url = values["KITTY_PUBLIC_BASE_URL"]
        if public_url and not public_url.startswith("https://"):
            errors.append("飞书回调地址必须使用 https://")
        # The deployment validator is deliberately deferred until the required
        # fields exist. Otherwise the setup UI would repeat every missing field
        # in both user-facing and internal configuration terminology.
        if not errors:
            try:
                self._settings(values).validate()
            except (DeploymentConfigError, ValueError) as exc:
                errors.extend(part.strip() for part in str(exc).split(";") if part.strip())
        return list(dict.fromkeys(errors))

    async def check(self, payload: Mapping[str, Any], *, live: bool = True) -> dict[str, Any]:
        values = self.environment_values(payload)
        validation_errors = self.validate(payload)
        checks = [
            DoctorCheck(
                "configuration",
                "配置完整性",
                not validation_errors,
                "配置字段完整" if not validation_errors else "；".join(validation_errors),
            )
        ]
        checks.append(await asyncio.to_thread(self._check_storage, values))
        checks.append(await asyncio.to_thread(self._check_extensions, values))
        if live and not validation_errors:
            model, feishu = await asyncio.gather(
                asyncio.to_thread(self._check_model, values),
                asyncio.to_thread(self._check_feishu, values),
            )
            checks.extend([model, feishu])
        elif live:
            checks.extend(
                [
                    DoctorCheck("model", "模型连接", False, "请先修复配置字段"),
                    DoctorCheck("feishu", "飞书连接", False, "请先修复配置字段"),
                ]
            )
        result = [item.to_dict() for item in checks]
        return {"ready": all(item["ok"] for item in result), "checks": result}

    def save(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        errors = self.validate(payload)
        if errors:
            raise ValueError("；".join(errors))
        values = self.environment_values(payload)
        saved = write_env_file(self.output_path, values)
        callback_url = f"{values['KITTY_PUBLIC_BASE_URL']}/feishu/events"
        return {
            "saved": True,
            "path": str(saved),
            "callback_url": callback_url,
            "doctor_command": f"kitty doctor --env-file {saved} --live",
            "serve_command": f"kitty serve --env-file {saved}",
        }

    def _settings(self, values: Mapping[str, str]) -> DeploymentSettings:
        return DeploymentSettings(
            environment=values["KITTY_ENV"],
            project_root=Path(values["KITTY_PROJECT_ROOT"]),
            bot_name=values["KITTY_BOT_NAME"],
            public_base_url=values["KITTY_PUBLIC_BASE_URL"],
            model_provider=values["KITTY_MODEL_PROVIDER"],
            model_api_key=values["LLM_API_KEY"],
            model_base_url=values["LLM_BASE_URL"],
            model_name=values["LLM_MODEL"],
            feishu_app_id=values["FEISHU_APP_ID"],
            feishu_app_secret=values["FEISHU_APP_SECRET"],
            feishu_verification_token=values["FEISHU_VERIFICATION_TOKEN"],
            feishu_encrypt_key=values["FEISHU_ENCRYPT_KEY"],
            feishu_require_mention=values["FEISHU_REQUIRE_MENTION"] == "1",
            feishu_accept_images=values["FEISHU_ACCEPT_IMAGES"] == "1",
            tool_modules=tuple(
                item.strip()
                for item in values["KITTY_TOOL_MODULES"].split(",")
                if item.strip()
            ),
            hook_paths=tuple(
                item.strip()
                for item in values["KITTY_HOOK_PATHS"].split(",")
                if item.strip()
            ),
        )

    def _check_storage(self, values: Mapping[str, str]) -> DoctorCheck:
        started = time.monotonic()
        try:
            state_dir = Path(values["KITTY_STATE_DIR"]).expanduser().resolve()
            state_dir.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(prefix=".kitty-doctor-", dir=state_dir)
            os.close(descriptor)
            Path(name).unlink()
            return _timed_check("storage", "持久化目录", True, str(state_dir), started)
        except Exception as exc:
            return _timed_check("storage", "持久化目录", False, _safe_error(exc), started)

    def _check_extensions(self, values: Mapping[str, str]) -> DoctorCheck:
        started = time.monotonic()
        try:
            root = str(self.project_root)
            if root not in sys.path:
                sys.path.insert(0, root)
            registry = ToolRegistry()
            modules = [
                item.strip() for item in values["KITTY_TOOL_MODULES"].split(",") if item.strip()
            ]
            for module_name in modules:
                module = importlib.import_module(module_name)
                register = getattr(module, "register_tools", None)
                if not callable(register):
                    raise ValueError(f"{module_name} 缺少 register_tools(registry)")
                register(registry)
            hooks = [
                item.strip() for item in values["KITTY_HOOK_PATHS"].split(",") if item.strip()
            ]
            for hook_path in hooks:
                path = Path(hook_path).expanduser()
                load_hook(path if path.is_absolute() else self.project_root / path, HookBus())
            detail = f"{len(registry.schemas())} 个工具，{len(hooks)} 个 Hook"
            return _timed_check("extensions", "扩展模块", True, detail, started)
        except Exception as exc:
            return _timed_check("extensions", "扩展模块", False, _safe_error(exc), started)

    def _check_model(self, values: Mapping[str, str]) -> DoctorCheck:
        started = time.monotonic()
        try:
            body = self.json_poster(
                f"{values['LLM_BASE_URL'].rstrip('/')}/chat/completions",
                {"Authorization": f"Bearer {values['LLM_API_KEY']}"},
                {
                    "model": values["LLM_MODEL"],
                    "messages": [{"role": "user", "content": "Reply OK"}],
                    "max_tokens": 2,
                    "temperature": 0,
                },
                self.request_timeout_seconds,
            )
            if not body.get("choices"):
                raise ValueError("模型响应缺少 choices")
            return _timed_check("model", "模型连接", True, "模型可用", started)
        except Exception as exc:
            return _timed_check("model", "模型连接", False, _safe_error(exc), started)

    def _check_feishu(self, values: Mapping[str, str]) -> DoctorCheck:
        started = time.monotonic()
        try:
            body = self.json_poster(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                {},
                {
                    "app_id": values["FEISHU_APP_ID"],
                    "app_secret": values["FEISHU_APP_SECRET"],
                },
                self.request_timeout_seconds,
            )
            if body.get("code") != 0 or not body.get("tenant_access_token"):
                raise ValueError(
                    f"飞书返回 code={body.get('code')} msg={body.get('msg', '')}"
                )
            return _timed_check("feishu", "飞书连接", True, "应用凭据有效", started)
        except Exception as exc:
            return _timed_check("feishu", "飞书连接", False, _safe_error(exc), started)


class OnboardingASGIApp:
    def __init__(self, service: OnboardingService, *, setup_token: str | None = None):
        self.service = service
        self.setup_token = setup_token or secrets.token_urlsafe(24)
        self._verified_digest = ""

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            return
        method = scope.get("method", "GET").upper()
        path = scope.get("path", "/")
        headers = _headers(scope)
        if method == "GET" and path == "/":
            query = urllib.parse.parse_qs(scope.get("query_string", b"").decode("utf-8"))
            if not secrets.compare_digest(str(query.get("token", [""])[0]), self.setup_token):
                await _respond_text(send, 403, "Setup token required")
                return
            await _respond_html(send, SETUP_HTML.replace("__SETUP_TOKEN__", self.setup_token))
            return
        if path.startswith("/api/"):
            supplied = headers.get("x-kitty-setup-token", "")
            if not secrets.compare_digest(supplied, self.setup_token):
                await _respond_json(send, 403, {"ok": False, "error": "invalid setup token"})
                return
            try:
                payload = await _read_json(receive)
                if method == "POST" and path == "/api/check":
                    result = await self.service.check(payload, live=True)
                    self._verified_digest = _payload_digest(payload) if result["ready"] else ""
                elif method == "POST" and path == "/api/save":
                    if not self._verified_digest or not secrets.compare_digest(
                        self._verified_digest, _payload_digest(payload)
                    ):
                        raise ValueError("配置已变化，请重新执行发布前检查")
                    result = self.service.save(payload)
                else:
                    await _respond_json(send, 404, {"ok": False, "error": "not found"})
                    return
                await _respond_json(send, 200, {"ok": True, **result})
            except ValueError as exc:
                await _respond_json(send, 400, {"ok": False, "error": str(exc)})
            except Exception:
                await _respond_json(send, 500, {"ok": False, "error": "setup failed"})
            return
        await _respond_json(send, 404, {"ok": False, "error": "not found"})


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _payload_digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_http_url(value: str) -> bool:
    parsed = urllib.parse.urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _post_json(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: float,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", **headers},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    if not isinstance(result, dict):
        raise ValueError("服务返回了无效 JSON")
    return result


def _safe_error(exc: Exception) -> str:
    text = str(exc).replace("\n", " ").replace("\r", " ").strip()
    return f"{type(exc).__name__}: {text[:300]}"


def _timed_check(
    key: str,
    label: str,
    ok: bool,
    detail: str,
    started: float,
) -> DoctorCheck:
    return DoctorCheck(key, label, ok, detail, int((time.monotonic() - started) * 1000))


def _headers(scope) -> dict[str, str]:
    return {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in scope.get("headers", [])
    }


async def _read_json(receive, max_bytes: int = 64 * 1024) -> dict[str, Any]:
    body = bytearray()
    more = True
    while more:
        message = await receive()
        if message["type"] != "http.request":
            continue
        body.extend(message.get("body", b""))
        if len(body) > max_bytes:
            raise ValueError("setup payload is too large")
        more = bool(message.get("more_body", False))
    try:
        value = json.loads(bytes(body or b"{}").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("request body must be valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("request body must be an object")
    return value


async def _respond_json(send, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    await _respond(send, status, body, b"application/json; charset=utf-8")


async def _respond_html(send, html: str) -> None:
    await _respond(send, 200, html.encode("utf-8"), b"text/html; charset=utf-8")


async def _respond_text(send, status: int, value: str) -> None:
    await _respond(send, status, value.encode("utf-8"), b"text/plain; charset=utf-8")


async def _respond(send, status: int, body: bytes, content_type: bytes) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", content_type),
                (b"cache-control", b"no-store"),
                (b"x-frame-options", b"DENY"),
                (
                    b"content-security-policy",
                    b"default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'",
                ),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


SETUP_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kitty · 10 分钟上线</title>
<style>
:root{--ink:#17211b;--muted:#67736c;--line:#dce4df;--paper:#fbfcfa;--brand:#166534;--soft:#eaf5ed;--danger:#b42318}
*{box-sizing:border-box}body{margin:0;background:linear-gradient(135deg,#eef7ef,#f8f7f2 52%,#edf5f7);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:var(--ink)}
.shell{max-width:1080px;margin:0 auto;padding:32px 20px 56px}.hero{display:flex;justify-content:space-between;gap:24px;align-items:flex-end;margin-bottom:22px}.badge{display:inline-block;padding:5px 10px;border-radius:99px;background:#dff2e3;color:var(--brand);font-weight:700;font-size:12px}.hero h1{margin:10px 0 4px;font-size:34px;letter-spacing:-1px}.hero p{margin:0;color:var(--muted)}
.layout{display:grid;grid-template-columns:190px 1fr;gap:18px}.steps,.panel{background:rgba(255,255,255,.9);border:1px solid var(--line);border-radius:18px;box-shadow:0 12px 35px rgba(36,58,45,.08)}.steps{padding:14px;height:max-content;position:sticky;top:18px}.step{padding:12px;border-radius:12px;color:var(--muted);font-weight:650}.step.active{background:var(--soft);color:var(--brand)}.step small{display:block;font-weight:400;margin-top:2px}.panel{padding:25px}.section{display:none}.section.active{display:block}.section h2{margin:0 0 5px;font-size:22px}.section>p{margin:0 0 22px;color:var(--muted)}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:15px}.full{grid-column:1/-1}label{display:block;font-weight:650;margin-bottom:6px}input,textarea,select{width:100%;border:1px solid #cfd9d2;border-radius:10px;background:#fff;padding:11px 12px;font:inherit;color:inherit;outline:none}input:focus,textarea:focus{border-color:#5b9a70;box-shadow:0 0 0 3px #e5f3e9}textarea{min-height:108px;resize:vertical}.hint{font-size:12px;color:var(--muted);margin-top:5px}.switch{display:flex;gap:10px;align-items:center;padding:11px 0}.switch input{width:auto}
.actions{display:flex;justify-content:space-between;gap:12px;margin-top:25px;padding-top:18px;border-top:1px solid var(--line)}button{border:0;border-radius:10px;padding:11px 17px;font:inherit;font-weight:700;cursor:pointer}.primary{background:var(--brand);color:white}.secondary{background:#eef2ef;color:var(--ink)}button:disabled{opacity:.45;cursor:not-allowed}.checks{display:grid;gap:10px;margin:16px 0}.check{padding:12px 14px;border:1px solid var(--line);border-radius:12px;display:flex;justify-content:space-between;gap:14px}.ok{color:var(--brand)}.bad{color:var(--danger)}pre{white-space:pre-wrap;background:#132019;color:#e8f3eb;border-radius:12px;padding:14px;overflow:auto}.success{padding:15px;border-radius:12px;background:var(--soft);color:var(--brand);display:none}.error{padding:12px;border-radius:10px;background:#fff0ee;color:var(--danger);display:none;margin-top:12px}
@media(max-width:760px){.layout{grid-template-columns:1fr}.steps{position:static;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:4px}.step{min-width:0;padding:10px;font-size:14px}.panel{padding:20px}.grid{grid-template-columns:1fr}.full{grid-column:auto}.hero{display:block}.hero h1{font-size:34px}}
</style>
</head>
<body><main class="shell">
<header class="hero"><div><span class="badge">LOCAL SETUP · 密钥只保存在本机</span><h1>让 Kitty 在 10 分钟内上线</h1><p>填写、检测、保存，然后把回调地址粘贴到飞书。</p></div><div id="clock">步骤 1 / 4</div></header>
<div class="layout"><nav class="steps">
<div class="step active" data-nav="0">1. 定义机器人<small>名称与职责</small></div><div class="step" data-nav="1">2. 连接模型<small>OpenAI-compatible</small></div><div class="step" data-nav="2">3. 连接飞书<small>应用与回调</small></div><div class="step" data-nav="3">4. 检测发布<small>一键 Doctor</small></div>
</nav><section class="panel">
<div class="section active">
<h2>这个机器人要做什么？</h2><p>先描述目标，Kitty 会生成可编辑的初始角色设定。</p>
<div class="grid"><div><label>机器人名称</label><input id="bot_name" value="团队助手" placeholder="例如：项目助手"></div><div><label>模板</label><select id="template"><option value="general">通用内部助手</option><option value="knowledge">知识问答助手</option><option value="workflow">流程与审批助手</option></select></div><div class="full"><label>主要职责</label><input id="purpose" value="回答团队问题，并在需要时调用已授权的工具" placeholder="一句话说明它负责什么"></div><div class="full"><label>System Prompt</label><textarea id="system_prompt"></textarea><div class="hint">可以继续编辑。不要在这里放 API Key。</div></div></div>
</div>
<div class="section"><h2>连接模型</h2><p>支持任何 OpenAI-compatible Chat Completions API，检测只消耗一个极小请求。</p><div class="grid"><div class="full"><label>Base URL</label><input id="model_base_url" value="https://api.openai.com/v1"></div><div><label>模型名称</label><input id="model_name" placeholder="例如：gpt-4.1-mini"></div><div><label>API Key</label><input id="model_api_key" type="password" autocomplete="off" placeholder="只保存到本机 .env"></div></div></div>
<div class="section"><h2>连接飞书</h2><p>在飞书开放平台创建企业自建应用，并启用机器人能力。</p><div class="grid"><div><label>App ID</label><input id="feishu_app_id" placeholder="cli_..."></div><div><label>App Secret</label><input id="feishu_app_secret" type="password" autocomplete="off"></div><div><label>Verification Token</label><input id="feishu_verification_token" type="password" autocomplete="off"></div><div><label>Encrypt Key</label><input id="feishu_encrypt_key" type="password" autocomplete="off"></div><div class="full"><label>公网 HTTPS 地址</label><input id="public_base_url" placeholder="https://bot.example.com"><div class="hint">回调地址会自动生成为 /feishu/events。</div></div><div class="full"><label>扩展（可选）</label><input id="tool_modules" value="examples.tools" placeholder="工具模块，逗号分隔"><input id="hook_paths" style="margin-top:8px" placeholder="Hook 路径，逗号分隔"></div></div><label class="switch"><input id="require_mention" type="checkbox" checked>群聊仅在被 @ 时回复</label><label class="switch"><input id="accept_images" type="checkbox">接收图片消息（需要自行接入视觉 Tool）</label></div>
<div class="section"><h2>发布前检查</h2><p>同时检查配置、存储、扩展、模型和飞书凭据。全部通过后才保存。</p><button class="primary" id="checkBtn">开始检查</button><div class="checks" id="checks"></div><div class="error" id="error"></div><div class="success" id="success"></div><button class="primary" id="saveBtn" disabled>保存生产配置</button><div id="commands"></div></div>
<div class="actions"><button class="secondary" id="prev">上一步</button><button class="primary" id="next">下一步</button></div>
</section></div></main>
<script>
const TOKEN="__SETUP_TOKEN__";let index=0,checked=false;
const $=id=>document.getElementById(id);const sections=[...document.querySelectorAll('.section')],nav=[...document.querySelectorAll('.step')];
const prompts={general:'You are a helpful internal Feishu assistant. Answer clearly and concisely. Use tools only when needed and never invent tool results.',knowledge:'You are a Feishu knowledge assistant. Answer only from available context and tools. If evidence is missing, say so clearly.',workflow:'You are a Feishu workflow assistant. Confirm consequential actions, explain the next step, and never claim an action succeeded without a successful tool result.'};
$('system_prompt').value=prompts.general;$('template').onchange=e=>$('system_prompt').value=prompts[e.target.value];
function show(i){index=Math.max(0,Math.min(3,i));sections.forEach((x,n)=>x.classList.toggle('active',n===index));nav.forEach((x,n)=>x.classList.toggle('active',n===index));$('prev').disabled=index===0;$('next').style.display=index===3?'none':'block';$('clock').textContent=`步骤 ${index+1} / 4`;}
$('prev').onclick=()=>show(index-1);$('next').onclick=()=>show(index+1);nav.forEach((x,n)=>x.onclick=()=>show(n));show(0);
function payload(){return {bot_name:$('bot_name').value,purpose:$('purpose').value,system_prompt:$('system_prompt').value,model_base_url:$('model_base_url').value,model_name:$('model_name').value,model_api_key:$('model_api_key').value,feishu_app_id:$('feishu_app_id').value,feishu_app_secret:$('feishu_app_secret').value,feishu_verification_token:$('feishu_verification_token').value,feishu_encrypt_key:$('feishu_encrypt_key').value,public_base_url:$('public_base_url').value,tool_modules:$('tool_modules').value,hook_paths:$('hook_paths').value,require_mention:$('require_mention').checked,accept_images:$('accept_images').checked}}
async function post(path){const r=await fetch(path,{method:'POST',headers:{'content-type':'application/json','x-kitty-setup-token':TOKEN},body:JSON.stringify(payload())});const b=await r.json();if(!r.ok||!b.ok)throw new Error(b.error||'请求失败');return b}
$('checkBtn').onclick=async()=>{checked=false;$('saveBtn').disabled=true;$('checks').innerHTML='<div class="check">正在检查…</div>';$('error').style.display='none';try{const b=await post('/api/check');$('checks').innerHTML=b.checks.map(c=>`<div class="check"><span>${c.ok?'✓':'✕'} ${c.label}</span><span class="${c.ok?'ok':'bad'}">${escapeHtml(c.detail)} · ${c.duration_ms}ms</span></div>`).join('');checked=b.ready;$('saveBtn').disabled=!checked}catch(e){fail(e)}};
$('saveBtn').onclick=async()=>{if(!checked)return;try{const b=await post('/api/save');$('success').style.display='block';$('success').textContent='配置已安全保存：'+b.path;$('commands').innerHTML=`<p><b>飞书回调地址</b></p><pre>${escapeHtml(b.callback_url)}</pre><p><b>最后两条命令</b></p><pre>${escapeHtml(b.doctor_command)}\n${escapeHtml(b.serve_command)}</pre>`}catch(e){fail(e)}};
function fail(e){$('error').style.display='block';$('error').textContent=e.message;$('checks').innerHTML=''}function escapeHtml(v){const d=document.createElement('div');d.textContent=v;return d.innerHTML}
</script></body></html>"""
