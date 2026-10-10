"""大模型访问网关。

统一 OpenAI Compatible、DeepSeek 和 Ollama 的结构化生成接口，并根据环境配置构建模型客户端。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Protocol

from pydantic import BaseModel

from .context_budget import ContextBudget, ContextBudgetExceeded, fit_messages


class ModelGateway(Protocol):
    @property
    def enabled(self) -> bool: ...

    def generate_structured(self, system_prompt: str, user_prompt: str, output_model: type[BaseModel]) -> BaseModel | None: ...


class DisabledModelGateway:
    enabled = False
    supports_context_tool_loop = False

    def generate_structured(self, system_prompt: str, user_prompt: str, output_model: type[BaseModel]) -> BaseModel | None:
        return None


def _json_retry_messages(system_prompt: str, user_prompt: str, error: ValueError,
                         output_model: type[BaseModel], *, native_schema: bool = False) -> list[dict]:
    """从原始输入重新生成 JSON，不累计失败回复，也不把源码夹进校验错误。"""
    if hasattr(error, "errors"):
        errors = error.errors(include_input=False, include_url=False)
        detail = json.dumps([
            {"loc": list(item["loc"])[:12], "type": item["type"], "msg": item["msg"][:200]}
            for item in errors[:8]
        ], ensure_ascii=False)[:1800]
    else:
        detail = str(error)[:1800]
    correction = "上次输出未通过 JSON 校验。基于原始需求和源码重新生成完整 JSON，不要解释，不要改变任务语义。\n校验错误：" + detail
    try:
        original = json.loads(user_prompt)
    except (ValueError, TypeError):
        original = {}
    if not native_schema and not (isinstance(original, dict) and "output_schema" in original):
        correction += "\nJSON Schema：" + json.dumps(output_model.model_json_schema(), ensure_ascii=False)
    return [{"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
            {"role": "user", "content": correction}]


class OpenAICompatibleGateway:
    enabled = True
    supports_context_tool_loop = True

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout: int = 90,
        extra_payload: dict | None = None,
    ):
        """初始化 OpenAI 兼容的模型网关。

        Args:
            base_url: 模型服务的 OpenAI 兼容 API 根地址。
            model: 每次请求使用的模型名称。
            api_key: 模型服务鉴权密钥，仅保存在当前进程内存中。
            timeout: 单次模型请求的超时时间，单位为秒。
            extra_payload: 合并到请求体中的供应商扩展参数。
        """
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.extra_payload = extra_payload or {}
        self.last_error = ""
        self.last_response_content = ""
        self.last_usage: dict[str, int] = {}
        self.last_context_budget: dict = {}

    def generate_structured(self, system_prompt: str, user_prompt: str, output_model: type[BaseModel]) -> BaseModel | None:
        messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
        ]
        for attempt in range(3):
            try:
                messages, self.last_context_budget = fit_messages(
                    messages, extra={"schema": output_model.model_json_schema()},
                )
            except ContextBudgetExceeded as error:
                self.last_error = str(error)
                return None
            content = ""
            payload = {
                "model": self.model,
                "messages": messages,
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
                **self.extra_payload,
                "max_tokens": min(int(self.extra_payload.get("max_tokens", ContextBudget.from_environment().output)), ContextBudget.from_environment().output),
            }
            request = urllib.request.Request(
                f"{self.base_url}/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}),
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = json.loads(response.read().decode("utf-8"))
                content = body["choices"][0]["message"]["content"]
                usage = body.get("usage", {})
                self.last_usage = {
                    "prompt_tokens": int(usage.get("prompt_tokens", 0)),
                    "completion_tokens": int(usage.get("completion_tokens", 0)),
                }
                self.last_response_content = content
                result = output_model.model_validate_json(content)
                self.last_error = ""
                return result
            except (OSError, KeyError, urllib.error.URLError) as error:
                self.last_error = str(error)
                if attempt == 2:
                    return None
                continue
            except ValueError as error:
                self.last_error = str(error)
                if attempt == 2:
                    return None
                messages = _json_retry_messages(system_prompt, user_prompt, error, output_model)
        return None


class DeepSeekGateway(OpenAICompatibleGateway):
    """DeepSeek gateway that can also supply a LangChain chat model."""

    supports_langchain_agent = True

    def build_langchain_model(self):
        from langchain_deepseek import ChatDeepSeek

        return ChatDeepSeek(
            model=self.model,
            api_key=self.api_key,
            base_url=self.base_url,
            temperature=0.1,
            timeout=self.timeout,
            max_retries=2,
            extra_body=self.extra_payload or None,
        )


class OllamaGateway(OpenAICompatibleGateway):
    """Ollama native API with thinking disabled and schema-constrained JSON output."""

    def __init__(self, base_url: str, model: str, api_key: str = "", timeout: int = 240):
        """初始化 Ollama 原生协议模型网关。

        Args:
            base_url: Ollama 服务根地址；传入的末尾 ``/v1`` 会被自动移除。
            model: Ollama 中已安装的模型名称。
            api_key: 可选鉴权密钥，供受保护的兼容服务使用。
            timeout: 单次模型请求的超时时间，单位为秒。
        """
        native_base = base_url.rstrip("/")
        if native_base.endswith("/v1"):
            native_base = native_base[:-3]
        super().__init__(native_base, model, api_key, timeout)

    def generate_structured(self, system_prompt: str, user_prompt: str, output_model: type[BaseModel]) -> BaseModel | None:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        for attempt in range(3):
            try:
                messages, self.last_context_budget = fit_messages(
                    messages, extra={"schema": output_model.model_json_schema()},
                )
            except ContextBudgetExceeded as error:
                self.last_error = str(error)
                return None
            payload = {
                "model": self.model,
                "messages": messages,
                "stream": False,
                "think": False,
                "format": output_model.model_json_schema(),
                "options": {"temperature": 0.1, "num_predict": ContextBudget.from_environment().output,
                            "num_ctx": ContextBudget.from_environment().window},
            }
            request = urllib.request.Request(
                f"{self.base_url}/api/chat",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            content = ""
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = json.loads(response.read().decode("utf-8"))
                content = body["message"]["content"]
                self.last_usage = {
                    "prompt_tokens": int(body.get("prompt_eval_count", 0)),
                    "completion_tokens": int(body.get("eval_count", 0)),
                }
                self.last_response_content = content
                result = output_model.model_validate_json(content)
                self.last_error = ""
                return result
            except (OSError, KeyError, urllib.error.URLError) as error:
                self.last_error = str(error)
                if attempt == 2:
                    return None
            except ValueError as error:
                self.last_error = str(error)
                if attempt == 2:
                    return None
                messages = _json_retry_messages(system_prompt, user_prompt, error, output_model, native_schema=True)
        return None


def build_model_gateway() -> ModelGateway:
    provider = os.getenv("MODEL_PROVIDER", "disabled").lower()
    if provider == "ollama":
        return OllamaGateway(
            os.getenv("MODEL_BASE_URL", "http://127.0.0.1:11434/v1"),
            os.getenv("MODEL_NAME", "qwen3-coder"),
            os.getenv("MODEL_API_KEY", "ollama"),
            timeout=int(os.getenv("MODEL_TIMEOUT_SECONDS", "240")),
        )
    if provider in {"openai", "openai_compatible"}:
        return OpenAICompatibleGateway(
            os.getenv("MODEL_BASE_URL", "https://api.openai.com/v1"),
            os.environ["MODEL_NAME"],
            os.getenv("MODEL_API_KEY", ""),
            timeout=int(os.getenv("MODEL_TIMEOUT_SECONDS", "90")),
        )
    if provider == "deepseek":
        return DeepSeekGateway(
            os.getenv("MODEL_BASE_URL", "https://api.deepseek.com"),
            os.getenv("MODEL_NAME", "deepseek-flash"),
            os.getenv("DEEPSEEK_API_KEY", os.getenv("MODEL_API_KEY", "")),
            timeout=int(os.getenv("MODEL_TIMEOUT_SECONDS", "180")),
            extra_payload={"thinking": {"type": "disabled"}},
        )
    return DisabledModelGateway()
