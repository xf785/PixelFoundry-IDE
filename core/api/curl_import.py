"""从浏览器开发者工具复制的 cURL 命令导入 API 配置。

## 为什么需要它

中转站/聚合站（relay / aggregator）的接口文档常常语焉不详，但**浏览器 F12 →
Network → 右键请求 → Copy as cURL** 能拿到一份「一定正确」的真实请求：
完整 URL、全部请求头、真实请求体。本模块把这份 cURL 解析成配置项，
用户不必手抄端点、请求头与 JSON 请求体。

## 解析流程

1. 容忍行尾续行符（``\\`` + 换行）、单/双引号、``--url`` 与裸 URL；
2. 提取 ``-X/--request`` 方法、``-H/--header`` 请求头、
   ``-d/--data/--data-raw/--data-binary/--json`` 请求体；
3. 请求体若是 JSON，则把**已知字段**替换成 ``$占位符``（见下），
   生成可直接填进「请求体模板(JSON)」的 ``body_template``；
4. ``to_params()`` 汇总成配置 params（provider=custom / submit_url / 方法 /
   额外请求头 / 请求体模板）。

## 字段映射（大小写不敏感，也支持嵌套一层）

| 请求体里的键 | 占位符 |
| --- | --- |
| prompt / text / description | ``$prompt`` |
| negative_prompt / negative | ``$negative_prompt`` |
| image / img / first_frame / first_frame_image / input_image | ``$image``（值是 http(s) 链接时改用 ``$image_url``） |
| last_frame / tail_image | ``$last_image`` |
| model / model_name | ``$model`` |
| duration | ``$duration`` |
| frames / frame_num / num_frames | ``$frames`` |
| fps / frame_rate | ``$fps`` |
| seed | ``$seed`` |
| ratio / aspect_ratio | ``$ratio`` |
| resolution / size | ``$resolution`` |
| mode | ``$mode`` |

## 已知限制（导入后仍需人工确认）

- **不做鉴权映射**：请求头里的 ``Authorization`` / ``X-API-Key`` 一律进「额外请求头」，
  不会写成 API Key 字段（Key 是明文，建议导入后把密钥填进「API Key」并删掉额外请求头）；
- 请求体不是 JSON（表单/纯文本/binary）时只保留 URL 与方法，不生成模板（会在 warnings 里说明）；
- 只能猜出**提交**端点；轮询端点、任务ID/状态/视频URL 字段路径仍需靠
  「测试并检测字段…」或人工填写；
- 嵌套超过一层的字段不会被替换（避免误伤业务数据），会留在模板里原样保留。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

# 请求方法白名单（curl 未显式给 -X 时：有请求体按 POST，否则 GET）
_HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")

# 请求体键名 -> 占位符（全部小写比较）
_KEY_MAP: Dict[str, str] = {
    "prompt": "$prompt",
    "text": "$prompt",
    "description": "$prompt",
    "negative_prompt": "$negative_prompt",
    "negative": "$negative_prompt",
    "image": "$image",
    "img": "$image",
    "first_frame": "$image",
    "first_frame_image": "$image",
    "input_image": "$image",
    "image_url": "$image",
    "last_frame": "$last_image",
    "tail_image": "$last_image",
    "model": "$model",
    "model_name": "$model",
    "duration": "$duration",
    "frames": "$frames",
    "frame_num": "$frames",
    "num_frames": "$frames",
    "fps": "$fps",
    "frame_rate": "$fps",
    "seed": "$seed",
    "ratio": "$ratio",
    "aspect_ratio": "$ratio",
    "resolution": "$resolution",
    "size": "$resolution",
    "mode": "$mode",
}

# 这些图片类键的值若是 http(s) 链接，说明是自备图床 URL（→ $image_url 而不是 base64）
_IMAGE_KEYS = {"image", "img", "first_frame", "first_frame_image", "input_image", "image_url"}

# 请求头里由客户端/Browser 自动生成的，导入时丢弃
_IGNORED_HEADERS = {
    "content-type",
    "accept",
    "accept-encoding",
    "accept-language",
    "user-agent",
    "content-length",
    "host",
    "connection",
    "origin",
    "referer",
    "priority",
    "sec-fetch-dest",
    "sec-fetch-mode",
    "sec-fetch-site",
    "sec-ch-ua",
    "sec-ch-ua-mobile",
    "sec-ch-ua-platform",
    "cookie",
    "pragma",
    "cache-control",
    "dnt",
    "te",
    "upgrade-insecure-requests",
}

# URL 路径里看起来像「任务查询」的片段（用于猜轮询端点）
_TASK_ID_SEGMENT = re.compile(r"^(\{[a-z_]+\}|<[a-z_]+>|:[a-z_]+|[0-9a-fA-F-]{6,}|\.\.\.|xxx)$", re.IGNORECASE)
_TASK_HINT_WORDS = ("task", "tasks", "job", "jobs", "prediction", "predictions", "status", "result", "resultats")


@dataclass
class CurlImport:
    """一条 cURL 命令解析出的 API 配置素材。"""

    method: str = "POST"
    url: str = ""
    base_url: str = ""                       # scheme + host，如 https://api.example.com
    path: str = ""                           # /v1/videos/generations
    headers: Dict[str, str] = field(default_factory=dict)
    body: Optional[dict] = None              # 解析出的 JSON 请求体（非 JSON 时为 None）
    body_template: str = ""                  # 带 $占位符的 JSON 文本（可直接填模板字段）
    placeholders: Dict[str, str] = field(default_factory=dict)   # JSON 路径 -> 占位符
    warnings: List[str] = field(default_factory=list)
    image_mode: str = "none"                 # data_uri / url / none
    raw_body: str = ""                       # 原始请求体文本（非 JSON 也有值）


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
def _strip_line_continuations(text: str) -> str:
    """去掉 shell 续行符（``\\`` + 换行）与孤立的续行反斜杠。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\\[ \t]*\n", " ", text)
    return re.sub(r"\^\s*\n", " ", text)     # 兼容 Windows cmd 的续行符


