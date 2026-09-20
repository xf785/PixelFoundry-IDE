"""中转站端点自适应：把「猜路径」变成「一键适配」。

## 为什么需要它

中转站/聚合站（relay / aggregator / one-api / new-api …）把上游服务商的接口
重新挂在自己的域名下，**路径往往与官方文档不同**：可灵官方的
``/v1/videos/image2video`` 在中转站上多半不存在（404 + ``Invalid URL``），
而 ``/v1/videos/generations``（OpenAI 风格）与
``/api/v3/contents/generations/tasks``（火山方舟风格）各有各的写法。
用户没法一眼看出自己的站点到底提供哪一条，只能逐个手填试错。

本模块把这件事自动化，两部分都**不依赖 Qt、不依赖网络**（可注入 httpx 传输层）：

1. :func:`normalize_base_url`：把用户粘贴的地址拆成「Base URL + 端点路径」。
   用户可以直接把服务商文档里那条**完整请求地址**粘进 Base URL 输入框。
2. :func:`probe_video_endpoints`：按 :data:`VIDEO_SUBMIT_CANDIDATES` 里的常见
   提交路径逐个发**无害的 GET**，按状态码与响应体判断「这条路径在不在」，
   给出推荐端点、轮询端点与「服务商适配」建议。

## 安全性（重要）

- 默认**只发 GET**：不会创建任务、不消耗额度；GET 打在不存在的路径上只会拿到
  404/405，打在对的路径上通常拿到 405/400/401/403，据此即可判断路径是否存在。
- ``allow_post=True`` 时会额外对「GET 判断为存在（含 405 只接受 POST）」的路径发一次
  POST，请求体故意写成极小且无效的 ``{"model": "__pixelfoundry_endpoint_probe__"}``。
  目的是区分「路径存在但拒绝我们的请求」与「路径不存在」这类模糊情况。
  **注意：宽松的中转站可能不做参数校验，直接为这次请求真实创建一个任务并计费**，
  所以该选项默认关闭，只在界面上由用户显式点击后才会开启。
- 所有网络异常都被收敛成 ``outcome="unknown"``，本模块**从不抛异常**。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

# --------------------------------------------------------------------------- #
# 常量：路径特征与常见提交端点
# --------------------------------------------------------------------------- #
# 路径片段里出现这些词，说明「这条地址指向具体接口」，末尾应拆到「提交端点」。
# 用「整段相等」比较（不是子串），避免把 /video-proxy 这类自定义前缀误判成端点。
_ENDPOINT_SEGMENT_HINTS = (
    "videos",
    "video",
    "images",
    "generations",
    "generation",
    "tasks",
    "task",
    "image2video",
    "video_generation",
)
# 需要两段合起来才认得出来的特征（避免把 /v1/chat 之类的前缀误当端点）
_ENDPOINT_PAIR_HINTS = ("chat/completions", "contents/generations")

# 前缀仍像「API 根路径」时保留在 Base URL 里（否则整个主机名才是 Base URL）
_API_ROOT_RE = re.compile(
    r"^/(?:v\d+(?:[.\-]?\d+)*(?:beta\d*)?"
    r"|api(?:/v\d+(?:beta\d*)?)?"
    r"|api/paas/v\d+"
    r"|compatible-mode/v\d+"
    r"|openai(?:/v\d+)?"
    r"|proxy(?:/v\d+)?)$",
    re.IGNORECASE,
)

# Base URL 说明模板（界面用 tr() 渲染；占位符 {0}/{1} 与英文包保持一致）
NOTE_ENDPOINT_SPLIT = "已把末尾的端点路径 {0} 拆到「提交端点」，Base URL 只保留 {1}"
NOTE_SCHEME_ADDED = "粘贴的地址缺少协议头，已自动按 https:// 处理"
NOTE_QUERY_DROPPED = "已忽略地址里的查询参数或锚点（?… / #…）"
NOTE_UNCHANGED = "Base URL 已是干净的地址（未包含多余端点路径）"
NOTE_NO_BASE_URL = "Base URL 为空，无法探测端点"

# 行说明：GET / POST 结论各一句（整句都是 i18n key）
GET_NOTE_BY_OUTCOME = {
    "ok": "端点可用：服务端正常响应了探测请求",
    "routed": "端点存在：服务端返回业务错误，说明路径有效",
    "auth": "端点存在：鉴权失败，请检查「鉴权方式」",
    "missing": "端点不存在：返回 404/405 或 Invalid URL / not found",
    "unknown": "无法判断：未收到可识别的响应",
}
POST_NOTE_BY_OUTCOME = {
    "ok": "POST 探测被接受（宽松站点可能已真实创建任务，请到站点后台确认）",
    "routed": "POST 被拒绝，说明端点有效（请求体不符合站点要求）",
    "auth": "POST 需要鉴权（请检查「鉴权方式」）",
    "missing": "POST 提示该路径不存在",
    "unknown": "POST 探测未收到可识别的响应",
}
# GET 返回 405：路径存在，只是不接受 GET（方舟这类只有 POST 的提交端点）
GET_NOTE_METHOD_NOT_ALLOWED = "端点存在：不接受 GET（这类端点只能用 POST 提交）"
# 网络异常：后面直接接底层异常文本（界面按前缀本地化）
NETWORK_NOTE_TEMPLATE = "网络异常：{0}"

# 汇总一句话（探到 / 没探到）
SUMMARY_FOUND = "已找到可用端点：{0}"
SUMMARY_FOUND_POST_ONLY = "找到只接受 POST 的端点：{0}（GET 返回 405；建议点「用 POST 再探测一次」确认）"
SUMMARY_NONE = "没有探测到可用端点"

# POST 探测用的「明显无效」请求体：模型名故意不存在，避免真的跑一次生成
POST_PROBE_PAYLOAD = {"model": "__pixelfoundry_endpoint_probe__"}

# 有序候选：越靠前越常见（同分时取靠前者）。覆盖中转站/厂商的常见形态：
# OpenAI 风格 /videos/generations、方舟 content 风格 contents/generations/tasks、
# 可灵 image2video、gpt.ge（V-API）的 /task/volces/seedance …
VIDEO_SUBMIT_CANDIDATES: Tuple[str, ...] = (
    "/v1/videos/generations",
    "/videos/generations",
    "/v1/video/generations",
    "/v1/videos",
    "/v1/videos/image2video",
    "/v1/image_to_video",
    "/v1/generations",
    "/v1/video_generation",
    "/v1/tasks",
    "/v1/task",
    "/contents/generations/tasks",
    "/api/v3/contents/generations/tasks",
    "/task/volces/seedance",
    "/v1/video/generations/image2video",
    "/v1/video/create",
    "/v1/images/videos",
)


# --------------------------------------------------------------------------- #
# 可翻译说明文本的小工具
# --------------------------------------------------------------------------- #
def _render_parts(
    parts: Sequence[Tuple[str, tuple]], translate: Optional[Callable[[str], str]]
) -> str:
    """把 [(模板, 参数)] 渲染成一句话。

    translate 为空时模板即为中文原文（core 不依赖 ui.i18n）；语言包缺词或占位符
    对不上时也会回退模板原文，保证界面永不因为翻译缺失而报错。
    """
    out: List[str] = []
    for template, args in parts:
        text = str(translate(template)) if translate is not None else template
        try:
            out.append(text.format(*args) if args else text)
        except (IndexError, KeyError, ValueError):
            out.append(template.format(*args) if args else template)
    return "；".join(out)


# --------------------------------------------------------------------------- #
# Base URL 规整
# --------------------------------------------------------------------------- #
@dataclass
class BaseUrlParts:
    """``normalize_base_url`` 的结果。

    - base_url：scheme + host + 可选 API 根路径前缀（无尾斜杠）；
    - endpoint：用户误粘进 Base URL 的端点路径（没有则为空串）；
    - changed：Base URL 与用户输入是否不同（补协议头/去尾斜杠/拆端点…）；
    - note：中文说明（可直接展示）；note_parts 供界面用 tr() 本地化。
    """

    base_url: str = ""
    endpoint: str = ""
    changed: bool = False
    note: str = ""
    note_parts: Tuple[Tuple[str, tuple], ...] = ()

    def localized_note(self, translate: Optional[Callable[[str], str]] = None) -> str:
        """按当前语言渲染说明；translate 传 ui.i18n.tr，为空则返回中文原文。"""
        if not self.note_parts:
            return self.note
        return _render_parts(self.note_parts, translate)


def _endpoint_split_index(segments: Sequence[str]) -> Optional[int]:
    """找出端点路径从第几段开始；整段路径都不像端点时返回 None。"""
    for index in range(len(segments)):
        segment = segments[index].lower()
        if segment in _ENDPOINT_SEGMENT_HINTS:
            return index
        if index + 1 < len(segments):
            pair = f"{segment}/{segments[index + 1].lower()}"
            if pair in _ENDPOINT_PAIR_HINTS:
                return index
    return None


def _looks_like_api_root(prefix: str) -> bool:
    """前缀是否像 API 根路径（/v1、/api/v3、/api/paas/v4、/compatible-mode/v1…）。"""
    return bool(_API_ROOT_RE.match(prefix.rstrip("/")))


def normalize_base_url(text: str) -> BaseUrlParts:
    """把用户粘贴的地址规整成「Base URL（+ 可选端点路径）」。

    规则（用户可以直接粘贴文档里的完整请求地址）：

    - 缺协议头时按 https:// 处理；去掉首尾空白与结尾 ``/``；丢弃查询串与锚点；
    - 路径里出现 :data:`_ENDPOINT_SEGMENT_HINTS`（videos / images / generations /
      tasks / task / image2video / video_generation / chat/completions /
      contents/generations …）时，把该处开始的路径拆进 ``endpoint``；
    - 拆出来的前缀仍像 API 根路径（/v1、/api/v3、/api/paas/v4、
      /compatible-mode/v1、/openai/v1…）就保留在 Base URL 里，否则 Base URL 只留
      scheme + host，整段原始路径进 ``endpoint``（信息不丢：``base + endpoint``
      始终等于用户粘贴的那条地址）。
    """
    raw = str(text or "").strip()
    if not raw:
        return BaseUrlParts(
            base_url="", endpoint="", changed=False,
            note=NOTE_NO_BASE_URL, note_parts=((NOTE_NO_BASE_URL, ()),),
        )

    pieces: List[Tuple[str, tuple]] = []
    scheme_added = False
    if not re.match(r"^[A-Za-z][A-Za-z0-9+.\-]*://", raw):
        raw = "https://" + raw.lstrip("/")
        scheme_added = True
    parsed = urlsplit(raw)
    host = f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else ""
    path = parsed.path or ""
    query_dropped = bool(parsed.query or parsed.fragment)

    segments = [s for s in path.split("/") if s]
    endpoint = ""
    split = _endpoint_split_index(segments)
    if split is None:
        base_url = f"{host}{path}".rstrip("/")
    else:
        prefix = "/" + "/".join(segments[:split]) if split else ""
        endpoint = "/" + "/".join(segments[split:])
        base_path = prefix if (not prefix or _looks_like_api_root(prefix)) else ""
        if not base_path:
            # 前缀不像 API 根路径：整段路径都是端点，Base URL 只留主机名
            endpoint = path if path.startswith("/") else f"/{path}"
        base_url = f"{host}{base_path}".rstrip("/")
        pieces.append((NOTE_ENDPOINT_SPLIT, (endpoint, base_url or host)))
    if scheme_added:
        pieces.append((NOTE_SCHEME_ADDED, ()))
    if query_dropped:
        pieces.append((NOTE_QUERY_DROPPED, ()))
    if not pieces:
        pieces.append((NOTE_UNCHANGED, ()))

    changed = (base_url != str(text or "").strip()) or bool(endpoint)
    return BaseUrlParts(
        base_url=base_url,
        endpoint=endpoint,
        changed=changed,
        note=_render_parts(pieces, None),
        note_parts=tuple(pieces),
    )


# --------------------------------------------------------------------------- #
# 探测结论
# --------------------------------------------------------------------------- #
class ProbeOutcome:
    """探测结论常量（字符串枚举：可直接存进配置或展示）。

    - ``ok``      服务端 2xx，正常响应；
    - ``routed``  路径存在但请求被拒（400/422，或结构化的业务错误）；
    - ``auth``    401/403：路径存在，鉴权没对上；
    - ``missing`` 404/405，或响应体说 Invalid URL / not found → 这条路径不存在；
    - ``unknown`` 其它情况（网络异常、无法解析的响应）。
    """

    OK = "ok"
    ROUTED = "routed"
    AUTH = "auth"
    MISSING = "missing"
    UNKNOWN = "unknown"

    ALL = (OK, ROUTED, AUTH, MISSING, UNKNOWN)
    # 表格里展示的短标签（界面用 tr() 翻译）
    LABELS = {
        OK: "可用",
        ROUTED: "存在",
        AUTH: "需鉴权",
        MISSING: "不存在",
        UNKNOWN: "未知",
    }
    # 完全没拿到响应码时的占位标签
    NO_RESPONSE = "无响应"

    # 「路径不存在」的响应体特征（中转站五花八门，这里只收最典型的几种）
    _MISSING_PHRASES = ("invalid url", "not found", "no such", "unknown request url")
    # 结构化错误体里常见的键（有其一即视为「服务端返回了业务错误」）
    _ERROR_KEYS = ("error", "errors", "error_message", "message", "msg", "code", "detail")

    @staticmethod
    def looks_like_error_body(body_text: Any) -> bool:
        """响应体是否像结构化的业务错误（JSON 对象且带 error/message/code 等键）。"""
        text = str(body_text or "").strip()
        if not text or text[0] not in "{[":
            return False
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            return False
        if not isinstance(data, dict):
            return False
        return any(key in data for key in ProbeOutcome._ERROR_KEYS)

    @staticmethod
    def classify(status_code: Optional[int], body_text: Any = "") -> str:
        """按状态码 + 响应体判断这条路径的状态。

        404 响应体里的 ``{"error": {"message": "Invalid URL (POST /v1/videos/image2video)"}}``
        是最典型的「这条路径在中转站上不存在」——它必须与「路径存在但参数不对」
        （400/422）区分开，前者要继续换路径试，后者就是我们要找的端点。
        """
        if status_code is None:
            return ProbeOutcome.UNKNOWN
        text = str(body_text or "").lower()
        if 200 <= status_code < 300:
            return ProbeOutcome.OK
        if status_code in (401, 403):
            return ProbeOutcome.AUTH
        if status_code in (404, 405):
            return ProbeOutcome.MISSING
        if any(phrase in text for phrase in ProbeOutcome._MISSING_PHRASES):
            return ProbeOutcome.MISSING
        if status_code in (400, 422):
            return ProbeOutcome.ROUTED
        if ProbeOutcome.looks_like_error_body(body_text):
            return ProbeOutcome.ROUTED
        return ProbeOutcome.UNKNOWN


# --------------------------------------------------------------------------- #
# 探测结果
# --------------------------------------------------------------------------- #
@dataclass
class EndpointRow:
    """单个候选端点的探测结果。

    ``get_status`` / ``post_status`` 为 None 表示该请求没拿到响应（网络异常）；
    ``post_*`` 全为空表示没做 POST 探测（默认行为）。
    """

    path: str
    get_status: Optional[int] = None
    get_outcome: str = ProbeOutcome.UNKNOWN
    post_status: Optional[int] = None
    post_outcome: str = ""
    note: str = ""
    note_parts: Tuple[Tuple[str, tuple], ...] = ()

    def localized_note(self, translate: Optional[Callable[[str], str]] = None) -> str:
        if not self.note_parts:
            return self.note
        return _render_parts(self.note_parts, translate)


@dataclass
class EndpointProbeReport:
    """端点探测汇总。

    - ``best``：按「POST 可用 > POST 业务错误 > GET 业务错误 > GET 可用 >
      GET 需鉴权」排序取最优，同分取 :data:`VIDEO_SUBMIT_CANDIDATES` 里靠前的；
    - ``submit_url_template`` / ``submit_url_absolute``：推荐提交端点（模板 / 绝对地址）；
    - ``poll_url_template``：按提交路径猜出的轮询端点（含 ``{id}``）；
    - ``provider_suggestion``：doubao / gptge / generic；
    - ``summary``：中文一句话（界面用 :meth:`localized_summary` 本地化）。
    """

    base_url: str = ""
    rows: List[EndpointRow] = field(default_factory=list)
    best: Optional[EndpointRow] = None
    submit_url_template: str = ""
    submit_url_absolute: str = ""
    poll_url_template: str = ""
    provider_suggestion: str = "generic"
    summary: str = ""
    summary_parts: Tuple[Tuple[str, tuple], ...] = ()
    allow_post: bool = False

    def localized_summary(self, translate: Optional[Callable[[str], str]] = None) -> str:
        """按当前语言渲染汇总一句话（translate 传 ui.i18n.tr）。"""
        if not self.summary_parts:
            return self.summary
        return _render_parts(self.summary_parts, translate)

    def row_paths(self) -> List[str]:
        """探测过的端点路径（测试/展示用）。"""
        return [row.path for row in self.rows]


# --------------------------------------------------------------------------- #
# 轮询端点 / 服务商适配 的推断
# --------------------------------------------------------------------------- #
def guess_poll_url_template(submit_url_template: str) -> str:
    """按提交端点猜轮询端点（含 ``{id}`` 占位符）。

    - gpt.ge（V-API）：提交 ``/task/volces/seedance``，查询在根路径 ``/task/{id}``；
    - 通义万相风格 ``…/video_generation``：查询 ``/v1/query/video_generation?task_id={id}``；
    - 其余（``/generations``、``/tasks``、``/image2video``、``/videos``…）：同路径追加 ``/{id}``。
    """
    text = str(submit_url_template or "").rstrip("/")
    if not text:
        return ""
    path = text.split("{base}", 1)[-1]
    lowered = path.lower()
    if lowered.startswith("/task/") or "/task/volces/" in lowered:
        return "{base}/task/{id}"
    if "video_generation" in lowered:
        return "{base}/v1/query/video_generation?task_id={id}"
    return f"{text}/{{id}}"


def provider_for_path(path: str) -> str:
    """按端点路径推荐「服务商适配」：方舟 content 数组 / gpt.ge / 通用。"""
    lowered = str(path or "").lower()
    if "contents/generations/tasks" in lowered:
        return "doubao"
    if "/task/volces/" in lowered:
        return "gptge"
    return "generic"


# --------------------------------------------------------------------------- #
# 探测主流程
# --------------------------------------------------------------------------- #
def _path_for_base(base_url: str, candidate: str) -> str:
    """把候选路径换算成相对当前 Base URL 的路径。

    Base URL 已经带了前缀（如 ``https://ark.cn-beijing.volces.com/api/v3``）时，
    候选 ``/api/v3/contents/generations/tasks`` 要退化成
    ``/contents/generations/tasks``，否则会拼出 ``…/api/v3/api/v3/…``（404）。
    """
    base_path = (urlsplit(base_url).path or "").rstrip("/")
    if base_path and candidate.lower().startswith(base_path.lower() + "/"):
        return candidate[len(base_path):]
    return candidate


def _request_once(client, method: str, url: str, payload: Optional[dict] = None):
    """发一次探测请求，返回 (status_code, body_text, error_text)，从不抛异常。"""
    try:
        target = client.auth_url(url)
        headers = client._headers()
        if method == "POST":
            resp = client._http().request(method, target, headers=headers, json=payload or {})
        else:
            resp = client._http().request(method, target, headers=headers)
    except Exception as exc:  # noqa: BLE001  探测不抛异常，一切收敛成 unknown
        return None, "", str(exc)
    return resp.status_code, resp.text or "", ""


def _probe_candidate(client, path: str, allow_post: bool) -> EndpointRow:
    """探测单个候选路径：先无害 GET，必要时再补一次 POST。"""
    url = f"{client.base_url.rstrip('/')}{path}"
    get_status, get_text, get_error = _request_once(client, "GET", url)
    get_outcome = ProbeOutcome.classify(get_status, get_text)
    parts: List[Tuple[str, tuple]] = []
    if get_error:
        parts.append((NETWORK_NOTE_TEMPLATE, (get_error,)))
    elif get_status == 405:
        parts.append((GET_NOTE_METHOD_NOT_ALLOWED, ()))
    else:
        parts.append((GET_NOTE_BY_OUTCOME.get(get_outcome, GET_NOTE_BY_OUTCOME[ProbeOutcome.UNKNOWN]), ()))

    post_status: Optional[int] = None
    post_outcome = ""
    # POST 只在「GET 说明路径可能存在」且确实拿到了响应时才发：
    # - outcome 不是 missing（404/Invalid URL 的路径再 POST 一次没有意义）；
    # - 例外：405 Method Not Allowed 说明**路径是存在的**，只是不接受 GET
    #   （方舟 contents/generations/tasks 这类只有 POST 的提交端点就是如此），
    #   这种情况必须靠 POST 才能确认，所以照样补一次；
    # - 网络不通（get_status is None）时再发一次同样不通，直接跳过。
    if allow_post and get_status is not None and (
        get_outcome != ProbeOutcome.MISSING or get_status == 405
    ):
        post_status, post_text, post_error = _request_once(client, "POST", url, POST_PROBE_PAYLOAD)
        post_outcome = ProbeOutcome.classify(post_status, post_text)
        if post_error:
            parts.append((NETWORK_NOTE_TEMPLATE, (post_error,)))
        else:
            parts.append((POST_NOTE_BY_OUTCOME.get(post_outcome, POST_NOTE_BY_OUTCOME[ProbeOutcome.UNKNOWN]), ()))

    return EndpointRow(
        path=path,
        get_status=get_status,
        get_outcome=get_outcome,
        post_status=post_status,
        post_outcome=post_outcome,
        note=_render_parts(parts, None),
        note_parts=tuple(parts),
    )


def _row_rank(row: EndpointRow) -> Optional[int]:
    """择优排序的权重（越小越好）；返回 None 表示这条不能当推荐端点。"""
    if row.post_outcome == ProbeOutcome.OK:
        return 0
    if row.post_outcome == ProbeOutcome.ROUTED:
        return 1
    if row.get_outcome == ProbeOutcome.ROUTED:
        return 2
    if row.get_outcome == ProbeOutcome.OK:
        return 3
    if row.get_outcome == ProbeOutcome.AUTH:
        return 4
    # GET 返回 405：路径存在、只是不接受 GET（只有 POST 的提交端点就是这样）。
    # 排在有响应证据的候选之后，但**仍然可以推荐** —— 否则「默认不发 POST」的探测
    # 永远找不到这类端点（真实中转站里很常见）。
    if row.get_status == 405:
        return 5
    return None


def _pick_best(rows: Sequence[EndpointRow]) -> Optional[EndpointRow]:
    """选最优端点：按权重取最小，同分取候选列表里靠前的（rows 已按候选顺序）。"""
    best: Optional[EndpointRow] = None
    best_rank: Optional[int] = None
    for row in rows:
        rank = _row_rank(row)
        if rank is None:
            continue
        if best_rank is None or rank < best_rank:
            best, best_rank = row, rank
    return best


def probe_video_endpoints(
    base_url: str,
    api_key: str = "",
    params: Optional[Dict[str, Any]] = None,
    transport: Optional[Any] = None,
    allow_post: bool = False,
    timeout: float = 10.0,
) -> EndpointProbeReport:
    """逐个探测常见视频提交端点，给出推荐配置。

    参数：
    - base_url：中转站地址（可以带端点路径，内部先走 :func:`normalize_base_url`）；
    - api_key / params：与设置页一致，鉴权方式（auth_style / 额外请求头）沿用
      :class:`~core.api.video_api.VideoAPI` 的逻辑，因此「探测用的请求」与
      「真正生成时的请求」鉴权完全一致；``params.timeout`` 会被 ``timeout`` 覆盖，
      ``params.max_retries`` 默认 0（探测失败就换下一条路径，不重试拖时间）；
    - transport：注入的 httpx 传输层（单测用 ``httpx.MockTransport``，不联网）；
    - allow_post：是否补一次 POST 探测。**默认关闭**；打开后本函数会向站点真实
      提交一次请求，宽松的中转站可能真的创建任务并计费，请只在用户明确同意时使用。
      补 POST 的范围：GET 判定为「路径可能存在」的候选，外加 GET 返回 405 的候选
      （405 说明路径存在、只是不接受 GET，如方舟只有 POST 的提交端点）；
    - timeout：单次探测请求的超时（秒）。

    永远不抛异常；网络异常会记进对应行的 ``note``（``outcome="unknown"``）。
    """
    parts = normalize_base_url(base_url)
    base = parts.base_url
    if not base:
        return EndpointProbeReport(
            base_url="",
            summary=NOTE_NO_BASE_URL,
            summary_parts=((NOTE_NO_BASE_URL, ()),),
            allow_post=allow_post,
        )

    # 延迟导入：避免 core.api 包内部在导入期互相牵扯
    from .video_api import VideoAPI

    probe_params = dict(params or {})
    probe_params.setdefault("max_retries", 0)
    probe_params["timeout"] = float(timeout)
    client = VideoAPI(
        {"base_url": base, "api_key": str(api_key or ""), "params": probe_params},
        transport=transport,
    )

    rows: List[EndpointRow] = []
    try:
        seen = set()
        for candidate in VIDEO_SUBMIT_CANDIDATES:
            path = _path_for_base(base, candidate)
            if path in seen:
                continue
            seen.add(path)
            rows.append(_probe_candidate(client, path, allow_post))
    finally:
        client.close()

    best = _pick_best(rows)
    submit_template = f"{{base}}{best.path}" if best is not None else ""
    if best is not None:
        # 只靠 GET 405 推出来的端点尚未用 POST 确认，摘要里说清楚
        post_only = best.get_status == 405 and not best.post_outcome
        template = SUMMARY_FOUND_POST_ONLY if post_only else SUMMARY_FOUND
        summary_parts: Tuple[Tuple[str, tuple], ...] = ((template, (best.path,)),)
    else:
        summary_parts = ((SUMMARY_NONE, ()),)
    return EndpointProbeReport(
        base_url=base,
        rows=rows,
        best=best,
        submit_url_template=submit_template,
        submit_url_absolute=submit_template.replace("{base}", base) if submit_template else "",
        poll_url_template=guess_poll_url_template(submit_template) if submit_template else "",
        provider_suggestion=provider_for_path(best.path) if best is not None else "generic",
        summary=_render_parts(summary_parts, None),
        summary_parts=summary_parts,
        allow_post=allow_post,
    )
