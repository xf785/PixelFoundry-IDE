"""API 配置控件：一套 API 类型的配置增删改查 + 连接测试。

每种 API 类型的字段由 config.api_config.FIELD_DEFS 定义，
控件按字段类型自动生成表单控件（文本/密码/整数/浮点/布尔）。

针对中转站/聚合站（relay）的辅助入口（不依赖网络即可构建界面）：
- 视频：「预览请求…」「测试并检测字段…」——先看将发出的请求，再真发一次提交请求，
  把响应里猜出的任务ID/状态/视频URL 字段路径一键写回表单；
- 三类 API：「从 curl 导入…」——粘贴浏览器 F12 里 Copy as cURL 的命令，
  自动填好 Base URL / 提交端点 / 请求方法 / 额外请求头 / 请求体模板。
"""
from __future__ import annotations

import io
import logging
from typing import Dict, Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from config.api_config import APIConfig, FIELD_DEFS, PROVIDER_PRESETS
from config.settings import API_KIND_LABELS
from core.api.curl_import import parse_curl, to_params
from core.api.factory import is_mock_config
from ui.i18n import T, tr
from ui.workers import FunctionWorker

logger = logging.getLogger("PixelFoundry.ui.api_config_widget")

# 「测试并检测字段」时用的合成首帧尺寸（API 最低要求通常是 256 长边）
_TEST_FRAME_SIDE = 256


class ModelPickerDialog(QDialog):
    """展示可用模型列表，支持关键字过滤，选中后返回模型 ID。"""

    def __init__(self, models: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("选择模型"))
        self.setMinimumSize(480, 540)
        self.selected: str | None = None
        self._models = list(models)

        layout = QVBoxLayout(self)
        self._filter = QLineEdit()
        self._filter.setPlaceholderText(tr("输入关键词过滤（如 seedance / kling / image）…"))
        self._filter.textChanged.connect(self._apply_filter)
        layout.addWidget(self._filter)

        self._list = QListWidget()
        self._list.addItems(self._models)
        self._list.itemDoubleClicked.connect(self._accept_item)
        layout.addWidget(self._list, 1)

        self._hint = QLabel("")
        self._hint.setObjectName("HintLabel")
        layout.addWidget(self._hint)
        self._update_hint(len(self._models))

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(tr("使用该模型"))
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _apply_filter(self, text: str) -> None:
        text = text.strip().lower()
        filtered = [m for m in self._models if text in m.lower()] if text else self._models
        self._list.clear()
        self._list.addItems(filtered)
        self._update_hint(len(filtered))

    def _update_hint(self, count: int) -> None:
        self._hint.setText(tr("共 {0} 个模型；双击或选中后点「使用该模型」").format(count))

    def _accept_item(self, item) -> None:
        self.selected = item.text()
        self.accept()

    def _on_ok(self) -> None:
        item = self._list.currentItem()
        if item is not None:
            self.selected = item.text()
            self.accept()


