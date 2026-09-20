"""API 抽象基类与统一结果对象。

设计目标：
- 屏蔽不同服务商差异，返回统一的 APIResult（成功/失败、数据、错误信息）。
- 统一处理超时、重试、日志。
- 所有耗时调用均可被上层（工作流 / UI）异步执行。
"""
from __future__ import annotations

import json
import logging
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional

import httpx

logger = logging.getLogger("PixelFoundry.api")


class APIError(Exception):
    """API 调用失败（网络、超时、HTTP 错误、解析错误等）。"""


@dataclass
class APIResult:
    """统一 API 调用结果。"""

    ok: bool
    data: Any = None
    error: Optional[str] = None
    raw: Any = None

    @property
    def message(self) -> str:
        if self.ok:
            return "成功"
        return self.error or "调用失败"


def _config_get(config: Any, key: str, default: Any = None) -> Any:
    """兼容 APIConfig 对象与 dict 的取值。"""
    if isinstance(config, dict):
        return config.get(key, default)
    return getattr(config, key, default)


class BaseAPI(ABC):
    """API 客户端抽象基类。

    子类需实现 KIND、call() 与 test_connection()。
    config 可以是 APIConfig（config.api_config 中定义）或普通 dict。
    """

    KIND = "base"

    def __init__(self, config: Any, transport: Optional[httpx.BaseTransport] = None):
        self.config = config
        self.base_url = str(_config_get(config, "base_url", "") or "").rstrip("/")
        self.api_key = str(_config_get(config, "api_key", "") or "")
        self.model = str(_config_get(config, "model", "") or "")
        self.params = dict(_config_get(config, "params", None) or {})
        self.timeout = float(self.params.get("timeout", 120))
        self.max_retries = int(self.params.get("max_retries", 2))
        self._proxy = str(self.params.get("proxy", "") or "").strip() or None
        verify = self.params.get("verify_ssl", True)
        self._verify_ssl = verify not in (False, 0, "0", "false", "False", "")
        self._transport = transport
        self._client: Optional[httpx.Client] = None
        # 最近一次失败请求的响应元信息 {status_code, text, url, method}（供探测/诊断展示）
        self.last_error_response: Optional[dict] = None

    # ------------------------------------------------------------------ #
    # HTTP 基础设施
    # ------------------------------------------------------------------ #
    def _resolve_url(self, default_path: str) -> str:
        """解析请求 URL。

        优先使用 params.url（完整 URL 覆盖），其次 base_url + params.endpoint
        （自定义路径，如不同服务商的 /api/v3/images/generations），
        最后 base_url + default_path。
        """
        full = self.params.get("url") or self.params.get("endpoint_url")
        if full:
            return str(full).rstrip("/")
        path = self.params.get("endpoint") or default_path
        return f"{self.base_url}{path}"

    # ------------------------------------------------------------------ #
    # 完全自定义请求：JSON 请求体模板 + 占位符
    # ------------------------------------------------------------------ #
    @staticmethod
    def render_template_payload(template: str, values: dict) -> Optional[dict]:
        """按占位符渲染 JSON 请求体模板。

        template 为合法 JSON 文本，占位符使用 $ 前缀（避免与 JSON 花括号冲突），
        例如 {"model": "$model", "messages": [{"role": "user", "content": "$prompt"}]}。
        values 为 {占位符名: 值}；值按 str() 替换（None/False -> "None"/"False"，因此
        布尔等需先转成字符串如 "true"）。模板非法 JSON 时返回 None。

        占位符按「标识符边界」匹配：$image 不会误伤 $image_url / $image_raw
        （它们更长，必须整体替换）；长名优先替换，避免被短名抢先吃掉。
        """
        text = str(template)
        for key in sorted((k for k in values if k), key=len, reverse=True):
            value = values[key]
            if value is None:
                continue
            if isinstance(value, bool):
                replacement = "true" if value else "false"
            else:
                replacement = str(value)
            text = re.sub(r"\$" + re.escape(str(key)) + r"(?![0-9A-Za-z_])", lambda _m, r=replacement: r, text)
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            return None
        return data if isinstance(data, dict) else None

    def custom_enabled(self) -> bool:
        """是否开启完全自定义请求（params.custom_request 或 provider=custom）。"""
        flag = self.params.get("custom_request")
        if flag in (True, 1, "1", "true", "True", "yes", "on"):
            return True
        return str(self.params.get("provider") or "").lower() == "custom"

    def custom_method(self) -> str:
        return str(self.params.get("request_method") or "POST").upper()

    # ------------------------------------------------------------------ #
    # 鉴权方式（中转站/聚合站的鉴权差异很大，统一在这里适配）
    # ------------------------------------------------------------------ #
    def auth_style(self) -> str:
        """当前鉴权方式（params.auth_style）。

        取值：bearer（默认，Authorization: Bearer <key>）/ x-api-key / api-key /
        query（追加到 URL 查询参数）/ custom（自定义请求头名与前缀）/ none（不鉴权）。
        未配置时返回 bearer，保持旧配置行为完全不变。
        """
        return str(self.params.get("auth_style") or "bearer").strip().lower()

    def _auth_header_pair(self) -> Optional[tuple]:
        """按鉴权方式返回 (头名, 头值)；无需请求头时返回 None。"""
        if not self.api_key:
            return None
        style = self.auth_style()
        if style == "bearer":
            return ("Authorization", f"Bearer {self.api_key}")
        if style == "x-api-key":
            return ("X-API-Key", self.api_key)
        if style == "api-key":
            return ("api-key", self.api_key)
        if style == "custom":
            name = str(self.params.get("auth_header") or "").strip() or "Authorization"
            prefix = str(self.params.get("auth_prefix") or "")
            return (name, f"{prefix}{self.api_key}")
        # query / none：不产生请求头（query 由 auth_url 拼到 URL 上）
        return None

    def auth_url(self, url: str) -> str:
        """按鉴权方式加工请求 URL。

        只有 auth_style=query 时才把 Key 作为查询参数追加（参数名取
        params.auth_query_param，默认 key），其余方式原样返回。
        所有请求路径（含视频提交/轮询、图片、文本、模型列表）都必须经过本方法。
        """
        if not self.api_key or self.auth_style() != "query":
            return url
        name = str(self.params.get("auth_query_param") or "").strip() or "key"
        try:
            from urllib.parse import quote, urlsplit, urlunsplit

            parts = urlsplit(url)
            query = parts.query
            piece = f"{quote(name, safe='')}={quote(self.api_key, safe='')}"
            query = f"{query}&{piece}" if query else piece
            return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))
        except Exception:  # noqa: BLE001  理论上不会发生，兜底不阻断请求
            joiner = "&" if "?" in url else "?"
            return f"{url}{joiner}{name}={self.api_key}"

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        pair = self._auth_header_pair()
        if pair is not None:
            headers[pair[0]] = pair[1]
        # 完全自定义：额外请求头（JSON，如 {"X-API-Key": "…", "Authorization": "…"}）
        # 额外请求头最后写入，可覆盖鉴权方式生成的任何头（含 Authorization）。
        extra = self.params.get("extra_headers")
        if extra:
            if isinstance(extra, str):
                try:
                    extra = json.loads(extra)
                except (ValueError, TypeError):
                    extra = {}
            if isinstance(extra, dict):
                for k, v in extra.items():
                    if isinstance(k, str) and isinstance(v, str) and k.strip():
                        headers[k.strip()] = v
        return headers

    def _http(self) -> httpx.Client:
        if self._client is None:
            kwargs: dict = {"timeout": self.timeout, "follow_redirects": True}
            if self._transport is not None:
                # 测试注入的 MockTransport 优先
                kwargs["transport"] = self._transport
            else:
                if self._proxy:
                    kwargs["proxy"] = self._proxy
                if not self._verify_ssl:
                    kwargs["verify"] = False
            self._client = httpx.Client(**kwargs)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    def __enter__(self) -> "BaseAPI":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # 请求与重试
    # ------------------------------------------------------------------ #
    def _request(self, method: str, url: str, multipart: bool = False, **kwargs) -> httpx.Response:
        """带重试的 HTTP 请求；失败抛 APIError。

        URL 会先经过 auth_url() 加工（鉴权方式为 query 时追加 Key 查询参数），
        因此调用方无需关心鉴权方式；错误信息里带加工后的完整 URL 便于排查。

        multipart=True 时发送 multipart/form-data（用于图片文件上传类服务商），
        此时不设置 Content-Type（由 httpx 自动生成带 boundary 的头）。
        """
        url = self.auth_url(url)
        retries = max(0, self.max_retries)
        backoff = [0.5, 1.5, 3.0]
        last_exc: Optional[Exception] = None
        self.last_error_response = None
        headers = self._headers()
        if multipart:
            headers = {k: v for k, v in headers.items() if k.lower() != "content-type"}
        for attempt in range(retries + 1):
            try:
                resp = self._http().request(method, url, headers=headers, **kwargs)
                resp.raise_for_status()
                return resp
            except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError) as exc:
                last_exc = exc
                logger.warning("API 请求网络异常(%s %s): %s", method, url, exc)
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                body = exc.response.text[:500]
                # 保留原始响应（诊断/探测用：4xx 直接抛出前也要留档）
                self.last_error_response = {
                    "status_code": status,
                    "text": exc.response.text,
                    "url": url,
                    "method": method,
                }
                # 429/5xx 可重试，其余直接抛出（错误信息带完整 URL 便于排查）
                if status in (408, 429) or status >= 500:
                    last_exc = exc
                else:
                    raise APIError(
                        self._friendly_error(
                            f"HTTP {status} ({method} {url}): {body}", status=status
                        )
                    ) from exc
            if attempt < retries:
                wait = backoff[min(attempt, len(backoff) - 1)]
                logger.info("重试 %s %s（第 %d 次，等待 %.1fs）", method, url, attempt + 1, wait)
                time.sleep(wait)
        raise APIError(f"请求失败（已重试 {retries} 次）: {self._describe_error(last_exc)}") from last_exc

    def _describe_error(self, exc: Exception) -> str:
        """把最终异常转为可读信息（含响应体/排查建议）。"""
        if isinstance(exc, httpx.HTTPStatusError):
            body = exc.response.text[:500]
            return self._friendly_error(
                f"HTTP {exc.response.status_code}: {body}", status=exc.response.status_code
            )
        return self._friendly_error(exc)

    def _post_json(self, url: str, payload: dict) -> dict:
        resp = self._request("POST", url, json=payload)
        return self._parse_json(resp)

    def _put_json(self, url: str, payload: dict) -> dict:
        """PUT + JSON 请求体（部分中转站的提交接口用 PUT）。"""
        resp = self._request("PUT", url, json=payload)
        return self._parse_json(resp)

    def _post_multipart(self, url: str, data: dict, files: dict) -> dict:
        """multipart/form-data 上传（图片文件等）；data 为文本字段，files 为文件字段。"""
        resp = self._request("POST", url, multipart=True, data=data, files=files)
        return self._parse_json(resp)

    def _get_json(self, url: str, **kwargs) -> dict:
        resp = self._request("GET", url, **kwargs)
        return self._parse_json(resp)

    @staticmethod
    def _parse_json(resp: httpx.Response) -> dict:
        try:
            return resp.json()
        except ValueError as exc:
            raise APIError(f"响应不是合法 JSON: {resp.text[:200]}") from exc

    @staticmethod
    def _dig(obj: Any, path: str) -> Any:
        """按 'a.b.0.c' 形式的路径取值，取不到返回 None。"""
        cur = obj
        for part in path.split("."):
            if cur is None:
                return None
            if part.lstrip("-").isdigit():
                try:
                    cur = cur[int(part)]
                except (IndexError, KeyError, TypeError):
                    return None
            elif isinstance(cur, dict):
                cur = cur.get(part)
            elif isinstance(cur, list):
                # 允许直接按字段名在列表里找
                found = None
                for item in cur:
                    if isinstance(item, dict) and part in item:
                        found = item[part]
                        break
                cur = found
            else:
                return None
        return cur

    # ------------------------------------------------------------------ #
    # 抽象接口
    # ------------------------------------------------------------------ #
    @abstractmethod
    def call(self, **kwargs) -> APIResult:
        """执行一次业务调用，返回统一结果。"""

    @abstractmethod
    def test_connection(self) -> APIResult:
        """连通性测试。"""

    def list_models(self) -> APIResult:
        """查询服务商当前令牌可用的模型列表（OpenAI 兼容 GET /models）。

        自动兼容两种 Base URL 写法：
        - https://api.gpt.ge        -> https://api.gpt.ge/v1/models
        - https://api.openai.com/v1 -> https://api.openai.com/v1/models
        """
        error = self._validate_config()
        if error:
            return APIResult(ok=False, error=error)
        base = self.base_url.rstrip("/")
        if base.endswith("/v1"):
            candidates = [f"{base}/models"]
        else:
            candidates = [f"{base}/v1/models", f"{base}/models"]
        last_err = "无法查询模型列表"
        for url in candidates:
            try:
                data = self._get_json(url)
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)
                continue
            items = data.get("data") if isinstance(data, dict) else None
            ids = [str(m["id"]) for m in (items or []) if isinstance(m, dict) and m.get("id")]
            if ids:
                return APIResult(ok=True, data=ids, raw=data)
            return APIResult(ok=False, error=f"响应中没有模型列表: {str(data)[:200]}", raw=data)
        return APIResult(ok=False, error=last_err)

    def _friendly_error(self, exc: Exception, status: Optional[int] = None) -> str:
        """把底层异常转为带排查建议的提示。

        exc 可以是异常对象，也可以是已经拼好的错误文本（带 HTTP 状态码时
        请用 status 传进来，便于追加 401/403 的鉴权排查建议）。

        404 + "Invalid URL" 通常有两种原因：
        - 多数 OpenAI 兼容服务：Base URL 缺少路径前缀（如 /v1），
          例如应填 https://api.gpt.ge/v1 而不是 https://api.gpt.ge；
        - gpt.ge 视频（豆包 Seedance）：不走 /videos/generations 通用路径，
          加 /v1 也无济于事——应使用「gpt.ge (V-API) 豆包视频」适配
          （端点 /task/volces/seedance）。
        - 视频接口的**端点路径本身**在中转站上不存在：厂商专有路径（如可灵
          Kling 的 /v1/videos/image2video）只在官方直连有效，中转站通常换成
          OpenAI 风格 /v1/videos/generations 或方舟风格 contents/generations/tasks
          ——此时用「一键适配端点…」自动探测，或「从 curl 导入…」贴示例请求。
        SSL 握手失败通常是网络被拦截或需要代理。
        401/403 多为鉴权方式不匹配（中转站常见 X-API-Key / api-key / 查询参数）。
        """
        msg = str(exc)
        if status is None:
            match = re.match(r"\s*(?:HTTP\s*)?(4\d\d|5\d\d)\b", msg)
            if match:
                status = int(match.group(1))
        if status is None and "401" in msg[:80]:
            status = 401
        if status is None and "403" in msg[:80]:
            status = 403
        if status in (401, 403) and "鉴权方式" not in msg:
            msg += (
                "（提示：401/403 多为鉴权方式不匹配——中转站/聚合站常用 X-API-Key、"
                "api-key 或 URL 查询参数，而非 Authorization: Bearer。"
                "请在高级项「鉴权方式」中改选对应方式，或选「自定义请求头」填正确的"
                "头名/前缀；也可用「额外请求头」直接覆盖，例如 {\"X-API-Key\": \"你的Key\"}）"
            )
        generic_hint_added = False
        if "Invalid URL" in msg and "/v1" not in msg:
            if self.KIND == "video" and "gpt.ge" in self.base_url:
                msg += (
                    "（提示：gpt.ge 视频不走通用 /videos/generations 路径——"
                    "请把「服务商适配」选为 gpt.ge (V-API) 豆包视频，自动使用 "
                    "/task/volces/seedance；或在高级项「提交端点/轮询端点」手动填写正确路径）"
                )
            else:
                msg += "（提示：多为 Base URL 缺少 /v1 等路径前缀所致，请核对服务商要求的完整路径，如 https://api.gpt.ge/v1）"
            generic_hint_added = True
        # 视频 404（含端点自带 /v1 的情况，如中转站上的可灵路径）：指向「一键适配端点…」。
        # 用 "一键适配端点" not in msg 兜底幂等——_request 与 call 都会走一遍本方法。
        if (
            self.KIND == "video"
            and not generic_hint_added
            and "一键适配端点" not in msg
            and ("Invalid URL" in msg or status == 404)
        ):
            msg += (
                "（提示：视频的「提交端点」在该服务上可能根本不存在——厂商专有路径"
                "（如可灵 Kling 的 /v1/videos/image2video）只在官方直连有效，"
                "经中转站/聚合站转发时路径通常是另一套（如 OpenAI 风格 "
                "/v1/videos/generations、方舟风格 /contents/generations/tasks）。"
                "请点「一键适配端点…」自动探测该站真实可用的端点并一键写入配置；"
                "或用「从 curl 导入…」粘贴服务商文档/浏览器里的示例请求）"
            )
        if ("SSL" in msg or "TLS" in msg or "EOF" in msg) and "代理" not in msg:
            msg += "（提示：SSL/TLS 握手失败通常是网络被拦截或直连不通。可在 API 配置的高级项「代理」中填写代理地址，如 http://127.0.0.1:7890；或更换网络后重试）"
        return msg

    def _validate_config(self) -> Optional[str]:
        if not self.base_url:
            return "未配置 Base URL"
        if not self.base_url.startswith(("http://", "https://")):
            return f"Base URL 必须以 http:// 或 https:// 开头（当前: {self.base_url}）"
        if not self.api_key:
            return "未配置 API Key"
        return None