def _strip_outer_quotes(segment: str) -> str:
    """去掉整段命令最外层成对的引号（引号在段中间时不动）。"""
    if len(segment) >= 2 and segment[0] == segment[-1] and segment[0] in "\"'":
        inner = segment[1:-1]
        # 只有「引号中间不再出现同类未转义引号」时才安全去除
        if not re.search(r'(?<!\\)' + re.escape(segment[0]), inner):
            return inner
    return segment


def _tokenize(text: str) -> List[Tuple[str, bool]]:
    """按 shell 规则切分：支持单引号、双引号与 $'…'，返回 [(token, 是否带引号)]。"""
    tokens: List[Tuple[str, bool]] = []
    buf: List[str] = []
    quoted = False
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "$" and index + 1 < length and text[index + 1] == "'":
            # $'…'：内部允许 \n / \t / \' 等转义
            index += 2
            quoted = True
            while index < length and text[index] != "'":
                if text[index] == "\\" and index + 1 < length:
                    index += 1
                    buf.append({"n": "\n", "t": "\t", "r": "\r"}.get(text[index], text[index]))
                else:
                    buf.append(text[index])
                index += 1
            index += 1  # 跳过收尾引号
            continue
        if char in "\"'":
            quote = char
            index += 1
            quoted = True
            while index < length and text[index] != quote:
                if text[index] == "\\" and quote == '"' and index + 1 < length:
                    index += 1
                    buf.append(text[index])
                else:
                    buf.append(text[index])
                index += 1
            index += 1  # 跳过收尾引号
            continue
        if char.isspace():
            if buf:
                tokens.append(("".join(buf), quoted))
                buf = []
                quoted = False
            index += 1
            continue
        buf.append(char)
        index += 1
    if buf:
        tokens.append(("".join(buf), quoted))
    return tokens


def _split_option(token: str) -> Tuple[str, Optional[str]]:
    """把 ``--data-raw=xxx`` 拆成 (flag, value)；没有 = 时 value 为 None。"""
    if token.startswith("--") and "=" in token:
        flag, value = token.split("=", 1)
        return flag, value
    return token, None


def _extract_json_body(text: str) -> Optional[Any]:
    """尝试把请求体解析成 JSON；容忍前后多余的文本（取最外层 {} 或 []）。"""
    text = text.strip()
    if not text:
        return None
    for candidate in (text, _outermost_json(text)):
        if candidate is None:
            continue
        try:
            return json.loads(candidate)
        except (ValueError, TypeError):
            continue
    return None


def _outermost_json(text: str) -> Optional[str]:
    """截取第一个 { / [ 到与之配对的收尾符之间的片段。"""
    start = None
    quote = ""
    escaped = False
    for index, char in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in "\"'":
            if start is not None:
                quote = char
            continue
        if start is None and char in "{[":
            start = index
            opener = char
            closer = "}" if char == "{" else "]"
            depth = 0
            quote = ""
            escaped = False
            for sub in range(index, len(text)):
                ch = text[sub]
                if quote:
                    if escaped:
                        escaped = False
                    elif ch == "\\":
                        escaped = True
                    elif ch == quote:
                        quote = ""
                    continue
                if ch in "\"'":
                    quote = ch
                elif ch == opener:
                    depth += 1
                elif ch == closer:
                    depth -= 1
                    if depth == 0:
                        return text[index:sub + 1]
            return None
    return None


