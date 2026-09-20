"""端点适配对话框：展示「一键适配端点」的探测结果，一键写回提交/轮询端点与服务商适配。

设计要点：
- 构造时**不联网**：探测由调用方（设置页的「一键适配端点…」按钮）完成，把
  :class:`~core.api.endpoint_probe.EndpointProbeReport` 交进来即可——便于单测，
  也便于用同一份报告重开对话框；
- 默认的 GET 探测是无害的；POST 探测会向站点真实提交一次请求（宽松的中转站
  可能真的创建任务并计费），因此做成对话框里的一次性按钮：只有用户明确点击
  ``post_probe_fn`` 才会执行；
- 选中的端点通过 :meth:`EndpointAdaptDialog.selection` 交回
  ``{submit_url, poll_url, provider}``，由设置页写进表单（点「保存配置」才落盘）。
"""
from __future__ import annotations

import logging
from typing import Callable, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from core.api.endpoint_probe import (
    EndpointProbeReport,
    EndpointRow,
    ProbeOutcome,
    guess_poll_url_template,
    provider_for_path,
)
from ui.i18n import T, tr

logger = logging.getLogger("PixelFoundry.ui.endpoint_adapt_dialog")

# 各列宽度（像素）：路径 / GET / POST
_PATH_WIDTH = 260
_STATUS_WIDTH = 96


def _outcome_text(status: Optional[int], outcome: str, probed: bool) -> str:
    """把「状态码 + 结论」渲染成表格单元格文本。"""
    if not probed:
        return tr("未探测")
    if status is None:
        return tr(ProbeOutcome.NO_RESPONSE)
    label = tr(ProbeOutcome.LABELS.get(outcome, ProbeOutcome.LABELS[ProbeOutcome.UNKNOWN]))
    return f"{status} {label}"


