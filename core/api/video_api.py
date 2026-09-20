"""图转视频 API：通用「提交任务 -> 轮询 -> 取结果」模型。

适配主流服务商（如 Luma、Kling、可灵、Runway 等）与各类**中转站/聚合站**
（relay / aggregator / one-api / new-api 等）的常见差异点通过 params 配置项控制：
- submit_url / poll_url   端点覆盖（默认 {base}/videos/generations），支持 {base} / {id}
- submit_method           提交方法（POST 默认，可 PUT / GET）
- job_id_path             任务 ID 的 JSON 路径（默认 "id"，取不到时按内置候选路径回退）
- status_path             状态字段路径（默认 "status"）
- status_success / status_failure  成功/失败状态值列表
- result_video_url_path   结果视频 URL 路径（默认 "output.video_url"，取不到时回退）
- result_frames_path      结果帧序列路径（默认 "output.frames"）
- poll_method             轮询方法（默认 GET，个别服务商用 POST/PUT）
- poll_payload_template   轮询请求体模板（POST/PUT 轮询时发送，支持 $task_id 等占位符）
- max_polls / poll_interval        轮询上限与间隔（秒）
- extra_payload           额外请求字段（JSON 字符串或 dict）
- auth_style / auth_header / auth_prefix / auth_query_param  鉴权方式（见 BaseAPI.auth_url）

请求体模板（payload_template / poll_payload_template）占位符：
$model / $prompt / $negative_prompt / $image（data URI）/ $image_raw（裸 base64）/
$image_url（params.image_url，用户自备图床 URL）/ $last_image / $frames / $fps /
$duration / $seed / $ratio / $resolution / $mode，轮询模板额外支持 $task_id。

内置服务商适配（params.provider）：
- generic（默认）：OpenAI 兼容轮询式
- doubao / ark：火山方舟 Doubao Seedance（contents/generations/tasks，
  content 数组携带首帧图片，时长由帧数/帧率换算，结果取 content.video_url）
- gptge：gpt.ge (V-API) 网关的豆包视频（提交 {base}/task/volces/seedance，
  轮询 {base}/task/{id}，请求体/结果解析与火山方舟一致）

调试辅助（都不改变 call() 的行为）：
- preview_request()：只组装、不发送，返回可复制的请求预览（Key 已打码）；
- probe()：只发一次提交请求，返回原始响应 + 字段路径建议（供「测试并检测字段」用）。

返回 APIResult(data={"video_url": str|None, "frames": [bytes]|None})。
若服务商直接返回帧序列（base64 图片列表），则 frames 非空。
"""
from __future__ import annotations

import base64
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from .base import APIResult, BaseAPI

logger = logging.getLogger("PixelFoundry.api.video")

_DOUBAO_PROVIDERS = ("doubao", "ark", "volcengine")
_GPTGE_PROVIDER = "gptge"

# 任务 ID 的候选回退路径（配置的 job_id_path 取不到时依次尝试）
_JOB_ID_FALLBACKS: Tuple[str, ...] = (
    "data.task_id",
    "data.id",
    "task_id",
    "request_id",
    "data.request_id",
    "output.task_id",
    "data.taskId",
    "taskId",
)

# 结果视频 URL 的候选回退路径（配置的 result_video_url_path 取不到时依次尝试）
_VIDEO_URL_FALLBACKS: Tuple[str, ...] = (
    "data.video_url",
    "video_url",
    "data.output.video_url",
    "output.url",
    "data.url",
    "url",
    "data.video.url",
    "videos.0.url",
    "data.outputs.0.url",
    "result.video_url",
    "content.video_url",
    "output.videos.0.url",
)

# 「测试并检测字段」建议路径时可识别的键名（小写比较）
_ID_KEY_HINTS = ("task_id", "taskid", "request_id", "id")
_STATUS_KEY_HINTS = ("task_status", "status", "state")
_URL_KEY_HINTS = ("video_url", "videourl", "video", "output", "url")

# 建议值的长度上限（预览用）与「状态」类短字符串上限
_VALUE_PREVIEW_LIMIT = 120
_STATUS_MAX_LEN = 24