def _looks_like_url(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower().startswith(("http://", "https://"))


def _apply_placeholders(body: Any) -> Tuple[Any, Dict[str, str], str]:
    """把已知字段替换成占位符，返回 (模板对象, {路径: 占位符}, image_mode)。

    只处理顶层与嵌套一层（dict/list 内的 dict），避免误伤业务数据。
    """
    found: Dict[str, str] = {}
    state = {"image_mode": "none"}

    def walk(node: Any, path: str, depth: int) -> Any:
        if not isinstance(node, dict):
            return node
        out: Dict[str, Any] = {}
        for key, value in node.items():
            child_path = f"{path}.{key}" if path else str(key)
            if isinstance(value, dict) and depth < 1:
                out[key] = walk(value, child_path, depth + 1)
                continue
            if isinstance(value, list) and depth < 1:
                out[key] = [
                    walk(item, f"{child_path}.{i}", depth + 1) if isinstance(item, dict) else item
                    for i, item in enumerate(value)
                ]
                continue
            placeholder = _KEY_MAP.get(str(key).strip().lower())
            if placeholder and value is not None and not isinstance(value, (dict, list)):
                if placeholder == "$image" and _looks_like_url(value):
                    placeholder = "$image_url"
                found[child_path] = placeholder
                if placeholder == "$image_url":
                    state["image_mode"] = "url"
                elif placeholder in ("$image", "$last_image") and state["image_mode"] == "none":
                    state["image_mode"] = "data_uri"
                out[key] = placeholder
                continue
            out[key] = value
        return out

    template = walk(body, "", 0)
    return template, found, state["image_mode"]


def _looks_like_task_url(url: str) -> bool:
    """URL 是否像「按任务 ID 查询」的轮询端点。"""
    path = urlsplit(url).path.rstrip("/")
    segments = [s for s in path.split("/") if s]
    if not segments:
        return False
    if _TASK_ID_SEGMENT.match(segments[-1]):
        return True
    return any(word in segments[-1].lower() for word in _TASK_HINT_WORDS)


def parse_curl(text: str) -> CurlImport:
    """解析一条 curl 命令（可多行、可含引号）为 CurlImport。"""
    imp = CurlImport()
    source = str(text or "")
    if "$(" in source or "`" in source:
        imp.warnings.append("命令里含命令替换（$() 或反引号），已按字面解析，请核对结果")
    tokens = _tokenize(_strip_line_continuations(source))
    # 去掉 `curl` 本身与前后的 shell 包装（如 bash -c '...'）
    if tokens and tokens[0][0].lower() == "curl":
        tokens = tokens[1:]
    elif tokens and tokens[0][0].lower() in ("bash", "sh", "pwsh", "powershell"):
        tokens = tokens[1:]
        if tokens and tokens[0][0].startswith("-"):
            tokens = tokens[1:]
    if not tokens:
        imp.warnings.append("没有解析到任何内容，请确认粘贴的是完整的 curl 命令")
        return imp

    headers: Dict[str, str] = {}
    body_parts: List[str] = []
    url = ""
    method = ""
    index = 0
    while index < len(tokens):
        token, _quoted = tokens[index]
        if token in ("&&", "||", "|", ";", ">", ">>"):
            break
        flag, inline_value = _split_option(token)

        def take_value() -> str:  # noqa: ANN202  内部小工具
            nonlocal index
            if inline_value is not None:
                return inline_value
            if index + 1 < len(tokens):
                index += 1
                return tokens[index][0]
            return ""

        if flag in ("-X", "--request"):
            method = take_value().upper()
        elif flag in ("-H", "--header"):
            raw = take_value()
            if ":" in raw:
                name, value = raw.split(":", 1)
                name, value = name.strip(), value.strip()
                if name:
                    headers[name] = value
            elif raw.strip():
                imp.warnings.append(f"请求头格式不认识，已忽略: {raw}")
        elif flag in ("-d", "--data", "--data-raw", "--data-binary", "--data-ascii", "--json"):
            body_parts.append(take_value())
        elif flag in ("-u", "--user"):
            raw = take_value()
            imp.warnings.append(
                f"检测到 Basic 鉴权（-u {raw.split(':')[0] if ':' in raw else raw}:…）："
                "请在「额外请求头」里填 Authorization: Basic <base64>（或改用站点支持的 Key 方式）"
            )
        elif flag in ("--url",):
            url = take_value()
        elif flag in ("-b", "--cookie", "-e", "--referer", "-A", "--user-agent", "--compressed",
                      "-k", "--insecure", "--location", "-L", "--globoff", "-s", "--silent",
                      "-v", "--verbose", "--http1.1", "--http2"):
            if flag in ("-b", "--cookie", "-e", "--referer", "-A", "--user-agent") and inline_value is None:
                take_value()          # 这些带值，但值一律丢弃
        elif flag.startswith("-"):
            if inline_value is None and index + 1 < len(tokens) and not tokens[index + 1][0].startswith("-"):
                # 未知的带值选项：保守起见吃掉它的值，避免把值当成 URL
                take_value()
        elif not url:
            url = token
        index += 1

    if not url:
        imp.warnings.append("没有解析到请求 URL，请确认命令里有 --url 或裸地址")
        return imp
    imp.url = url
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        imp.warnings.append(f"URL 不完整（缺少 http(s):// 主机名）: {url}")
        imp.base_url = ""
    else:
        imp.base_url = f"{parts.scheme}://{parts.netloc}"
    imp.path = parts.path or ""
    imp.headers = headers
    imp.method = method if method in _HTTP_METHODS else ("POST" if body_parts else "GET")
    if method and method not in _HTTP_METHODS:
        imp.warnings.append(f"请求方法 {method} 不是常见取值，已按 {imp.method} 处理")

    raw_body = " ".join(part for part in body_parts if part).strip()
    imp.raw_body = raw_body
    if raw_body:
        parsed = _extract_json_body(raw_body)
        if parsed is None:
            # 表单/纯文本请求体：保留原文与警告，不生成 JSON 模板
            imp.warnings.append(
                "请求体不是 JSON（可能是表单或纯文本），已跳过请求体模板——"
                "请手工确认「请求体模板(JSON)」是否需要填写"
            )
        elif isinstance(parsed, (dict, list)):
            template, found, image_mode = _apply_placeholders(parsed)
            imp.body = parsed if isinstance(parsed, dict) else None
            imp.body_template = json.dumps(template, ensure_ascii=False, indent=2)
            imp.placeholders = found
            imp.image_mode = image_mode
            if not found:
                imp.warnings.append(
                    "请求体里没有识别出已知字段：模板已原样保留，可手工把提示词/图片换成 "
                    "$prompt / $image 等占位符"
                )
    if _looks_like_task_url(url):
        imp.warnings.append(
            "该 URL 看起来是「按任务 ID 查询」的轮询端点（不是提交端点）；"
            "已按原样填入提交端点，请确认后手动改到「轮询端点」"
        )
    if not headers:
        imp.warnings.append(
            "没有解析到请求头：鉴权方式请按站点要求选择（中转站常见 X-API-Key / api-key / URL 查询参数）"
        )
    return imp


# --------------------------------------------------------------------------- #
# 转配置 params
# --------------------------------------------------------------------------- #
def _url_with_injected_key(imp: CurlImport, key: str) -> str:
    """把 API Key 填进 URL 里的占位符（如 ``?key=YOUR_API_KEY``）。"""
    if not key:
        return imp.url
    out = re.sub(
        r"(?i)([?&](?:key|api_?key|token|access_token)=)[^&#]*",
        lambda m: m.group(1) + key,
        imp.url,
    )
    return out


def to_params(imp: CurlImport, api_key: str = "") -> dict:
    """把解析结果转成配置 params（provider=custom 起步）。

    产出：``provider=submit_url/request_method/submit_method/extra_headers/payload_template``，
    以及可猜出时填好的 ``poll_url``；其余字段（轮询方法、状态与结果路径、鉴权方式）
    仍需用户在设置页确认——中转站的返回结构差异太大，猜不如当场「测试并检测字段…」。
    """
    params: dict = {"provider": "custom"}
    if imp.url:
        params["submit_url"] = _url_with_injected_key(imp, api_key)
    if imp.method:
        params["request_method"] = imp.method
        # 视频适配以 submit_method 为准（BaseAPI.custom_method 用的是 request_method）
        params["submit_method"] = imp.method
    # 额外请求头：去掉 Content-Type/Accept 等自动生成的项
    kept = {k: v for k, v in (imp.headers or {}).items() if k.strip().lower() not in _IGNORED_HEADERS}
    if kept:
        params["extra_headers"] = json.dumps(kept, ensure_ascii=False, indent=2)
    # 请求体模板：只有请求体本身是 JSON 时才给出
    if imp.body is not None and imp.body_template:
        params["payload_template"] = imp.body_template
    if imp.base_url and imp.path:
        # 能猜出轮询端点时顺手填上（同一站点通常同前缀 + 任务 ID）
        if not _looks_like_task_url(imp.url):
            params["poll_url"] = f"{{base}}{imp.path}/{{id}}"
    return params
