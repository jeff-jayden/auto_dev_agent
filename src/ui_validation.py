"""UI 设计验收服务。

读取 Figma MCP 设计上下文并对浏览器截图执行视觉检查，为任务和 MR 生成 UI 验收证据。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen
from uuid import uuid4

from domain.models import (
    DesignReference,
    Task,
    UIAcceptanceCheck,
    UIAcceptanceReport,
)


class FigmaMCPClient:
    """Small Streamable HTTP MCP client for Figma design read tools."""

    def __init__(self, endpoint: str, artifact_root: Path, timeout: int = 60):
        """初始化 Figma MCP 客户端。

        Args:
            endpoint: Figma MCP 服务的 HTTP 地址；为空时表示未启用。
            artifact_root: 设计快照和相关验收产物的保存目录。
            timeout: 单次 MCP 请求的超时时间，单位为秒。
        """
        self.endpoint = endpoint.strip()
        self.artifact_root = artifact_root
        self.timeout = timeout
        self._session_id: str | None = None
        self._request_id = 0

    @property
    def enabled(self) -> bool:
        return bool(self.endpoint)

    def capture(
        self,
        task_id: str,
        figma_url: str,
        preview_url: str | None,
        viewport_width: int,
        viewport_height: int,
    ) -> DesignReference:
        if not self.enabled:
            raise ValueError("Figma MCP 尚未配置，请设置 FIGMA_MCP_URL")
        file_key, node_id = self.parse_url(figma_url)
        self._initialize()
        arguments = {
            "fileKey": file_key,
            "nodeId": node_id,
            "clientLanguages": "javascript,typescript,css",
            "clientFrameworks": "react",
        }
        context_result = self._call_tool("get_design_context", arguments)
        variables_result = self._call_tool("get_variable_defs", arguments)
        screenshot_result = self._call_tool(
            "get_screenshot", {"fileKey": file_key, "nodeId": node_id}
        )
        context = self._text_content(context_result)
        variables = self._text_content(variables_result)
        screenshot = self._image_content(screenshot_result)
        target = self.artifact_root / task_id
        target.mkdir(parents=True, exist_ok=True)
        screenshot_path = None
        if screenshot:
            screenshot_path = str(target / "figma-reference.png")
            Path(screenshot_path).write_bytes(screenshot)
        digest = hashlib.sha256(
            context.encode("utf-8") + variables.encode("utf-8") + (screenshot or b"")
        ).hexdigest()
        return DesignReference(
            url=figma_url,
            file_key=file_key,
            node_id=node_id,
            viewport_width=viewport_width,
            viewport_height=viewport_height,
            preview_url=preview_url or None,
            context=context[:160_000],
            variables=variables[:40_000],
            screenshot_path=screenshot_path,
            snapshot_hash=digest,
        )

    @staticmethod
    def parse_url(figma_url: str) -> tuple[str, str]:
        parsed = urlparse(figma_url.strip())
        parts = [part for part in parsed.path.split("/") if part]
        if parsed.netloc not in {"figma.com", "www.figma.com"} or len(parts) < 2:
            raise ValueError("请输入包含文件和节点信息的 Figma 链接")
        try:
            marker = next(index for index, part in enumerate(parts) if part in {"design", "file"})
            file_key = parts[marker + 1]
        except (StopIteration, IndexError) as error:
            raise ValueError("无法从 Figma 链接中识别 file key") from error
        node_id = parse_qs(parsed.query).get("node-id", [""])[0]
        node_id = unquote(node_id).replace("-", ":")
        if not node_id:
            raise ValueError("Figma 链接必须包含 node-id，请复制具体 Frame 的链接")
        return file_key, node_id

    def _initialize(self) -> None:
        if self._session_id:
            return
        result, headers = self._post({
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "ai-dev-agent", "version": "0.1.0"},
            },
        })
        if "error" in result:
            message = result["error"].get("message", "unknown error")
            raise ValueError(f"Figma MCP 初始化失败：{message}")
        self._session_id = headers.get("Mcp-Session-Id")
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        payload, _ = self._post({
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        })
        if "error" in payload:
            message = payload["error"].get("message", "unknown error")
            raise ValueError(f"Figma MCP {name} 调用失败：{message}")
        result = payload.get("result", {})
        if result.get("isError"):
            raise ValueError(f"Figma MCP {name} 调用失败：{self._text_content(result)}")
        return result

    def _post(self, payload: dict[str, Any]) -> tuple[dict[str, Any], Any]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        request = Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
                response_headers = response.headers
        except OSError as error:
            raise ValueError(f"无法连接 Figma MCP：{error}") from error
        if not body.strip():
            return {}, response_headers
        if body.lstrip().startswith("{"):
            return json.loads(body), response_headers
        messages = [
            json.loads(line[5:].strip())
            for line in body.splitlines()
            if line.startswith("data:") and line[5:].strip().startswith("{")
        ]
        if not messages:
            raise ValueError("Figma MCP 返回了无法识别的响应")
        return messages[-1], response_headers

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    @staticmethod
    def _text_content(result: dict[str, Any]) -> str:
        return "\n".join(
            str(item.get("text", ""))
            for item in result.get("content", [])
            if item.get("type") == "text"
        ).strip()

    @staticmethod
    def _image_content(result: dict[str, Any]) -> bytes | None:
        for item in result.get("content", []):
            if item.get("type") == "image" and item.get("data"):
                return base64.b64decode(item["data"])
        return None


class UIAcceptanceService:
    def __init__(self, artifact_root: Path):
        """初始化 UI 验收服务。

        Args:
            artifact_root: 实现截图、视觉差异等 UI 验收产物的保存目录。
        """
        self.artifact_root = artifact_root

    def validate(self, task: Task) -> UIAcceptanceReport | None:
        design = task.design_reference
        if design is None:
            return None
        checks = [UIAcceptanceCheck(
            category="design",
            name="Figma 设计基线",
            status="passed",
            detail=f"已固定节点 {design.node_id}，快照 {design.snapshot_hash[:12]}",
        )]
        if not design.preview_url:
            checks.append(UIAcceptanceCheck(
                category="browser",
                name="实现页面",
                status="skipped",
                detail="未配置预览 URL，已保留设计基线但未执行浏览器验收",
            ))
            return UIAcceptanceReport(
                status="not_run",
                summary="设计基线已保存，等待配置实现页面地址",
                checks=checks,
                design_snapshot_hash=design.snapshot_hash,
            )

        browser = self._find_browser()
        if browser is None:
            checks.append(UIAcceptanceCheck(
                category="browser",
                name="浏览器渲染",
                status="failed",
                detail="未找到 Chrome 或 Edge，无法渲染实现页面",
                blocking=True,
            ))
            return self._report(design, checks)

        target = self.artifact_root / task.id / uuid4().hex[:8]
        target.mkdir(parents=True, exist_ok=True)
        screenshot = target / "implementation.png"
        try:
            dom = self._render(browser, design.preview_url, design, screenshot)
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            checks.append(UIAcceptanceCheck(
                category="browser",
                name="实现页面渲染",
                status="failed",
                detail=str(error),
                blocking=True,
            ))
            return self._report(design, checks)

        checks.append(UIAcceptanceCheck(
            category="browser",
            name="实现页面渲染",
            status="passed",
            detail=f"已按 {design.viewport_width}×{design.viewport_height} 渲染并保存截图",
        ))
        expected_texts = self._expected_texts(design.context)
        matched = [text for text in expected_texts if text in dom]
        if expected_texts:
            coverage = len(matched) / len(expected_texts)
            checks.append(UIAcceptanceCheck(
                category="structure",
                name="关键文案与结构",
                status="passed" if coverage >= 0.6 else "warning",
                detail=f"浏览器页面命中 {len(matched)}/{len(expected_texts)} 个设计稿关键文本",
            ))

        score = None
        diff_path = None
        if design.screenshot_path and Path(design.screenshot_path).is_file():
            score, diff_path = self._compare_images(
                Path(design.screenshot_path), screenshot, target / "visual-diff.png"
            )
            checks.append(UIAcceptanceCheck(
                category="visual",
                name="截图视觉一致性",
                status="passed" if score >= 85 else "warning",
                detail=f"参考图与实现截图相似度 {score:.1f}%（视觉偏差仅提示，不单独阻塞发布）",
            ))
        else:
            checks.append(UIAcceptanceCheck(
                category="visual",
                name="截图视觉一致性",
                status="skipped",
                detail="Figma MCP 未返回参考截图，无法执行像素对比",
            ))
        return self._report(design, checks, score, screenshot, diff_path)

    def _report(
        self,
        design: DesignReference,
        checks: list[UIAcceptanceCheck],
        score: float | None = None,
        screenshot: Path | None = None,
        diff_path: Path | None = None,
    ) -> UIAcceptanceReport:
        blocking = any(item.blocking and item.status == "failed" for item in checks)
        warning = any(item.status in {"warning", "skipped"} for item in checks)
        status = "failed" if blocking else ("warning" if warning else "passed")
        return UIAcceptanceReport(
            status=status,
            summary=(
                "UI 验收存在阻塞问题" if blocking
                else "UI 验收通过" if status == "passed"
                else "UI 验收完成，但仍有非阻塞提示"
            ),
            similarity_score=score,
            checks=checks,
            implementation_screenshot_path=str(screenshot) if screenshot else None,
            diff_screenshot_path=str(diff_path) if diff_path else None,
            design_snapshot_hash=design.snapshot_hash,
        )

    @staticmethod
    def _find_browser() -> Path | None:
        candidates = [
            Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Microsoft/Edge/Application/msedge.exe",
        ]
        return next((path for path in candidates if path.is_file()), None)

    @staticmethod
    def _render(
        browser: Path,
        url: str,
        design: DesignReference,
        screenshot: Path,
    ) -> str:
        screenshot = screenshot.resolve()
        try:
            with urlopen(url, timeout=15) as response:
                dom = response.read().decode("utf-8", errors="replace")
        except OSError as error:
            raise ValueError(f"预览页面无法访问：{error}") from error
        if "<html" not in dom.lower():
            raise ValueError("预览地址没有返回 HTML 页面")
        command = [
            str(browser),
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--no-first-run",
            "--incognito",
            "--hide-scrollbars",
            f"--window-size={design.viewport_width},{design.viewport_height}",
            "--virtual-time-budget=5000",
            f"--screenshot={screenshot}",
            url,
        ]
        shot_result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=45,
            check=False,
        )
        if shot_result.returncode != 0 or not screenshot.is_file():
            raise ValueError("浏览器截图失败：" + shot_result.stderr.strip()[-500:])
        return dom

    @staticmethod
    def _expected_texts(context: str) -> list[str]:
        candidates = re.findall(r">([^<>\n]{2,80})<|[\"']([^\"'\n]{2,80})[\"']", context)
        ignored = (
            "className", "display", "flex", "width", "height",
            "background", "padding",
        )
        result: list[str] = []
        for pair in candidates:
            text = next((value.strip() for value in pair if value.strip()), "")
            if (
                not text
                or any(token in text for token in ignored)
                or text.startswith(("http", "./", "../"))
            ):
                continue
            if text not in result:
                result.append(text)
        return result[:20]

    @staticmethod
    def _compare_images(
        reference: Path, actual: Path, diff_path: Path
    ) -> tuple[float, Path]:
        try:
            from PIL import Image, ImageChops, ImageStat
        except ImportError as error:
            raise ValueError("需要安装 Pillow 才能执行截图视觉对比") from error
        with Image.open(reference) as ref_image, Image.open(actual) as actual_image:
            ref = ref_image.convert("RGB")
            rendered = actual_image.convert("RGB").resize(ref.size)
            difference = ImageChops.difference(ref, rendered)
            rms = ImageStat.Stat(difference).rms
            normalized = sum(rms) / (len(rms) * 255)
            score = max(0.0, (1.0 - normalized) * 100)
            difference.save(diff_path)
        return round(score, 1), diff_path
