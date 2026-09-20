"""接口探测对话框：预览将要发出的请求 → 发送一次测试请求 → 一键采用检测到的字段路径。

设计目标（针对中转站/聚合站的排查）：
- 「预览请求…」：把客户端组装好的请求（方法 / URL / 请求头 / JSON 请求体）原样展示，
  请求头与 URL 里的 API Key 已打码，可直接复制去比对站点文档；
- 「测试并检测字段…」：真的发一次**提交请求**（不轮询），把原始响应显示出来，
  并在返回结构里猜出任务ID / 状态 / 视频URL 的字段路径，点「使用」即写回设置表单。

同步执行但带忙碌光标（不引入线程）：探测只发一次请求，且已有 httpx 超时兜底，
保持实现简单、可测试。构造时不会联网，测试可注入任意 probe_fn。
"""
from __future__ import annotations

import json
from typing import Callable, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.api.base import APIResult
from ui.i18n import T, tr

# 检测结果分类 -> 对应设置字段（与 FIELD_DEFS 的 key 一致）
_FIELD_LABELS = {
    "job_id_path": "任务ID字段路径",
    "status_path": "状态字段路径",
    "result_video_url_path": "视频URL字段路径",
}

# 值预览的最大长度（表格里不换行）
_PREVIEW_LIMIT = 90


def _pretty_json(value) -> str:
    """尽量美化 JSON；失败则退回原文本。"""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ""
        try:
            return json.dumps(json.loads(text), ensure_ascii=False, indent=2)
        except (ValueError, TypeError):
            return value
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return str(value)


def _preview_text(value) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = text.replace("\n", " ")
    return text if len(text) <= _PREVIEW_LIMIT else text[: _PREVIEW_LIMIT - 1] + "…"


