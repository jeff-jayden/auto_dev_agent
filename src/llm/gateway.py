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


class ModelGateway(Protocol):
    @property
    def enabled(self) -> bool: ...

    def generate_structured(self, system_prompt: str, user_prompt: str, output_model: type[BaseModel]) -> BaseModel | None: ...


class DisabledModelGateway:
    enabled = False
    supports_context_tool_loop = False

    def generate_structured(self, system_prompt: str, user_prompt: str, output_model: type[BaseModel]) -> BaseModel | None:
        return None


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
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.extra_payload = extra_payload or {}
        self.last_error = ""
        self.last_response_content = ""
        self.last_usage: dict[str, int] = {}

    def generate_structured(self, system_prompt: str, user_prompt: str, output_model: type[BaseModel]) -> BaseModel | None:
        messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
        ]
        for attempt in range(3):
            content = ""
            payload = {
                "model": self.model,
                "messages": messages,
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
                **self.extra_payload,
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
                if not any(message.get("role") == "assistant" for message in messages):
                    messages.extend([
                        {"role": "assistant", "content": content},
                        {
                            "role": "user",
                            "content": (
                                "上一个 JSON 未通过结构校验。只修正 JSON，不要解释，不要改变任务语义。"
                                f"\n校验错误：{error}"
                                f"\n必须符合的 JSON Schema：{json.dumps(output_model.model_json_schema(), ensure_ascii=False)}"
                            ),
                        },
                    ])
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
            payload = {
                "model": self.model,
                "messages": messages,
                "stream": False,
                "think": False,
                "format": output_model.model_json_schema(),
                "options": {"temperature": 0.1, "num_predict": 2048},
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
                messages.extend([
                    {"role": "assistant", "content": content},
                    {
                        "role": "user",
                        "content": "只修正 JSON 结构并严格符合既定 Schema，不要解释。",
                    },
                ])
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