class VideoAPI(BaseAPI):
    KIND = "video"

    @property
    def provider(self) -> str:
        return str(self.params.get("provider") or "generic").lower()

    def _is_doubao_style(self) -> bool:
        """请求体/结果解析使用火山方舟 content 数组格式的服务商。"""
        return self.provider in _DOUBAO_PROVIDERS or self.provider == _GPTGE_PROVIDER

    def _last_frame_enabled(self) -> bool:
        """是否把首帧同时作为尾帧传入（首尾帧一致）。"""
        v = self.params.get("last_frame", False)
        return v not in (False, 0, "0", "false", "False", "", None)

    def _task_base(self) -> str:
        """gpt.ge 等网关的任务端点位于根路径（无 /v1），两种填法都容忍。"""
        base = self.base_url.rstrip("/")
        if base.endswith("/v1"):
            return base[:-3]
        return base

    # ------------------------------------------------------------------ #
    def _submit_url(self) -> str:
        override = self.params.get("submit_url")
        if override:
            return str(override).format(base=self.base_url).rstrip("/")
        if self.provider == _GPTGE_PROVIDER:
            return f"{self._task_base()}/task/volces/seedance"
        if self._is_doubao_style():
            return f"{self.base_url}/contents/generations/tasks"
        return f"{self.base_url}/videos/generations"

    def _poll_url(self, job_id: str) -> str:
        template = self.params.get("poll_url")
        if template:
            return str(template).format(base=self.base_url, id=job_id)
        if self.provider == _GPTGE_PROVIDER:
            return f"{self._task_base()}/task/{job_id}"
        if self._is_doubao_style():
            return f"{self.base_url}/contents/generations/tasks/{job_id}"
        return f"{self.base_url}/videos/generations/{job_id}"

    def _result_video_url_path(self) -> str:
        return self.params.get("result_video_url_path") or (
            "content.video_url" if self._is_doubao_style() else "output.video_url"
        )

    def _submit_method(self) -> str:
        """提交请求方法（params.submit_method，默认 POST）。"""
        return str(self.params.get("submit_method") or "POST").upper()

    def _poll_method(self) -> str:
        """轮询请求方法（params.poll_method，默认 GET）。"""
        return str(self.params.get("poll_method") or "GET").upper()

    # ------------------------------------------------------------------ #
    def _status_set(self, key: str, default: list) -> set:
        """状态值集合，兼容逗号分隔字符串与列表。"""
        value = self.params.get(key)
        if isinstance(value, str):
            parts = {s.strip().lower() for s in value.split(",") if s.strip()}
            # 空字符串（设置页表单未填）视为未配置，回退默认值
            if parts:
                return parts
            return {str(s).lower() for s in default}
        if value:
            return {str(s).lower() for s in value}
        return {str(s).lower() for s in default}

    # ------------------------------------------------------------------ #
    # 路径回退：中转站的返回结构五花八门，配置路径取不到时按候选继续找
    # ------------------------------------------------------------------ #
    def _find_job_id(self, data: Any) -> Optional[str]:
        """取任务 ID：先按 job_id_path（默认 id），再依次尝试常见候选路径。

        覆盖中转站常见的 {"data": {"task_id": …}} / {"request_id": …} 等结构。
        """
        candidates = [str(self.params.get("job_id_path") or "id")] + list(_JOB_ID_FALLBACKS)
        for path in candidates:
            value = self._dig(data, path)
            if value not in (None, "", [], {}):
                return value if isinstance(value, str) else str(value)
        return None

    def _find_video_url(self, data: Any) -> Optional[str]:
        """取结果视频 URL：先按 result_video_url_path，再依次尝试常见候选路径。"""
        paths = [self._result_video_url_path()] + list(_VIDEO_URL_FALLBACKS)
        for path in paths:
            value = self._dig(data, path)
            if isinstance(value, str) and value.strip():
                return value
            if isinstance(value, dict):
                # 形如 {"video": {"url": …}} / {"output": {"url": …}}
                inner = value.get("url") or value.get("video_url") or value.get("uri")
                if isinstance(inner, str) and inner.strip():
                    return inner
        return None

    # ------------------------------------------------------------------ #
    # 请求体模板渲染
    # ------------------------------------------------------------------ #
    @staticmethod
    def _json_escape(value: Any) -> str:
        """把值转成可安全嵌入 JSON 字符串的文本（引号/换行会转义）。

        模板里的占位符通常写作 "$prompt"（自带引号），替换进去的必须是已转义
        的字符串内容，否则提示词里的引号/换行会把模板 JSON 破坏掉。
        """
        text = "" if value is None else str(value)
        return json.dumps(text, ensure_ascii=False)[1:-1]

    def _template_values(
        self,
        image_bytes: Optional[bytes],
        prompt: str,
        frames: Optional[int],
        fps: Optional[int],
        duration: Optional[float],
        task_id: Optional[str] = None,
    ) -> Dict[str, str]:
        """组装模板占位符的取值表（提交与轮询共用；$task_id 仅轮询时有值）。

        值来源：params（negative_prompt/seed/ratio/resolution/mode/image_url 等）
        与调用参数；图片同时提供 data URI（$image）、裸 base64（$image_raw）与
        用户自备图床 URL（$image_url，未配置为空串）。
        """
        frame_count = int(frames if frames is not None else self.params.get("frames", 8))
        frame_rate = int(fps if fps is not None else self.params.get("fps", 8))
        dur = int(round(float(duration))) if duration else max(5, min(10, round(frame_count / max(1, frame_rate))))
        image_bytes = image_bytes or b""
        raw_b64 = base64.b64encode(image_bytes).decode("ascii") if image_bytes else ""
        values: Dict[str, str] = {
            "model": self.model,
            "prompt": self._json_escape(prompt),
            "negative_prompt": self._json_escape(self.params.get("negative_prompt") or ""),
            "image": f"data:image/png;base64,{raw_b64}" if raw_b64 else "",
            "image_raw": raw_b64,
            "image_url": self._json_escape(self.params.get("image_url") or ""),
            "last_image": f"data:image/png;base64,{raw_b64}" if raw_b64 else "",
            "frames": str(frame_count),
            "fps": str(frame_rate),
            "duration": str(dur),
            "seed": str(int(self.params.get("seed", -1))),
            "ratio": self._json_escape(self.params.get("ratio") or ""),
            "resolution": self._json_escape(self.params.get("resolution") or ""),
            "mode": self._json_escape(self.params.get("mode") or ""),
        }
        if task_id is not None:
            values["task_id"] = self._json_escape(task_id)
        return values

    def _render_template(
        self,
        template: str,
        image_bytes: Optional[bytes],
        prompt: str,
        frames: Optional[int],
        fps: Optional[int],
        duration: Optional[float],
        task_id: Optional[str] = None,
    ) -> Optional[dict]:
        """渲染 JSON 模板为 dict；模板非法 JSON 返回 None。

        $image / $image_raw 的值（base64 很长）在这里直接替换，其余占位符交给
        BaseAPI.render_template_payload —— 它按标识符边界匹配，长名优先，
        所以 $image 不会误伤 $image_url（详见该方法说明）。
        """
        raw_b64 = base64.b64encode(image_bytes).decode("ascii") if image_bytes else ""
        text = re.sub(r"\$image_raw(?![0-9A-Za-z_])", lambda _m: raw_b64, str(template))
        text = re.sub(r"\$image(?![0-9A-Za-z_])", lambda _m: self._to_data_uri(image_bytes or b""), text)
        values = self._template_values(image_bytes, prompt, frames, fps, duration, task_id)
        values.pop("image", None)      # $image 已在上面精确替换
        values.pop("image_raw", None)  # $image_raw 同上
        return self.render_template_payload(text, values)

    def _flatten_query(self, payload: Any, prefix: str = "") -> List[tuple]:
        """把 JSON 请求体摊平成查询参数（**只保留标量值**，数组整体跳过）。

        用于 submit_method / poll_method = GET 的场景：中转站有时用
        GET /task?prompt=…&image=… 这种纯查询串形式；嵌套对象用 a.b 点号展开，
        布尔转 true/false，数组（如 tags）整体不发送——GET 下把数组展开成
        tags.0=… 既不符合多数服务端预期，也容易撞上长度限制。
        """
        out: List[tuple] = []
        if not isinstance(payload, dict):
            return out
        for key, value in payload.items():
            name = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, bool):
                out.append((name, "true" if value else "false"))
            elif isinstance(value, (int, float)):
                out.append((name, str(value)))
            elif isinstance(value, str):
                out.append((name, value))
            elif isinstance(value, dict):
                out.extend(self._flatten_query(value, name))
        return out

    def _send_json(self, method: str, url: str, payload: Optional[dict]) -> Any:
        """按方法发送请求并解析 JSON；GET 把请求体摊平成查询参数。

        文档化行为：
        - POST / PUT：请求体以 JSON 发送（模板保持原结构）；
        - GET：模板按「查询参数」发送——JSON 形状的请求体被摊平成 k=v
          （嵌套结构用 a.b 形式，布尔转 true/false，仅标量；数组/对象本身
          不作为值发送），因为 GET 没有请求体。
        """
        method = str(method or "POST").upper()
        if method == "GET":
            params = self._flatten_query(payload) if payload else None
            resp = self._request("GET", url, params=params)
        elif method == "PUT":
            resp = self._request("PUT", url, json=payload or {})
        else:
            resp = self._request("POST", url, json=payload or {})
        return self._parse_json(resp)

    def _payload_from_template(
        self,
        template: str,
        image_bytes: bytes,
        prompt: str,
        frames: Optional[int],
        fps: Optional[int],
        duration: Optional[float],
    ) -> dict:
        """按 JSON 模板渲染提交请求体（非法模板回退最小可用请求体）。"""
        rendered = self._render_template(template, image_bytes, prompt, frames, fps, duration)
        if rendered is not None:
            return rendered
        logger.warning("payload_template 渲染后不是合法 JSON，回退默认请求体: %s", str(template)[:200])
        return {
            "model": self.model,
            "prompt": prompt,
            "image": self._to_data_uri(image_bytes),
        }

    def _build_payload(
        self,
        image_bytes: bytes,
        prompt: str,
        frames: Optional[int],
        fps: Optional[int],
        duration: Optional[float],
    ) -> dict:
        """按服务商组装提交请求体（首帧图片始终包含）。"""
        template = self.params.get("payload_template")
        if template:
            return self._payload_from_template(template, image_bytes, prompt, frames, fps, duration)
        if self._is_doubao_style():
            # 火山方舟 Seedance：content 数组携带文本 + 首帧图片（data URI）
            # last_frame 开启时，把同一张图作为尾帧一并传入（首尾帧一致）
            payload: dict = {
                "model": self.model,
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": self._to_data_uri(image_bytes)}},
                ],
            }
            if self._last_frame_enabled():
                payload["content"].append(
                    {"type": "image_url", "image_url": {"url": self._to_data_uri(image_bytes)}}
                )
            if duration:
                payload["duration"] = int(round(float(duration)))
            elif frames and fps:
                secs = round(int(frames) / max(1, int(fps)))
                payload["duration"] = max(5, min(10, secs))
        else:
            payload = {
                "model": self.model,
                "prompt": prompt,
                "image": self._to_data_uri(image_bytes),
                "frame_num": int(frames if frames is not None else self.params.get("frames", 8)),
                "fps": int(fps if fps is not None else self.params.get("fps", 8)),
            }
            if self._last_frame_enabled():
                # 通用服务商：尽力而为，把首帧同时作为尾帧（字段名可能因服务商而异，
                # 也可用 payload_template + $last_image 精确指定）
                payload["last_image"] = self._to_data_uri(image_bytes)
            template = self.params.get("prompt_template")
            if template and "{prompt}" in template:
                payload["prompt"] = template.format(
                    prompt=prompt,
                    frames=payload["frame_num"],
                    fps=payload["fps"],
                )
            if duration:
                payload["duration"] = float(duration)

        extra = self.params.get("extra_payload")
        if extra:
            if isinstance(extra, str):
                try:
                    extra = json.loads(extra)
                except (ValueError, TypeError):
                    logger.warning("extra_payload 不是合法 JSON: %s", extra)
                    extra = {}
            if isinstance(extra, dict):
                payload.update(extra)
        return payload

    # ------------------------------------------------------------------ #
    def call(
        self,
        image_bytes: bytes,
        prompt: str,
        frames: Optional[int] = None,
        fps: Optional[int] = None,
        duration: Optional[float] = None,
    ) -> APIResult:
        """以首帧图片 + 提示词生成动画，返回视频 URL 或帧序列字节。"""
        error = self._validate_config()
        if error:
            return APIResult(ok=False, error=error)

        frames = int(frames if frames is not None else self.params.get("frames", 8))
        fps = int(fps if fps is not None else self.params.get("fps", 8))

        payload = self._build_payload(image_bytes, prompt, frames, fps, duration)

        try:
            data = self._send_json(self._submit_method(), self._submit_url(), payload)
        except Exception as exc:  # noqa: BLE001
            logger.exception("图转视频提交失败")
            return APIResult(ok=False, error=self._friendly_error(exc))

        job_id = self._find_job_id(data)
        if job_id is None:
            return APIResult(ok=False, error=f"无法从响应中获取任务 ID: {str(data)[:300]}", raw=data)

        # 部分服务商同步返回结果（如直接给 URL），无需轮询
        video_url = self._find_video_url(data)
        frames_bytes = self._extract_frames_from(data, self.params.get("result_frames_path") or "output.frames")
        if video_url or frames_bytes:
            return APIResult(
                ok=True,
                data={"video_url": video_url, "frames": frames_bytes, "job_id": job_id},
                raw=data,
            )

        poll_result = self._poll(job_id)
        if not poll_result.ok:
            return poll_result
        return APIResult(ok=True, data={**poll_result.data, "job_id": job_id}, raw=poll_result.raw)

    # ------------------------------------------------------------------ #
    def _poll(self, job_id: str) -> APIResult:
        max_polls = int(self.params.get("max_polls", 120))
        interval = float(self.params.get("poll_interval", 5))
        method = self._poll_method()
        success = self._status_set("status_success", ["succeeded", "success", "completed", "done", "finished"])
        failure = self._status_set("status_failure", ["failed", "error", "cancelled", "canceled", "expired", "rejected"])
        frames_path = self.params.get("result_frames_path") or "output.frames"

        # 先轮询、后休眠：任务若在两次轮询之间完成可提前一个 interval 拿到结果
        # （不会因「提交后先空等一个间隔」而额外增加延迟）
        for attempt in range(max_polls):
            try:
                data = self._send_json(method, self._poll_url(job_id), self._poll_payload(job_id))
            except Exception as exc:  # noqa: BLE001
                # 轮询期间的网络抖动不致命，继续尝试
                logger.warning("轮询异常（第 %d 次）: %s", attempt + 1, exc)
            else:
                status = str(self._dig(data, self.params.get("status_path") or "status") or "").lower()
                if status in failure:
                    err = (
                        self._dig(data, "error.message")
                        or self._dig(data, "error_message")
                        or self._dig(data, "error")
                        or data
                    )
                    err = str(err)[:300] if err is not None else ""
                    return APIResult(ok=False, error=f"视频任务失败（{status}）: {err}", raw=data)
                if status in success or status in ("", "none"):
                    video_url = self._find_video_url(data)
                    frames_bytes = self._extract_frames_from(data, frames_path)
                    if video_url or frames_bytes or status in success:
                        return APIResult(
                            ok=True,
                            data={"video_url": video_url, "frames": frames_bytes},
                            raw=data,
                        )
                logger.info("视频任务 %s 轮询中（%d/%d）: %s", job_id, attempt + 1, max_polls, status)
            if attempt < max_polls - 1:
                time.sleep(interval)

        waited_secs = max(0, (max_polls - 1)) * interval
        return APIResult(ok=False, error=f"轮询超时（{max_polls} 次，约 {waited_secs:.0f} 秒）")

    def _poll_payload(self, job_id: str) -> Optional[dict]:
        """轮询请求体：配置了 poll_payload_template 且轮询方法非 GET 时渲染发送。

        占位符与提交模板一致，另有 $task_id（当前任务 ID）。
        """
        template = self.params.get("poll_payload_template")
        if not template or not str(template).strip():
            return None
        if self._poll_method() == "GET":
            logger.info("poll_method=GET：poll_payload_template 会被摊平成查询参数发送")
        return self._render_template(str(template), None, "", None, None, None, task_id=job_id)

    @staticmethod
    def _extract_frames_from(data: dict, path: str) -> List[bytes]:
        """从响应中解析帧序列（base64 图片列表 / url 列表）。"""
        frames: List[bytes] = []
        raw = VideoAPI._dig(data, path)
        if not raw:
            return frames
        if isinstance(raw, dict):
            raw = raw.get("frames") or raw.get("images") or raw.get("items")
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    b64 = item.get("b64_json") or item.get("base64")
                    if b64:
                        try:
                            if isinstance(b64, str) and b64.startswith("data:"):
                                b64 = b64.split(",", 1)[1]
                            frames.append(base64.b64decode(b64))
                        except Exception:  # noqa: BLE001
                            continue
        return frames

    @staticmethod
    def _to_data_uri(image_bytes: bytes) -> str:
        return "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")

    # ------------------------------------------------------------------ #
    # 请求预览 / 探测（都不参与真正的生成流程，只用于配置排查）
    # ------------------------------------------------------------------ #
    def _redact_headers(self, headers: dict) -> dict:
        """把请求头里的 API Key 打码（保留 Bearer 前缀，便于确认鉴权方式）。"""
        out = {}
        for key, value in headers.items():
            text = str(value)
            if self.api_key and self.api_key in text:
                text = text.replace(self.api_key, "***")
            out[key] = text
        return out

    def _redact_url(self, url: str) -> str:
        """把 URL 查询参数里的 API Key 打码（auth_style=query 时会出现在 URL 上）。"""
        if self.api_key and self.api_key in url:
            return url.replace(self.api_key, "***")
        return url

    def preview_request(
        self,
        image_bytes: bytes,
        prompt: str,
        frames: Optional[int] = None,
        fps: Optional[int] = None,
        duration: Optional[float] = None,
    ) -> dict:
        """组装提交请求的完整预览，**不发送任何网络请求**。

        返回 {"method", "url", "headers", "body"}；API Key 在 headers/url 里
        一律打码为 "***"（Authorization: "Bearer ***"），可直接复制分享。
        """
        payload = self._build_payload(image_bytes, prompt, frames, fps, duration)
        method = self._submit_method()
        url = self._redact_url(self.auth_url(self._submit_url()))
        return {
            "method": method,
            "url": url,
            "headers": self._redact_headers(self._headers()),
            "body": payload,
        }

    @staticmethod
    def _looks_like_id(value: Any) -> bool:
        return isinstance(value, str) and 0 < len(value.strip()) <= 128 and "\n" not in value

    @staticmethod
    def _looks_like_status(value: Any) -> bool:
        return isinstance(value, str) and 0 < len(value.strip()) <= _STATUS_MAX_LEN and "\n" not in value

    @staticmethod
    def _looks_like_url(value: Any) -> bool:
        return isinstance(value, str) and value.strip().startswith(("http://", "https://", "//", "data:"))

    @classmethod
    def _suggest_paths(cls, obj: Any) -> Dict[str, List[str]]:
        """在响应 JSON 里猜字段路径，返回 {"job_id_path": [...], …}（每类最多 3 条）。

        规则：按常见键名（id/task_id、status/state、url/video_url/output/video）
        递归查找，且值必须「像那一类」（id/url 为非空字符串、url 还须是链接形，
        status 为短字符串），避免把一堆无关字段混进建议里。
        返回的路径都是 _dig 可解析的点号路径（数组用数字下标）。
        """
        buckets: Dict[str, List[str]] = {"job_id_path": [], "status_path": [], "result_video_url_path": []}
        seen = {"job_id_path": set(), "status_path": set(), "result_video_url_path": set()}

        def add(category: str, path: str) -> None:
            if path in seen[category] or len(buckets[category]) >= 3:
                return
            seen[category].add(path)
            buckets[category].append(path)

        def walk(node: Any, path: str, depth: int) -> None:
            if depth > 4:
                return
            if isinstance(node, dict):
                for key, value in node.items():
                    key_text = str(key)
                    child = f"{path}.{key_text}" if path else key_text
                    low = key_text.lower()
                    if cls._looks_like_id(value) and low in _ID_KEY_HINTS:
                        add("job_id_path", child)
                    if cls._looks_like_status(value) and low in _STATUS_KEY_HINTS:
                        add("status_path", child)
                    if cls._looks_like_url(value) and any(h in low for h in _URL_KEY_HINTS):
                        add("result_video_url_path", child)
                    walk(value, child, depth + 1)
            elif isinstance(node, list):
                for index, item in enumerate(node[:5]):
                    walk(item, f"{path}.{index}", depth + 1)

        walk(obj, "", 0)
        return buckets

    def probe(
        self,
        image_bytes: Optional[bytes] = None,
        prompt: str = "a small red cube rotating",
        frames: Optional[int] = None,
        fps: Optional[int] = None,
        duration: Optional[float] = None,
    ) -> APIResult:
        """只发一次**提交请求**（不轮询），把原始响应与字段路径建议交回给用户。

        永远不会抛异常：网络/HTTP/解析问题都收敛成 APIResult(ok=False, error=…)，
        以便「测试并检测字段」按钮把失败原因原样展示出来。

        返回 data = {"raw_text", "json", "suggestions", "status_code", "request"}：
        - request：与 preview_request() 相同结构的请求预览（Key 已打码）；
        - suggestions：_suggest_paths() 猜出的候选字段路径（每类最多 3 条），
          供界面一键写回「任务ID/状态/视频URL 字段路径」。
        """
        request_preview = self.preview_request(image_bytes or b"", prompt, frames, fps, duration)
        payload = request_preview["body"]
        method = request_preview["method"]
        url = self.auth_url(self._submit_url())
        base_data = {"request": request_preview}
        try:
            if method == "GET":
                resp = self._request("GET", url, params=self._flatten_query(payload) if payload else None)
            elif method == "PUT":
                resp = self._request("PUT", url, json=payload or {})
            else:
                resp = self._request("POST", url, json=payload or {})
        except Exception as exc:  # noqa: BLE001
            # 4xx/5xx 会抛 APIError，但原始响应已由 _request 记录：照样把状态码与响应体
            # 交回界面，用户才能看清中转站到底回了什么（探测永远不抛异常）。
            failure = self.last_error_response or {}
            return APIResult(
                ok=False,
                error=self._friendly_error(exc),
                raw=failure.get("text"),
                data={
                    **base_data,
                    "status_code": failure.get("status_code"),
                    "raw_text": failure.get("text") or "",
                    "json": None,
                    "suggestions": {"job_id_path": [], "status_path": [], "result_video_url_path": []},
                },
            )

        raw_text = resp.text or ""
        try:
            parsed = resp.json()
        except ValueError:
            parsed = None
        suggestions = self._suggest_paths(parsed) if isinstance(parsed, (dict, list)) else {
            "job_id_path": [], "status_path": [], "result_video_url_path": [],
        }
        ok = 200 <= resp.status_code < 300 and parsed is not None
        error = None
        if not ok:
            status = resp.status_code
            detail = raw_text[:300] if raw_text else "（响应体为空）"
            if not (200 <= status < 300):
                error = self._friendly_error(f"HTTP {status}: {detail}", status=status)
            else:
                error = f"响应不是合法 JSON: {detail}"
        return APIResult(
            ok=ok,
            error=error,
            raw=parsed if parsed is not None else raw_text,
            data={
                **base_data,
                "raw_text": raw_text,
                "json": parsed,
                "suggestions": suggestions,
                "status_code": resp.status_code,
            },
        )

    # ------------------------------------------------------------------ #
    def test_connection(self) -> APIResult:
        """连通性测试：只做一次 GET Base URL 的原始请求，**从不抛异常**。

        分类（中转站的根路径返回 404 是常态，不能据此判定失败）：
        - 2xx/3xx：服务可达；
        - 401/403：服务可达但鉴权失败，提示检查「鉴权方式 / 额外请求头」；
        - 其它 4xx/5xx：仍报可达，但注明状态码并提示核对端点；
        - 网络异常：连接失败 + 友好提示。
        """
        error = self._validate_config()
        if error:
            return APIResult(ok=False, error=error)
        try:
            # 不用 _request（它会 raise_for_status）：这里要按状态码分类
            resp = self._http().get(self.auth_url(self.base_url))
        except Exception as exc:  # noqa: BLE001
            return APIResult(ok=False, error=self._friendly_error(f"连接失败: {exc}"))
        status = resp.status_code
        body = (resp.text or "")[:200]
        if 200 <= status < 400:
            return APIResult(ok=True, data=f"服务可达（HTTP {status}）", raw=body)
        if status in (401, 403):
            return APIResult(
                ok=False,
                error=(
                    f"服务可达但鉴权失败（HTTP {status}）。请检查高级项「鉴权方式」"
                    "（中转站常用 X-API-Key / api-key / URL 查询参数，而非 Bearer）、"
                    "「自定义鉴权头名/前缀」或「额外请求头」是否正确；"
                    "部分中转站根路径本来就要求鉴权，可改用「测试并检测字段…」直接测提交接口。"
                ),
                raw=body,
            )
        return APIResult(
            ok=True,
            data=(
                f"服务可达（HTTP {status}），但 Base URL 根路径不提供内容——"
                "多数中转站属正常现象；请核对「提交端点/轮询端点」，"
                "或用「测试并检测字段…」直接验证提交接口"
            ),
            raw=body,
        )