class EndpointAdaptDialog(QDialog):
    """探测结果表：推荐行高亮，逐行「使用这个端点」。

    参数：
    - report：:func:`core.api.endpoint_probe.probe_video_endpoints` 的返回值；
    - parent：父窗口；
    - post_probe_fn：可选的无参回调，返回一份 ``allow_post=True`` 的新报告
      （由设置页接线）。为空时不显示 POST 探测按钮。
    """

    def __init__(
        self,
        report: EndpointProbeReport,
        parent=None,
        post_probe_fn: Optional[Callable[[], EndpointProbeReport]] = None,
    ):
        super().__init__(parent)
        self._report = report
        self._post_probe_fn = post_probe_fn
        self._choice: Optional[EndpointRow] = None
        self._row_buttons: List[QPushButton] = []
        self._row_hosts: List[QWidget] = []
        self.setWindowTitle(tr("一键适配端点 — 探测结果"))
        self.setMinimumSize(780, 560)
        self._build_ui()
        self._fill_rows()

    # ------------------------------------------------------------------ #
    # 界面
    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        self._summary = QLabel("")
        self._summary.setWordWrap(True)
        self._summary.setObjectName("HintLabel")
        root.addWidget(self._summary)

        self._base_label = QLabel("")
        self._base_label.setWordWrap(True)
        root.addWidget(self._base_label)

        self._mode_hint = QLabel("")
        self._mode_hint.setWordWrap(True)
        self._mode_hint.setObjectName("HintLabel")
        root.addWidget(self._mode_hint)

        # 表头：端点路径 / GET / POST / 说明
        header = QHBoxLayout()
        header.setContentsMargins(6, 0, 6, 0)
        head_path = T(QLabel(), "端点路径")
        head_path.setMinimumWidth(_PATH_WIDTH)
        head_path.setStyleSheet("font-weight: 600;")
        header.addWidget(head_path)
        head_get = T(QLabel(), "GET")
        head_get.setMinimumWidth(_STATUS_WIDTH)
        head_get.setStyleSheet("font-weight: 600;")
        header.addWidget(head_get)
        head_post = T(QLabel(), "POST")
        head_post.setMinimumWidth(_STATUS_WIDTH)
        head_post.setStyleSheet("font-weight: 600;")
        header.addWidget(head_post)
        header.addWidget(T(QLabel(), "说明"), 1)
        root.addLayout(header)

        self._area = QScrollArea()
        self._area.setWidgetResizable(True)
        self._host = QWidget()
        self._rows_layout = QVBoxLayout(self._host)
        self._rows_layout.setContentsMargins(2, 2, 2, 2)
        self._rows_layout.setSpacing(4)
        self._area.setWidget(self._host)
        root.addWidget(self._area, 1)

        # POST 复探（会真实提交请求，故选做显式按钮 + 警告）
        post_row = QHBoxLayout()
        self._btn_post = T(QPushButton(), "用 POST 再探测一次…")
        self._btn_post.clicked.connect(self._on_post_probe)
        self._btn_post.setVisible(self._post_probe_fn is not None)
        post_row.addWidget(self._btn_post)
        self._post_warn = T(
            QLabel(),
            "POST 探测会在站点上真实提交一次请求：宽松的中转站可能真的创建任务并计费，请自行确认",
        )
        self._post_warn.setWordWrap(True)
        self._post_warn.setObjectName("HintLabel")
        self._post_warn.setVisible(self._post_probe_fn is not None)
        post_row.addWidget(self._post_warn, 1)
        root.addLayout(post_row)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self._btn_copy = T(QPushButton(), "复制诊断信息")
        self._btn_copy.clicked.connect(self._on_copy)
        buttons.addWidget(self._btn_copy)
        self._btn_close = T(QPushButton(), "关闭")
        self._btn_close.clicked.connect(self.reject)
        buttons.addWidget(self._btn_close)
        root.addLayout(buttons)

    def _fill_rows(self) -> None:
        """按报告重建表格（含重新探测后的刷新）。"""
        self._row_buttons = []
        self._row_hosts = []
        while self._rows_layout.count():
            item = self._rows_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        report = self._report
        self._summary.setText(report.localized_summary(tr) or tr("没有探测到可用端点"))
        self._base_label.setText(tr("Base URL：{0}").format(report.base_url or tr("（未填写）")))
        if report.best is not None:
            self._summary.setStyleSheet("color: #22c55e; font-weight: 600;")
            self._mode_hint.setText(
                tr("推荐端点：{0}（提交端点 {1}，轮询端点 {2}）").format(
                    report.best.path,
                    report.submit_url_template,
                    report.poll_url_template or tr("未推断"),
                )
            )
        else:
            self._summary.setStyleSheet("color: #f25a5a;")
            self._mode_hint.setText(
                tr("没有探测到可用端点：请确认 Base URL 是否正确，或改用「从 curl 导入…」粘贴服务商文档里的示例")
            )

        best = report.best
        for row in report.rows:
            self._rows_layout.addWidget(self._make_row(row, recommended=row is best))
        self._rows_layout.addStretch(1)

    def _make_row(self, row: EndpointRow, recommended: bool) -> QWidget:
        host = QWidget()
        layout = QHBoxLayout(host)
        layout.setContentsMargins(6, 4, 6, 4)
        if recommended:
            host.setStyleSheet("background-color: rgba(34, 197, 94, 0.14); border-radius: 4px;")
        name = QLabel(row.path)
        name.setMinimumWidth(_PATH_WIDTH)
        name.setStyleSheet("font-family: Consolas, monospace; font-weight: 600;" if recommended
                           else "font-family: Consolas, monospace;")
        layout.addWidget(name)
        if recommended:
            layout.addWidget(T(QLabel(), "推荐"))
        get_label = QLabel(_outcome_text(row.get_status, row.get_outcome, probed=True))
        get_label.setMinimumWidth(_STATUS_WIDTH)
        layout.addWidget(get_label)
        post_probed = bool(row.post_outcome)
        post_label = QLabel(_outcome_text(row.post_status, row.post_outcome, probed=post_probed))
        post_label.setMinimumWidth(_STATUS_WIDTH)
        layout.addWidget(post_label)
        note = QLabel(row.localized_note(tr))
        note.setWordWrap(True)
        note.setStyleSheet("color: #8a8f98;")
        layout.addWidget(note, 1)
        button = T(QPushButton(), "使用这个端点")
        button.setProperty("path", row.path)
        button.clicked.connect(lambda _checked=False, r=row: self._on_use(r))
        layout.addWidget(button)
        self._row_buttons.append(button)
        self._row_hosts.append(host)
        return host

    # ------------------------------------------------------------------ #
    # 交互
    # ------------------------------------------------------------------ #
    def _on_use(self, row: EndpointRow) -> None:
        self._choice = row
        self.accept()

    def _on_post_probe(self) -> None:
        """用 POST 重新探测一次（真实提交请求，只有用户点击才会走这里）。"""
        if self._post_probe_fn is None:
            return
        self._btn_post.setEnabled(False)
        self._mode_hint.setText(tr("正在用 POST 重新探测…"))
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            report = self._post_probe_fn()
        except Exception as exc:  # noqa: BLE001  探测不应把异常抛到界面
            QMessageBox.warning(self, tr("提示"), tr("探测失败: {0}").format(exc))
            report = None
        finally:
            QApplication.restoreOverrideCursor()
            self._btn_post.setEnabled(True)
        if isinstance(report, EndpointProbeReport):
            self._report = report
            self._choice = None
            self._fill_rows()

    def _on_copy(self) -> None:
        QGuiApplication.clipboard().setText(self.diagnostic_text())
        self._mode_hint.setText(tr("已复制诊断信息到剪贴板"))

    # ------------------------------------------------------------------ #
    # 供设置页 / 测试使用
    # ------------------------------------------------------------------ #
    def choice(self) -> Optional[EndpointRow]:
        """用户选择的端点行（未选择时为空）。"""
        return self._choice

    def selection(self) -> Dict[str, str]:
        """所选端点对应的三项配置（写进设置表单）。"""
        row = self._choice
        if row is None:
            return {}
        submit = f"{{base}}{row.path}"
        return {
            "submit_url": submit,
            "poll_url": guess_poll_url_template(submit),
            "provider": provider_for_path(row.path),
        }

    def report(self) -> EndpointProbeReport:
        return self._report

    def row_buttons(self) -> List[QPushButton]:
        """每行的「使用这个端点」按钮（测试用）。"""
        return list(self._row_buttons)

    def diagnostic_text(self) -> str:
        """可复制的诊断信息（Base URL + 逐行状态 + 说明）。"""
        lines = [tr("一键适配端点 — 探测结果"), tr("Base URL：{0}").format(self._report.base_url or "")]
        lines.append(
            f"{tr('端点路径')} | GET | POST | {tr('说明')}"
        )
        for row in self._report.rows:
            lines.append(
                "{} | {} | {} | {}".format(
                    row.path,
                    _outcome_text(row.get_status, row.get_outcome, probed=True),
                    _outcome_text(row.post_status, row.post_outcome, probed=bool(row.post_outcome)),
                    row.localized_note(tr),
                )
            )
        lines.append(self._report.localized_summary(tr))
        return "\n".join(lines)