class ApiProbeDialog(QDialog):
    """请求预览 + 测试请求 + 字段路径建议。

    参数：
    - kind：API 类型（llm/image/video），用于标题展示；
    - preview：客户端 preview_request() 的返回，{"method","url","headers","body"}；
    - probe_fn：无参回调，返回 APIResult（点「发送测试请求」时同步调用）；
    - parent：父窗口。

    采用字段时发出 ``field_applied(field_key, path)`` 信号，同时若构造时传了
    ``on_apply`` 回调则一并调用（便于测试与外部直接接线）。
    """

    field_applied = Signal(str, str)

    def __init__(
        self,
        kind: str,
        preview: dict,
        probe_fn: Callable[[], APIResult],
        parent=None,
        on_apply: Optional[Callable[[str, str], None]] = None,
    ):
        super().__init__(parent)
        self._kind = kind
        self._preview = dict(preview or {})
        self._probe_fn = probe_fn
        self._on_apply = on_apply
        self._suggestion_rows: List[QPushButton] = []
        self._result: Optional[APIResult] = None

        self.setWindowTitle(tr("接口探测 — 预览请求 / 测试并检测字段"))
        self.setMinimumSize(720, 620)
        self._build_ui()
        self._fill_request_view()

    # ------------------------------------------------------------------ #
    # 界面
    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        # ---- 请求预览 ----
        root.addWidget(T(QLabel(), "将发送的请求（API Key 已打码，可复制）"))
        self._request_view = QTextEdit()
        self._request_view.setReadOnly(True)
        self._request_view.setObjectName("ProbeRequestView")
        self._request_view.setMinimumHeight(180)
        root.addWidget(self._request_view, 1)

        request_row = QHBoxLayout()
        request_row.addStretch(1)
        self._btn_copy = T(QPushButton(), "复制请求")
        self._btn_copy.clicked.connect(self._on_copy)
        request_row.addWidget(self._btn_copy)
        root.addLayout(request_row)

        # ---- 发送测试请求 ----
        probe_row = QHBoxLayout()
        self._btn_probe = T(QPushButton(), "发送测试请求")
        self._btn_probe.setObjectName("PrimaryButton")
        self._btn_probe.clicked.connect(self._on_probe)
        probe_row.addWidget(self._btn_probe)
        probe_row.addWidget(T(QLabel(), "只发一次提交请求、不轮询；失败也会把原始响应显示出来"))
        probe_row.addStretch(1)
        root.addLayout(probe_row)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setObjectName("HintLabel")
        root.addWidget(self._status)

        # ---- 响应 ----
        root.addWidget(T(QLabel(), "响应"))
        self._response_view = QTextEdit()
        self._response_view.setReadOnly(True)
        self._response_view.setObjectName("ProbeResponseView")
        self._response_view.setMinimumHeight(140)
        root.addWidget(self._response_view, 1)

        # ---- 检测到的字段（可滚动） ----
        self._suggest_title = T(QLabel(), "检测到的字段（点「使用」写入设置表单）")
        root.addWidget(self._suggest_title)
        self._suggest_area = QScrollArea()
        self._suggest_area.setWidgetResizable(True)
        self._suggest_area.setMinimumHeight(120)
        self._suggest_host = QWidget()
        self._suggest_layout = QVBoxLayout(self._suggest_host)
        self._suggest_layout.setContentsMargins(2, 2, 2, 2)
        self._suggest_layout.setSpacing(4)
        self._suggest_area.setWidget(self._suggest_host)
        root.addWidget(self._suggest_area, 1)
        self._suggest_hint = QLabel("")
        self._suggest_hint.setWordWrap(True)
        self._suggest_hint.setObjectName("HintLabel")
        root.addWidget(self._suggest_hint)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self._btn_close = T(QPushButton(), "关闭")
        self._btn_close.clicked.connect(self.accept)
        buttons.addWidget(self._btn_close)
        root.addLayout(buttons)

        self._set_suggestions({})
        self._suggest_hint.setText(tr("点「发送测试请求」后在此列出可用的字段路径"))

    # ------------------------------------------------------------------ #
    def _fill_request_view(self) -> None:
        method = str(self._preview.get("method") or "POST").upper()
        url = str(self._preview.get("url") or "")
        lines = [f"{method} {url}", ""]
        headers = self._preview.get("headers") or {}
        if headers:
            lines.append(tr("请求头:"))
            lines.extend(f"  {key}: {value}" for key, value in headers.items())
            lines.append("")
        body = self._preview.get("body")
        if body is not None:
            lines.append(tr("请求体:"))
            lines.append(_pretty_json(body))
        self._request_view.setPlainText("\n".join(lines))

    # ------------------------------------------------------------------ #
    # 交互
    # ------------------------------------------------------------------ #
    def _on_copy(self) -> None:
        QGuiApplication.clipboard().setText(self._request_view.toPlainText())
        self._status.setText(tr("已复制请求到剪贴板"))

    def _on_probe(self) -> None:
        """同步执行 probe_fn（忙碌光标 + 按钮禁用），失败不抛异常。"""
        if self._probe_fn is None:
            return
        self._btn_probe.setEnabled(False)
        self._status.setStyleSheet("")
        self._status.setText(tr("正在发送测试请求…"))
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            result = self._probe_fn()
        except Exception as exc:  # noqa: BLE001  兜底：探测永远不该把异常抛到界面
            result = APIResult(ok=False, error=str(exc))
        finally:
            QApplication.restoreOverrideCursor()
            self._btn_probe.setEnabled(True)
        self._result = result
        self._fill_response(result)
        self._set_suggestions((getattr(result, "data", None) or {}).get("suggestions") or {})

    # ------------------------------------------------------------------ #
    def _fill_response(self, result: APIResult) -> None:
        data = getattr(result, "data", None) or {}
        status_code = data.get("status_code")
        raw_text = data.get("raw_text")
        parsed = data.get("json")
        pieces = []
        if status_code is not None:
            pieces.append(tr("HTTP 状态码: {0}").format(status_code))
        body_text = _pretty_json(parsed) if parsed is not None else (raw_text or "")
        if not body_text and getattr(result, "raw", None) is not None:
            body_text = _pretty_json(result.raw)
        pieces.append(body_text or tr("（响应体为空）"))
        self._response_view.setPlainText("\n".join(pieces))
        if getattr(result, "ok", False):
            self._status.setStyleSheet("color: #22c55e;")
            self._status.setText(tr("✓ 请求成功（HTTP {0}），已尝试识别字段路径").format(status_code))
        else:
            self._status.setStyleSheet("color: #f25a5a;")
            self._status.setText(tr("✗ 请求失败: {0}").format(getattr(result, "error", "") or tr("未知错误")))

    def _set_suggestions(self, suggestions: dict) -> None:
        """按 {字段: [路径]} 重建建议行（每行：[路径] [值预览] [使用]）。"""
        self._suggestion_rows = []
        while self._suggest_layout.count():
            item = self._suggest_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        found = 0
        for field_key, paths in (suggestions or {}).items():
            if field_key not in _FIELD_LABELS:
                continue
            for path in paths or []:
                self._suggest_layout.addWidget(self._make_suggestion_row(field_key, str(path)))
                found += 1
        self._suggest_layout.addStretch(1)
        if found:
            self._suggest_hint.setText(
                tr("共检测到 {0} 条候选路径；点「使用」即写入对应的「{1}」字段")
                .format(found, tr("字段路径"))
            )
        else:
            self._suggest_hint.setText(
                tr("未检测到可用字段：可展开原始响应手动填写路径")
            )

    def _make_suggestion_row(self, field_key: str, path: str) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        label = tr(_FIELD_LABELS.get(field_key, field_key))
        name = QLabel(f"[{label}]")
        name.setMinimumWidth(150)
        layout.addWidget(name)
        path_label = QLabel(path)
        path_label.setStyleSheet("font-family: Consolas, monospace;")
        layout.addWidget(path_label)
        preview = QLabel(self._preview_of(path))
        preview.setStyleSheet("color: #8a8f98;")
        layout.addWidget(preview)
        layout.addStretch(1)
        button = QPushButton(tr("使用"))
        button.setProperty("field_key", field_key)
        button.setProperty("path", path)
        button.clicked.connect(lambda _checked=False, b=button: self._apply_button(b))
        layout.addWidget(button)
        self._suggestion_rows.append(button)
        return row

    def _preview_of(self, path: str) -> str:
        """用响应 JSON 显示该路径当前取到的值（供用户判断是否选对）。"""
        data = (getattr(self._result, "data", None) or {}) if self._result else {}
        parsed = data.get("json")
        if parsed is None:
            return ""
        value = _dig(parsed, path)
        if value is None:
            return tr("（当前响应里取不到该路径）")
        return _preview_text(value)

    def _apply_button(self, button: QPushButton) -> None:
        field_key = str(button.property("field_key") or "")
        path = str(button.property("path") or "")
        if not field_key or not path:
            return
        self.field_applied.emit(field_key, path)
        if self._on_apply is not None:
            self._on_apply(field_key, path)
        self._status.setStyleSheet("color: #22c55e;")
        self._status.setText(tr("已写入：{0} = {1}（点「保存配置」后生效）").format(tr(_FIELD_LABELS.get(field_key, field_key)), path))

    # ------------------------------------------------------------------ #
    def suggestions(self) -> dict:
        """当前展示的建议（测试用）。"""
        return (getattr(self._result, "data", None) or {}).get("suggestions") or {}

    def suggestion_buttons(self) -> List[QPushButton]:
        """建议行里的「使用」按钮（测试用）。"""
        return list(self._suggestion_rows)


def _dig(obj, path: str):
    """本地最小实现：按 'a.b.0.c' 取值（与 BaseAPI._dig 行为一致）。"""
    cur = obj
    for part in str(path).split("."):
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
            found = None
            for item in cur:
                if isinstance(item, dict) and part in item:
                    found = item[part]
                    break
            cur = found
        else:
            return None
    return cur