class CurlImportDialog(QDialog):
    """粘贴 cURL 命令 → 解析出端点/方法/请求头/请求体模板。

    只负责收集文本，解析交给 core.api.curl_import.parse_curl（纯函数，便于测试）。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("从 curl 导入"))
        self.setMinimumSize(640, 420)
        layout = QVBoxLayout(self)
        hint = T(
            QLabel(),
            "在浏览器开发者工具（F12 → 网络）里右键任意请求 → 复制 → 以 cURL 格式复制，"
            "粘贴到下面即可自动填好 Base URL / 提交端点 / 请求方法 / 额外请求头 / 请求体模板。",
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self._text = QPlainTextEdit()
        self._text.setPlaceholderText(
            tr("curl 'https://relay.example.com/v1/videos/generations' -H 'x-api-key: sk-…' --data-raw '{…}'")
        )
        layout.addWidget(self._text, 1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(tr("解析并填入"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def text(self) -> str:
        return self._text.toPlainText()

    def set_text(self, text: str) -> None:
        self._text.setPlainText(text)


class ApiConfigWidget(QWidget):
    """管理一种 API 类型（llm/image/video）的多套配置。"""

    configs_changed = Signal()

    def __init__(self, api_manager, kind: str, parent=None):
        super().__init__(parent)
        self._api = api_manager
        self.kind = kind
        self._fields: Dict[str, QWidget] = {}
        self._current_id: Optional[str] = None
        self._test_worker: Optional[FunctionWorker] = None
        self._build_ui()
        self.refresh()

    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        # 标题 + 配置选择
        title_row = QHBoxLayout()
        title = QLabel(tr(API_KIND_LABELS.get(self.kind, self.kind)))
        title.setStyleSheet("font-weight: 600; font-size: 14px;")
        title_row.addWidget(title)
        title_row.addStretch(1)
        root.addLayout(title_row)

        # 服务商预设（一键填充 Base URL / 模型 / 适配参数）
        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel(tr("服务商预设")))
        self._preset_combo = QComboBox()
        self._preset_combo.addItem(tr("（自定义）"), userData="")
        for preset in PROVIDER_PRESETS.get(self.kind, []):
            self._preset_combo.addItem(tr(preset["label"]), userData=preset["key"])
        self._preset_combo.currentIndexChanged.connect(self._on_preset_selected)
        preset_row.addWidget(self._preset_combo, 1)
        root.addLayout(preset_row)

        # 中转站辅助：预览请求 / 测试并检测字段 / 从 curl 导入（不联网即可构建）
        tools_row = QHBoxLayout()
        tools_row.addWidget(T(QLabel(), "中转站辅助"))
        self._btn_preview = T(QPushButton(), "预览请求…")
        self._btn_preview.setToolTip(tr("只组装不发送：查看将发出的方法/URL/请求头/请求体（Key 已打码）"))
        self._btn_preview.clicked.connect(self._on_preview_request)
        tools_row.addWidget(self._btn_preview)
        self._btn_probe = T(QPushButton(), "测试并检测字段…")
        self._btn_probe.setToolTip(tr("真发一次提交请求（不轮询），并自动识别任务ID/状态/视频URL 的字段路径"))
        self._btn_probe.clicked.connect(self._on_probe_request)
        tools_row.addWidget(self._btn_probe)
        self._btn_curl = T(QPushButton(), "从 curl 导入…")
        self._btn_curl.setToolTip(tr("粘贴浏览器「Copy as cURL」的命令，自动填端点、请求头与请求体模板"))
        self._btn_curl.clicked.connect(self._on_import_curl)
        tools_row.addWidget(self._btn_curl)
        tools_row.addStretch(1)
        self._relay_buttons = [self._btn_preview, self._btn_probe, self._btn_curl]
        if self.kind != "video":
            # 预览/探测目前只对「提交+轮询」型视频接口有意义
            self._btn_preview.hide()
            self._btn_probe.hide()
        root.addLayout(tools_row)

        row = QHBoxLayout()
        self._combo = QComboBox()
        self._combo.currentIndexChanged.connect(self._on_select)
        row.addWidget(self._combo, 1)
        self._btn_new = T(QPushButton(), "新建")
        self._btn_new.clicked.connect(self._on_new)
        row.addWidget(self._btn_new)
        self._btn_delete = T(QPushButton(), "删除")
        self._btn_delete.setObjectName("DangerButton")
        self._btn_delete.clicked.connect(self._on_delete)
        row.addWidget(self._btn_delete)
        root.addLayout(row)

        # 表单：基础字段 + 可折叠「高级选项」
        self._form = QFormLayout()
        self._form.setContentsMargins(4, 4, 4, 4)
        self._form.setVerticalSpacing(8)

        self._adv_box = QGroupBox(tr("高级选项"))
        self._adv_box.setCheckable(True)
        self._adv_box.setChecked(False)
        adv_layout = QVBoxLayout(self._adv_box)
        adv_layout.setContentsMargins(10, 14, 10, 10)
        self._adv_container = QWidget()
        self._adv_form = QFormLayout(self._adv_container)
        self._adv_form.setContentsMargins(0, 0, 0, 0)
        self._adv_form.setVerticalSpacing(8)
        adv_layout.addWidget(self._adv_container)
        self._adv_box.toggled.connect(self._on_adv_toggled)

        for field in FIELD_DEFS.get(self.kind, []):
            key, ftype = field["key"], field["type"]
            widget = self._make_field_widget(field)
            self._fields[key] = widget
            target = self._adv_form if field.get("group") == "advanced" else self._form
            if ftype == "bool":
                target.addRow(widget)  # 占整行
            else:
                target.addRow(tr(field["label"]), widget)

        root.addLayout(self._form)
        root.addWidget(self._adv_box)
        self._on_adv_toggled(False)  # 默认收起高级选项

        # 操作按钮
        actions = QHBoxLayout()
        self._btn_save = T(QPushButton(), "保存配置")
        self._btn_save.setObjectName("PrimaryButton")
        self._btn_save.clicked.connect(self._on_save)
        actions.addWidget(self._btn_save)
        self._btn_default = T(QPushButton(), "设为默认")
        self._btn_default.setCheckable(True)
        self._btn_default.clicked.connect(self._on_toggle_default)
        actions.addWidget(self._btn_default)
        actions.addStretch(1)
        self._btn_test = T(QPushButton(), "测试连接")
        self._btn_test.clicked.connect(self._on_test)
        actions.addWidget(self._btn_test)
        self._btn_models = T(QPushButton(), "查询模型")
        self._btn_models.clicked.connect(self._on_list_models)
        actions.addWidget(self._btn_models)
        root.addLayout(actions)

        self._test_result = QLabel("")
        self._test_result.setWordWrap(True)
        self._test_result.setObjectName("HintLabel")
        root.addWidget(self._test_result)

    def _on_adv_toggled(self, checked: bool) -> None:
        """高级选项折叠/展开。"""
        self._adv_container.setVisible(checked)
        self._adv_box.setMaximumHeight(16777215 if checked else 48)
        self._adv_box.setTitle(tr("高级选项（收起）") if checked else tr("高级选项（展开）"))

    def _make_field_widget(self, field: dict) -> QWidget:
        ftype = field["type"]
        if ftype == "bool":
            w = T(QCheckBox(), field["label"])
        elif ftype == "choice":
            w = QComboBox()
            for value, label in field.get("options", []):
                w.addItem(tr(label), userData=value)
        elif ftype == "int":
            w = QSpinBox()
            w.setRange(int(field.get("min", -10_000_000)), int(field.get("max", 10_000_000)))
        elif ftype == "float":
            w = QDoubleSpinBox()
            w.setRange(float(field.get("min", -1e9)), float(field.get("max", 1e9)))
            w.setDecimals(2)
            w.setSingleStep(0.1)
        elif ftype == "textarea":
            # 多行文本（JSON 请求体模板 / 额外请求头等）
            w = QPlainTextEdit()
            w.setPlainText(str(field.get("default", "") or ""))
            w.setPlaceholderText(tr(field.get("placeholder") or ""))
            w.setFixedHeight(max(56, min(96, w.fontMetrics().lineSpacing() * 4 + 14)))
            w.setTabChangesFocus(True)
        else:
            w = QLineEdit()
            if ftype == "password":
                w.setEchoMode(QLineEdit.EchoMode.Password)
            w.setPlaceholderText(tr(field.get("placeholder") or str(field.get("default", ""))))
        return w

    # ------------------------------------------------------------------ #
    # 数据加载
    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        """重新从管理器加载配置列表。"""
        current = self._current_id
        self._combo.blockSignals(True)
        self._combo.clear()
        for cfg in self._api.list(self.kind):
            label = cfg.name + (" ★" if cfg.is_default else "")
            self._combo.addItem(label, userData=cfg.id)
        self._combo.blockSignals(False)
        # 预设下拉回到「（自定义）」，避免切换配置时误触发
        self._preset_combo.blockSignals(True)
        self._preset_combo.setCurrentIndex(0)
        self._preset_combo.blockSignals(False)

        if self._combo.count() == 0:
            self._current_id = None
            self._set_form_enabled(False)
            self._test_result.setText(tr("暂无配置，点击「新建」创建"))
            return

        # 恢复选中项
        index = 0
        if current:
            for i in range(self._combo.count()):
                if self._combo.itemData(i) == current:
                    index = i
                    break
        self._combo.setCurrentIndex(index)
        self._load_config(self._api.get(self._combo.itemData(index)))

    def _load_config(self, cfg: APIConfig) -> None:
        if cfg is None:
            return
        self._current_id = cfg.id
        self._set_form_enabled(True)
        self._set_field("base_url", cfg.base_url)
        self._set_field("api_key", cfg.api_key)
        self._set_field("model", cfg.model)
        for key, widget in self._fields.items():
            if key in ("base_url", "api_key", "model"):
                continue
            self._set_field(key, cfg.params.get(key, self._default_for(key)))
        self._btn_default.setChecked(cfg.is_default)
        self._test_result.setText("")

    def _collect_config(self) -> APIConfig:
        cfg = APIConfig(
            kind=self.kind,
            name=self._current_name(),
            base_url=self._get_field("base_url") or "",
            api_key=self._get_field("api_key") or "",
            model=self._get_field("model") or "",
            params={},
        )
        if self._current_id:
            cfg.id = self._current_id
        for key, widget in self._fields.items():
            if key in ("base_url", "api_key", "model"):
                continue
            cfg.params[key] = self._get_field(key)
        return cfg

    def _current_name(self) -> str:
        base = self._get_field("model") or tr("{0} 配置").format(self.kind)
        return base[:24]

    # ------------------------------------------------------------------ #
    # 事件
    # ------------------------------------------------------------------ #
    def _on_select(self) -> None:
        cfg_id = self._combo.currentData()
        if cfg_id:
            self._load_config(self._api.get(cfg_id))

    def _on_new(self) -> None:
        cfg = APIConfig.defaults(self.kind)
        self._api.add(cfg)
        self.refresh()
        # 选中新配置
        for i in range(self._combo.count()):
            if self._combo.itemData(i) == cfg.id:
                self._combo.setCurrentIndex(i)
                break
        self.configs_changed.emit()

    def _on_delete(self) -> None:
        cfg_id = self._combo.currentData()
        if not cfg_id:
            return
        ret = QMessageBox.question(self, tr("删除配置"), tr("确定删除当前配置？"))
        if ret != QMessageBox.StandardButton.Yes:
            return
        self._api.delete(cfg_id)
        self.refresh()
        self.configs_changed.emit()

    def _on_save(self) -> None:
        cfg = self._collect_config()
        if not cfg.base_url:
            QMessageBox.warning(self, tr("提示"), tr("Base URL 不能为空"))
            return
        try:
            if self._api.get(cfg.id):
                self._api.update(cfg)
            else:
                self._api.add(cfg)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, tr("保存失败"), str(exc))
            return
        self._test_result.setText(tr("已保存"))
        self.refresh()
        self.configs_changed.emit()

    def _on_toggle_default(self, checked: bool) -> None:
        cfg_id = self._combo.currentData()
        if not cfg_id:
            return
        if checked:
            try:
                self._api.set_default(cfg_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(tr("设为默认失败: {0}").format(exc))
        self.refresh()
        self.configs_changed.emit()

    def _on_test(self) -> None:
        cfg = self._collect_config()
        if not cfg.base_url:
            QMessageBox.warning(self, tr("提示"), tr("请先填写 Base URL 并保存"))
            return
        self._btn_test.setEnabled(False)
        self._test_result.setText(tr("测试中…"))
        self._test_worker = FunctionWorker(self._api.test_connection, cfg)
        self._test_worker.succeeded.connect(self._on_test_done)
        self._test_worker.failed.connect(lambda msg: self._on_test_done(APIResultShim(False, msg)))
        self._test_worker.start()

    def _on_test_done(self, result) -> None:
        self._btn_test.setEnabled(True)
        if getattr(result, "ok", False):
            self._test_result.setStyleSheet("color: #22c55e;")
            self._test_result.setText(tr("✓ {0}（{1}）").format(result.message, getattr(result, 'data', '')))
        else:
            self._test_result.setStyleSheet("color: #f25a5a;")
            self._test_result.setText(tr("✗ {0}").format(getattr(result, 'message', result)))

    # ------------------------------------------------------------------ #
    # 服务商预设
    # ------------------------------------------------------------------ #
    def _on_preset_selected(self) -> None:
        key = self._preset_combo.currentData()
        if not key:
            return
        preset = next((p for p in PROVIDER_PRESETS.get(self.kind, []) if p["key"] == key), None)
        if preset is None:
            return
        self._set_field("base_url", preset.get("base_url", ""))
        self._set_field("model", preset.get("model", ""))
        if "endpoint" in preset:
            self._set_field("endpoint", preset.get("endpoint", ""))
        for pkey, pval in (preset.get("params") or {}).items():
            if pkey in self._fields:
                self._set_field(pkey, pval)
        self._test_result.setStyleSheet("")
        if self._get_field("api_key"):
            self._test_result.setText(tr("已应用预设「{0}」，正在查询可用模型…").format(tr(preset["label"])))
            self._on_list_models()
        else:
            self._test_result.setText(
                tr("已应用预设「{0}」；填写 API Key 后可点「查询模型」一键选择").format(tr(preset["label"]))
            )

    # ------------------------------------------------------------------ #
    # 查询可用模型
    # ------------------------------------------------------------------ #
    def _on_list_models(self) -> None:
        cfg = self._collect_config()
        if not cfg.base_url:
            QMessageBox.warning(self, tr("提示"), tr("请先填写 Base URL"))
            return
        if is_mock_config(cfg):
            QMessageBox.information(self, tr("提示"), tr("模拟 API 无需查询模型"))
            return
        self._btn_models.setEnabled(False)
        self._test_result.setText(tr("正在查询可用模型…"))
        self._models_worker = FunctionWorker(self._list_models_sync, cfg)
        self._models_worker.succeeded.connect(self._on_models_done)
        self._models_worker.failed.connect(lambda msg: self._on_models_done(APIResultShim(False, msg)))
        self._models_worker.start()

    def _list_models_sync(self, cfg: APIConfig):
        from core.api.factory import create_api_client

        client = create_api_client(cfg.kind, cfg)
        try:
            return client.list_models()
        finally:
            client.close()

    def _on_models_done(self, result) -> None:
        self._btn_models.setEnabled(True)
        if not getattr(result, "ok", False):
            self._test_result.setStyleSheet("color: #f25a5a;")
            self._test_result.setText(tr("✗ 查询失败: {0}").format(getattr(result, 'message', result)))
            return
        models = result.data or []
        if not models:
            self._test_result.setText(tr("接口可用，但未返回模型列表"))
            return
        dialog = ModelPickerDialog(models, self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.selected:
            self._set_field("model", dialog.selected)
            self._test_result.setStyleSheet("color: #22c55e;")
            self._test_result.setText(tr("已选择模型: {0}").format(dialog.selected))

    # ------------------------------------------------------------------ #
    # 中转站辅助：预览请求 / 测试并检测字段 / 从 curl 导入
    # ------------------------------------------------------------------ #
    @staticmethod
    def _test_frame_png(side: int = _TEST_FRAME_SIDE) -> bytes:
        """合成一张测试首帧 PNG（灰底 + 彩色方块/斜线，便于肉眼确认结果）。

        真正的 3D 渲染没必要：探测只关心服务端是否接受这张图。
        """
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (side, side), (48, 52, 64))
        draw = ImageDraw.Draw(image)
        margin = max(4, side // 8)
        draw.rectangle(
            [margin, margin, side - margin, side - margin],
            outline=(220, 226, 236),
            width=max(1, side // 64),
        )
        third = side // 3
        draw.rectangle([third, third, third * 2, third * 2], fill=(226, 88, 74))
        draw.line([0, side - 1, side - 1, 0], fill=(120, 190, 255), width=max(1, side // 64))
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        return buf.getvalue()

    def _make_client_or_warn(self, need_base_url: bool = True):
        """按当前表单构建客户端；条件不满足时提示并返回 None。"""
        cfg = self._collect_config()
        if need_base_url and not cfg.base_url:
            QMessageBox.warning(self, tr("提示"), tr("请先填写 Base URL"))
            return None
        if is_mock_config(cfg):
            QMessageBox.information(self, tr("提示"), tr("模拟 API 无需预览/探测请求"))
            return None
        from core.api.factory import create_api_client

        return create_api_client(self.kind, cfg)

    def _build_preview_frame(self, client):
        """组装请求预览 + 合成测试帧，返回 (preview, frame)。"""
        frame = self._test_frame_png()
        preview = client.preview_request(frame, tr("a small red cube rotating"), frames=8, fps=8)
        return preview, frame

    def _on_preview_request(self) -> None:
        """「预览请求…」：只组装不发送，把请求原样展示出来（Key 已打码）。"""
        client = self._make_client_or_warn()
        if client is None:
            return
        try:
            preview, _frame = self._build_preview_frame(client)
        except Exception as exc:  # noqa: BLE001
            logger.exception("组装请求预览失败")
            QMessageBox.critical(
                self, tr("预览失败"), tr("无法组装请求: {0}").format(exc)
            )
            client.close()
            return
        self._open_probe_dialog(client, preview, probe=False)

    def _on_probe_request(self) -> None:
        """「测试并检测字段…」：真发一次提交请求，并允许一键采用检测到的字段路径。"""
        client = self._make_client_or_warn()
        if client is None:
            return
        if not self._get_field("api_key") and not self._extra_headers_present():
            answer = QMessageBox.question(
                self,
                tr("提示"),
                tr("还没有填写 API Key，测试请求很可能返回 401/403。仍要继续吗？"),
            )
            if answer != QMessageBox.StandardButton.Yes:
                client.close()
                return
        try:
            preview, frame = self._build_preview_frame(client)
        except Exception as exc:  # noqa: BLE001
            logger.exception("组装请求预览失败")
            QMessageBox.critical(self, tr("预览失败"), tr("无法组装请求: {0}").format(exc))
            client.close()
            return
        dialog = self._open_probe_dialog(client, preview, probe=True, frame=frame)
        if dialog is not None and dialog.result() == QDialog.DialogCode.Accepted:
            client.close()

    def _extra_headers_present(self) -> bool:
        """额外请求头里是否写了内容（有些站点把 Key 放在额外请求头里）。"""
        return bool(str(self._get_field("extra_headers") or "").strip())

    def _open_probe_dialog(self, client, preview: dict, probe: bool, frame: Optional[bytes] = None):
        """打开探测对话框；probe=True 时接线「使用」→ 写回表单字段。"""
        from ui.dialogs.api_probe_dialog import ApiProbeDialog

        def run_probe():
            return client.probe(frame or self._test_frame_png(), tr("a small red cube rotating"), frames=8, fps=8)

        dialog = ApiProbeDialog(
            self.kind,
            preview,
            run_probe if probe else (lambda: None),
            parent=self,
            on_apply=lambda field, path: self._apply_detected_field(client, field, path),
        )
        dialog.exec()
        return dialog

    def _apply_detected_field(self, client, field: str, path: str) -> None:
        """把检测到的字段路径写回对应表单字段，并同步到客户端以便继续预览。"""
        widget = self._fields.get(field)
        if widget is None:
            QMessageBox.information(self, tr("提示"), tr("当前 API 类型没有「{0}」字段").format(field))
            return
        if isinstance(widget, QPlainTextEdit):
            widget.setPlainText(path)
        elif isinstance(widget, QLineEdit):
            widget.setText(path)
        else:  # 其它控件类型（理论上不会走到）
            self._set_field(field, path)
        params = dict(getattr(client, "params", {}) or {})
        params[field] = path
        client.params = params
        self._test_result.setStyleSheet("color: #22c55e;")
        self._test_result.setText(tr("已写入「{0}」= {1}（点「保存配置」后生效）").format(field, path))

    def _on_import_curl(self) -> None:
        """「从 curl 导入…」：解析 cURL 命令并填入端点/请求头/请求体模板。"""
        dialog = CurlImportDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        text = dialog.text().strip()
        if not text:
            QMessageBox.warning(self, tr("提示"), tr("请先粘贴 curl 命令"))
            return
        try:
            imported = parse_curl(text)
            params = to_params(imported)
        except Exception as exc:  # noqa: BLE001
            logger.exception("解析 curl 失败")
            QMessageBox.critical(self, tr("导入失败"), tr("无法解析 curl 命令: {0}").format(exc))
            return
        self._fill_from_curl(imported, params)
        warnings = list(imported.warnings)
        summary = tr("已从 curl 填入：{0}").format(
            tr("{0} 个字段").format(len([p for p in params if p in self._fields]))
        )
        QMessageBox.information(
            self,
            tr("从 curl 导入"),
            summary + ("\n\n" + tr("注意事项：") + "\n- " + "\n- ".join(warnings) if warnings else ""),
        )
        self._test_result.setStyleSheet("")
        self._test_result.setText(summary + tr("；请核对端点与鉴权后点「保存配置」"))

    def _fill_from_curl(self, imported, params: dict) -> None:
        """把解析结果写进表单（只写存在的字段）。"""
        if imported.base_url:
            self._set_field("base_url", imported.base_url)
        for key, value in params.items():
            if key in self._fields:
                self._set_field(key, value)
        logger.info(
            "curl 导入：method=%s url=%s headers=%d placeholders=%d",
            imported.method,
            imported.url,
            len(imported.headers or {}),
            len(imported.placeholders or {}),
        )

    # ------------------------------------------------------------------ #
    # 表单字段辅助
    # ------------------------------------------------------------------ #
    def _default_for(self, key: str):
        for f in FIELD_DEFS.get(self.kind, []):
            if f["key"] == key:
                return f.get("default")
        return None

    def _set_field(self, key: str, value) -> None:
        widget = self._fields.get(key)
        if widget is None:
            return
        if isinstance(widget, QCheckBox):
            # 兼容 JSON 手改后存成字符串的布尔值（"false"/"0"/"" -> False）
            if isinstance(value, str):
                value = value.strip().lower() not in ("", "0", "false", "no", "off", "none")
            widget.setChecked(bool(value))
        elif isinstance(widget, QComboBox):
            index = widget.findData(value)
            if index < 0:
                index = widget.findData(self._default_for(key))
            if index >= 0:
                widget.setCurrentIndex(index)
        elif isinstance(widget, QSpinBox):
            widget.setValue(int(value if value is not None else 0))
        elif isinstance(widget, QDoubleSpinBox):
            widget.setValue(float(value if value is not None else 0.0))
        elif isinstance(widget, QPlainTextEdit):
            widget.setPlainText(str(value if value is not None else ""))
        else:
            widget.setText(str(value if value is not None else ""))

    def _get_field(self, key: str):
        widget = self._fields.get(key)
        if widget is None:
            return None
        if isinstance(widget, QCheckBox):
            return widget.isChecked()
        if isinstance(widget, QComboBox):
            return widget.currentData()
        if isinstance(widget, QSpinBox):
            return widget.value()
        if isinstance(widget, QDoubleSpinBox):
            return widget.value()
        if isinstance(widget, QPlainTextEdit):
            return widget.toPlainText().strip()
        return widget.text().strip()

    def _set_form_enabled(self, enabled: bool) -> None:
        for widget in self._fields.values():
            widget.setEnabled(enabled)
        self._btn_save.setEnabled(enabled)
        self._btn_default.setEnabled(enabled)
        self._btn_test.setEnabled(enabled)
        self._btn_models.setEnabled(enabled)
        self._btn_delete.setEnabled(enabled)
        for button in getattr(self, "_relay_buttons", []):
            button.setEnabled(enabled)


class APIResultShim:
    """测试失败回调的简单包装（保持与 _on_test_done 的接口一致）。"""

    def __init__(self, ok: bool, message: str):
        self.ok = ok
        self.message = message
        self.data = ""
