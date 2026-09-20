"""IDE 模式页：分步式专业工作区。

布局：
- 左侧：步骤引导栏（6 步）+ 项目（新建/打开/保存）。
- 中间：主工作区（预览 / 像素编辑 / 提示词 三个 Tab）。
- 右侧：参数面板（描述、动作、分辨率、颜色、图转视频参数、处理选项、输出目录）。
- 底部：时间轴（缩略图 / 拖动排序 / 插入 / 复制 / 删除）+ 状态 + 日志。

每一步可独立执行；中间产物保存在 IdeSession 中，可随时编辑、重跑。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

from PIL import Image
from PySide6.QtCore import QSize, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from config.settings import (
    ASPECT_RATIOS,
    DEFAULT_FPS,
    DEFAULT_FRAME_COUNT,
    DEFAULT_MAX_COLORS,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_SPEED,
    PIXEL_SIZES,
)
from core.api.factory import create_api_client
from core.processing import frame_utils as fu
from core.workflow import (
    IDE_STEPS,
    IdeSession,
    IdeWorkflow,
    SoloResult,
    load_ide_project,
    save_ide_project,
)
from core.workflow.solo_workflow import WorkflowError
from ui.app_context import AppContext
from ui.i18n import T, tr
from ui.layout import scaled
from ui.qt_image import pil_to_qpixmap as _pil_to_qpixmap
from ui.widgets.action_combo import populate_action_combo
from ui.widgets.dock import RAIL_W, SideDock
from ui.widgets.image_viewer import ImageViewer
from ui.widgets.pixel_editor import PixelEditorWidget
from ui.widgets.reference_box import ReferenceImageBox
from ui.widgets.step_bar import StepBar
from ui.widgets.timeline import TimelineWidget
from ui.workers import IdeStepWorker

logger = logging.getLogger("PixelFoundry.ui.ide_page")

_LOG_COLORS = {"info": "#adb2b8", "warn": "#f59e0b", "error": "#f25a5a"}

PARAM_WIDTH = 360
ASSET_WIDTH = 236

# 步骤 -> 执行按钮文案（zh 原文 + 运行时翻译）
STEP_ACTIONS_ZH = ["生成提示词", "生成首帧图片", "生成动画", "像素化处理", "去除背景", "导出"]


class IdePage(QWidget):
    step_changed = Signal(int)  # 当前步骤切换（供主窗口侧栏高亮）

    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self._ctx = ctx
        self._session = IdeSession()
        self._worker: Optional[IdeStepWorker] = None
        self._current = 0  # 当前选中的帧索引
        self._current_step = 0
        self._playing = False
        self._play_index = 0
        self._play_timer = QTimer(self)
        self._play_timer.timeout.connect(self._on_play_tick)
        self._dirty = False
        self._editing_first_frame = False   # 帧列表为空时编辑器编辑的是首帧图
        self._step_done: set = set()        # 已完成步骤（步骤条上打勾）
        self._build_ui()
        self._restore_layout()
        self._restore_settings()
        self._refresh_all()

    # ------------------------------------------------------------------ #
    # UI 构建
    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 10)
        root.setSpacing(8)

        # ---------- 顶部：步骤条（进度可见、点一下切换）+ 主执行按钮 ----------
        top = QHBoxLayout()
        top.setSpacing(8)
        self._step_bar = StepBar(STEP_ACTIONS_ZH)
        self._step_bar.step_selected.connect(self.set_current_step)
        top.addWidget(self._step_bar, 1)
        self._btn_run_top = T(QPushButton(), "生成提示词")
        self._btn_run_top.setObjectName("PrimaryButton")
        self._btn_run_top.setMinimumHeight(scaled(30))
        self._btn_run_top.setMinimumWidth(scaled(120))
        T(self._btn_run_top, "执行当前步骤（与右侧参数栏底部按钮相同）", attr="tooltip")
        self._btn_run_top.clicked.connect(self._on_run_step)
        top.addWidget(self._btn_run_top)
        root.addLayout(top)

        # ---------- 三栏工作区：资源 | 预览/编辑/提示词 + 时间轴 | 参数 ----------
        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.setObjectName("Workspace")
        self._splitter.setChildrenCollapsible(False)
        self._splitter.setHandleWidth(scaled(5))

        # 左：资源（项目 / 参考图 / 首帧图）
        self._splitter.addWidget(self._build_asset_panel())

        # 中：上「预览 / 编辑 / 提示词」，下时间轴（帧与画面挨在一起，改帧不用来回找）
        center = QSplitter(Qt.Orientation.Vertical)
        center.setObjectName("IdeCenter")
        center.setChildrenCollapsible(False)
        center.setHandleWidth(scaled(5))
        self._tabs = QTabWidget()
        self._build_preview_tab()
        self._build_editor_tab()
        self._build_prompt_tab()
        center.addWidget(self._tabs)

        self._timeline = TimelineWidget()
        self._timeline.frame_selected.connect(self._on_frame_selected)
        self._timeline.reordered.connect(self._on_reordered)
        self._timeline.insert_requested.connect(self._on_insert_frame)
        self._timeline.duplicate_requested.connect(self._on_duplicate_frame)
        self._timeline.delete_requested.connect(self._on_delete_frame)
        self._timeline.add_requested.connect(self._on_add_frame)
        self._timeline.add_current_requested.connect(self._on_add_current_frame)
        center.addWidget(self._timeline)
        center.setStretchFactor(0, 1)
        center.setStretchFactor(1, 0)
        center.setSizes([scaled(460), scaled(140)])
        self._center_splitter = center
        self._splitter.addWidget(center)

        # 右：参数（默认展开，避免「进来什么都没有」）+ 日志
        self._splitter.addWidget(self._build_params_panel())
        self._splitter.setStretchFactor(0, 0)
        self._splitter.setStretchFactor(1, 1)
        self._splitter.setStretchFactor(2, 0)
        self._left_dock.bind_splitter(self._splitter, 0, default_width=ASSET_WIDTH)
        self._right_dock.bind_splitter(self._splitter, 2, default_width=PARAM_WIDTH)
        self._splitter.setSizes([scaled(ASSET_WIDTH), scaled(760), scaled(PARAM_WIDTH)])
        root.addWidget(self._splitter, 1)

        # ---------- 底部：状态行 ----------
        status_row = QHBoxLayout()
        # 状态行由 _update_status() 动态拼接（帧数 / 画布尺寸 / 下一步），不用 T() 注册，
        # 否则语言切换时注册表会把它回退成裸的「就绪」
        self._status_label = QLabel(tr("就绪"))
        self._status_label.setObjectName("StepLabel")
        status_row.addWidget(self._status_label)
        status_row.addStretch(1)
        self._dirty_label = QLabel("")
        self._dirty_label.setObjectName("HintLabel")
        status_row.addWidget(self._dirty_label)
        root.addLayout(status_row)

    # ------------------------------------------------------------------ #
    # 主窗口工具条协议（Krita 风格：工具条内容随工作区变化）
    # ------------------------------------------------------------------ #
    def toolbar_actions(self) -> list:
        """返回 [(图标, 文本, 提示, 回调, 是否主按钮), …] 供主窗口工具条渲染。"""
        step = max(0, min(self._current_step, len(STEP_ACTIONS_ZH) - 1))
        step_text = self._btn_run.text() or tr(STEP_ACTIONS_ZH[step])
        return [
            (self._step_icon(step), step_text,
             tr("执行步骤：{0} …").format(tr(STEP_ACTIONS_ZH[step])), self._on_run_step, True),
            ("play", self._btn_play.text(),
             tr("播放") if not self._playing else tr("暂停"), self._on_toggle_play, False),
            ("undo", tr("撤销（Ctrl+Z）"), tr("撤销（Ctrl+Z）"), self._editor.undo, False),
            ("redo", tr("重做（Ctrl+Shift+Z）"), tr("重做（Ctrl+Shift+Z）"), self._editor.redo, False),
        ]

    @staticmethod
    def _step_icon(step: int) -> str:
        """当前步骤对应的工具条图标（editor_icon 的 kind）。"""
        return {
            0: "pencil",        # 生成提示词
            1: "import_image",  # 生成首帧图片
            2: "play",          # 生成动画
            3: "grid",          # 像素化处理
            4: "eraser",        # 去除背景
            5: "export_image",  # 导出
        }.get(int(step), "play")

    def workspace_status(self) -> str:
        """工具条右侧的状态标识：当前步骤 + 帧数 / 帧率（无帧时也能安全返回）。"""
        try:
            step = max(0, min(self._current_step, len(STEP_ACTIONS_ZH) - 1))
            fps = max(1, int(self._session.fps or 0))
            return "{} · {} · {}fps".format(
                tr(STEP_ACTIONS_ZH[step]), tr("{0} 帧").format(len(self._session.frames)), fps
            )
        except Exception:  # noqa: BLE001
            return ""

    # ------------------------------------------------------------------ #
    def _build_preview_tab(self) -> None:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(4, 4, 4, 4)
        self._preview = ImageViewer()
        self._preview.zoomChanged.connect(self._on_preview_zoom_changed)
        layout.addWidget(self._preview, 1)
        row = QHBoxLayout()
        self._btn_play = T(QPushButton(), "播放")
        self._btn_play.clicked.connect(self._on_toggle_play)
        row.addWidget(self._btn_play)
        # 缩放控制（预览区放大/缩小/适应）
        zoom_out = QPushButton("−")
        zoom_out.setFixedSize(26, 26)
        T(zoom_out, "缩小预览", attr="tooltip")
        zoom_out.clicked.connect(lambda: self._preview.zoom_out())
        row.addWidget(zoom_out)
        self._zoom_label = QLabel("100%")
        self._zoom_label.setFixedWidth(40)
        self._zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(self._zoom_label)
        zoom_in = QPushButton("＋")
        zoom_in.setFixedSize(26, 26)
        T(zoom_in, "放大预览", attr="tooltip")
        zoom_in.clicked.connect(lambda: self._preview.zoom_in())
        row.addWidget(zoom_in)
        fit_btn = QPushButton(tr("适应"))
        T(fit_btn, "重置为适应窗口", attr="tooltip")
        fit_btn.clicked.connect(lambda: self._preview.reset_zoom())
        row.addWidget(fit_btn)
        # 播放倍速
        self._preview_speed_combo = QComboBox()
        for label, value in [("0.5x", 0.5), ("1x（原速）", 1.0), ("1.5x", 1.5), ("2x", 2.0), ("3x", 3.0)]:
            self._preview_speed_combo.addItem(tr(label), userData=value)
        self._preview_speed_combo.setCurrentIndex(1)
        self._preview_speed_combo.currentIndexChanged.connect(self._on_preview_speed_changed)
        row.addWidget(self._preview_speed_combo)
        self._preview_hint = QLabel("")
        self._preview_hint.setObjectName("HintLabel")
        row.addWidget(self._preview_hint)
        row.addStretch(1)
        layout.addLayout(row)
        self._tabs.addTab(tab, T(None, "预览"))
        T(self._tabs, "预览", attr="tab", index=0)

    def _build_editor_tab(self) -> None:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(4, 4, 4, 4)
        self._editor = PixelEditorWidget()
        self._editor.edited.connect(self._on_editor_edited)
        layout.addWidget(self._editor, 1)
        self._editor_hint = QLabel(tr("在时间轴选择帧后，在此用铅笔/橡皮/取色/填充编辑像素"))
        self._editor_hint.setObjectName("HintLabel")
        layout.addWidget(self._editor_hint)
        self._tabs.addTab(tab, T(None, "编辑"))
        T(self._tabs, "编辑", attr="tab", index=1)

    def _build_prompt_tab(self) -> None:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(4, 8, 4, 4)
        layout.setSpacing(6)
        self._prompt_edits: dict = {}
        for key, label in (
            ("image_prompt", "图片提示词"),
            ("animation_prompt", "动画提示词"),
            ("negative_prompt", "负面提示词"),
        ):
            layout.addWidget(T(QLabel(), label))
            edit = QPlainTextEdit()
            edit.setObjectName("LogView")
            edit.setMaximumHeight(90)
            self._prompt_edits[key] = edit
            layout.addWidget(edit)
        btn_row = QHBoxLayout()
        apply_btn = T(QPushButton(), "应用提示词到工作区")
        apply_btn.clicked.connect(self._on_apply_prompts)
        btn_row.addWidget(apply_btn)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)
        layout.addStretch(1)
        self._tabs.addTab(tab, T(None, "提示词"))
        T(self._tabs, "提示词", attr="tab", index=2)

    # ------------------------------------------------------------------ #
    # 右侧参数停靠栏：默认整栏收起（只剩竖排标签），点击展开提示词/文生图等参数
    # ------------------------------------------------------------------ #
    def _on_toggle_params(self) -> None:
        self._params_collapsed = not self._params_collapsed
        self._apply_params_collapsed()

    def _on_dock_collapsed_changed(self, collapsed: bool) -> None:
        """停靠栏自己收起/展开（点竖排标签）-> 同步参数面板状态。"""
        if self._params_collapsed != bool(collapsed):
            self._params_collapsed = bool(collapsed)
            self._apply_params_collapsed()

    def _apply_params_collapsed(self) -> None:
        from ui.icons import editor_icon

        self._params_scroll.setVisible(not self._params_collapsed)
        kind = "chevron_left" if self._params_collapsed else "chevron_right"
        self._params_toggle_btn.setIcon(editor_icon(kind, "#9aa0a8", size=scaled(14)))
        self._params_toggle_btn.setToolTip(
            tr("展开参数面板") if self._params_collapsed else tr("收起参数面板")
        )
        # 整栏一起收起：宽度让给画布/预览（SideDock 会自行重排 splitter，可再拖回来）
        if hasattr(self, "_right_dock"):
            self._right_dock.set_collapsed(self._params_collapsed)

    def _on_toggle_log(self) -> None:
        """底部日志框收起/展开。"""
        self._log_collapsed = not self._log_collapsed
        self._log_view.setVisible(not self._log_collapsed)
        self._log_toggle_btn.setText("▴" if self._log_collapsed else "▾")

    def apply_ui_scale(self, scale: float) -> None:
        """按界面比例调整停靠栏与分隔条（接口比例由全局 scaled() 取值）。"""
        if hasattr(self, "_params_scroll"):
            self._params_scroll.setMinimumWidth(scaled(200))
        if hasattr(self, "_params_toggle_btn"):
            self._params_toggle_btn.setFixedSize(scaled(20), scaled(20))
            self._params_toggle_btn.setIconSize(QSize(scaled(14), scaled(14)))
        if hasattr(self, "_first_frame_thumb"):
            self._first_frame_thumb.setFixedSize(scaled(48), scaled(48))
        for name in ("_left_dock", "_right_dock"):
            dock = getattr(self, name, None)
            if dock is not None:
                dock.apply_ui_scale()
        for name in ("_splitter", "_center_splitter"):
            splitter = getattr(self, name, None)
            if splitter is not None:
                splitter.setHandleWidth(scaled(5))
        if hasattr(self, "_btn_run_top"):
            self._btn_run_top.setMinimumHeight(scaled(30))
            self._btn_run_top.setMinimumWidth(scaled(120))

    # ------------------------------------------------------------------ #
    # 布局持久化（三栏宽度 + 两侧收起状态 + 中栏上下比例）
    # ------------------------------------------------------------------ #
    def _restore_layout(self) -> None:
        """恢复上次的停靠栏宽度与收起状态（病态布局自动回退默认值）。"""
        try:
            s = self._ctx.ui_settings
            sizes = s.get("ide_dock_sizes") or []
            if (isinstance(sizes, (list, tuple)) and len(sizes) == 3
                    and int(sizes[1]) >= scaled(360)):
                # 中栏（预览+时间轴）过窄说明上次保存的是病态布局 -> 退回默认宽度
                self._splitter.setSizes([int(v) for v in sizes])
                if int(sizes[0]) > scaled(RAIL_W):
                    self._left_dock.bind_splitter(self._splitter, 0, default_width=int(sizes[0]))
                if int(sizes[2]) > scaled(RAIL_W):
                    self._right_dock.bind_splitter(self._splitter, 2, default_width=int(sizes[2]))
            center = s.get("ide_center_sizes") or []
            if isinstance(center, (list, tuple)) and len(center) == 2 and int(center[0]) >= scaled(200):
                self._center_splitter.setSizes([int(v) for v in center])
            saved = s.get("ide_params_collapsed")
            if saved is not None:
                self._params_collapsed = bool(saved)
                self._apply_params_collapsed()
            left_saved = s.get("ide_left_collapsed")
            if left_saved is not None:
                self._left_dock.set_collapsed(bool(left_saved))
        except Exception as exc:  # noqa: BLE001
            logger.warning("IDE 页布局恢复失败: %s", exc)

    def _remember_layout(self) -> None:
        try:
            s = self._ctx.ui_settings
            s.set("ide_dock_sizes", list(self._splitter.sizes()))
            s.set("ide_center_sizes", list(self._center_splitter.sizes()))
            s.set("ide_params_collapsed", bool(self._params_collapsed))
            s.set("ide_left_collapsed", bool(self._left_dock.is_collapsed()))
        except Exception as exc:  # noqa: BLE001
            logger.warning("IDE 页布局保存失败: %s", exc)

    def hideEvent(self, event) -> None:  # noqa: N802
        self._remember_layout()
        super().hideEvent(event)

    # ------------------------------------------------------------------ #
    def _build_asset_panel(self) -> QWidget:
        """左侧资源停靠栏：项目 + 参考图 / 首帧图（这两块原先挤在右侧且默认收起）。"""
        dock = SideDock(tr("资源"), side="left", default_width=ASSET_WIDTH)
        self._left_dock = dock

        # ---- 项目 ----
        proj_box = QGroupBox(tr("项目"))
        pf = QHBoxLayout(proj_box)
        pf.setContentsMargins(12, 18, 12, 12)
        pf.setSpacing(6)
        self._btn_new = QPushButton(tr("新建"))
        T(self._btn_new, "清空工作区，从头开始", attr="tooltip")
        self._btn_new.clicked.connect(self._on_new)
        pf.addWidget(self._btn_new)
        self._btn_open = QPushButton(tr("打开"))
        T(self._btn_open, "打开已保存的 IDE 项目（帧序列 + 首帧图）", attr="tooltip")
        self._btn_open.clicked.connect(self._on_open_project)
        pf.addWidget(self._btn_open)
        self._btn_save = QPushButton(tr("保存"))
        T(self._btn_save, "保存项目：帧序列 PNG + 首帧图 + 参数", attr="tooltip")
        self._btn_save.clicked.connect(self._on_save_project)
        pf.addWidget(self._btn_save)
        proj_panel = dock.add_docker("项目", proj_box, icon_kind="layers")
        proj_panel.set_icon("layers")

        # ---- 参考图 / 首帧图 ----
        img_box = QGroupBox(tr("参考图 / 首帧图"))
        ib = QVBoxLayout(img_box)
        ib.setContentsMargins(12, 18, 12, 12)
        ib.setSpacing(8)
        ref_row = QHBoxLayout()
        ref_row.setSpacing(10)
        self._ref_box = ReferenceImageBox(size=88)
        self._ref_box.changed.connect(self._on_ref_changed)
        ref_row.addWidget(self._ref_box)
        col = QVBoxLayout()
        col.setSpacing(4)
        col.addWidget(T(QLabel(), "点击添加自备参考图"))
        hint = T(QLabel(), "同时作为首帧图：可直接走「动画生成」步骤，\n或走「图片生成」步骤做图生图。")
        hint.setObjectName("HintLabel")
        hint.setWordWrap(True)
        col.addWidget(hint)
        ref_row.addLayout(col, 1)
        ib.addLayout(ref_row)

        # 首帧图来源一目了然：动画步骤实际会把哪张图送进视频 API
        ff_row = QHBoxLayout()
        ff_row.setSpacing(8)
        self._first_frame_thumb = QLabel()
        self._first_frame_thumb.setFixedSize(scaled(48), scaled(48))
        self._first_frame_thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._first_frame_thumb.setObjectName("FirstFrameThumb")
        T(self._first_frame_thumb, "将作为首帧送入视频 API 的图片", attr="tooltip")
        ff_row.addWidget(self._first_frame_thumb)
        ff_col = QVBoxLayout()
        ff_col.setSpacing(3)
        # 文案由 _refresh_first_frame_row() 动态生成（含「未设置」/尺寸两种形态），
        # 不用 T() 注册，避免语言切换时被注册表覆盖成与当前状态不符的文案
        self._first_frame_label = QLabel(tr("首帧图：未设置（生成首帧图片，或用当前帧作为首帧）"))
        self._first_frame_label.setObjectName("HintLabel")
        self._first_frame_label.setWordWrap(True)
        ff_col.addWidget(self._first_frame_label)
        self._btn_first_from_current = T(QPushButton(), "用当前帧作为首帧")
        T(self._btn_first_from_current, "把时间轴当前选中的帧设为动画生成的首帧图", attr="tooltip")
        self._btn_first_from_current.clicked.connect(self._set_first_frame_from_current)
        self._btn_first_from_current.setEnabled(False)
        ff_col.addWidget(self._btn_first_from_current)
        ff_row.addLayout(ff_col, 1)
        ib.addLayout(ff_row)
        img_panel = dock.add_docker("参考图 / 首帧图", img_box, icon_kind="import_image")
        img_panel.set_icon("import_image")
        return dock

    # ------------------------------------------------------------------ #
    def _build_params_panel(self) -> QWidget:
        """右侧参数停靠栏：步骤参数（默认展开）+ 日志。"""
        dock = SideDock(tr("参数"), side="right", default_width=PARAM_WIDTH)
        self._right_dock = dock
        dock.collapsedChanged.connect(self._on_dock_collapsed_changed)

        # ---- 分步骤参数（随步骤切换）+ 执行按钮 ----
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setMinimumWidth(scaled(200))
        self._params_scroll = scroll

        host = QWidget()
        layout = QVBoxLayout(host)
        layout.setContentsMargins(0, 0, 6, 0)
        layout.setSpacing(10)

        # 分步骤参数：随步骤切换只显示本步骤相关参数
        self._step_params = QStackedWidget()
        self._step_params.addWidget(self._build_step_text_panel())    # 0 文本
        self._step_params.addWidget(self._build_step_image_panel())   # 1 图片
        self._step_params.addWidget(self._build_step_anim_panel())    # 2 动画
        self._step_params.addWidget(self._build_step_pixel_panel())   # 3 像素
        self._step_params.addWidget(self._build_step_bg_panel())      # 4 背景
        self._step_params.addWidget(self._build_step_export_panel())  # 5 导出
        layout.addWidget(self._step_params, 1)

        # 执行按钮固定在参数面板底部
        self._btn_run = T(QPushButton(), "生成提示词")
        self._btn_run.setObjectName("PrimaryButton")
        self._btn_run.setMinimumHeight(36)
        self._btn_run.clicked.connect(self._on_run_step)
        layout.addWidget(self._btn_run)

        scroll.setWidget(host)

        # 标题栏里的「收起整栏」三角钮（收起后点竖排标签即可展开）
        self._params_toggle_btn = QToolButton()
        self._params_toggle_btn.setObjectName("DockToggle")
        self._params_toggle_btn.setAutoRaise(True)
        self._params_toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._params_toggle_btn.setIconSize(QSize(scaled(14), scaled(14)))
        self._params_toggle_btn.setFixedSize(scaled(20), scaled(20))
        self._params_toggle_btn.clicked.connect(self._on_toggle_params)

        step_panel = dock.add_docker("步骤参数", scroll, icon_kind="palette", stretch=3)
        step_panel.set_icon("palette")
        step_panel.add_header_widget(self._params_toggle_btn)

        # ---- 日志（可折叠；运行时看进度，平时收起来给参数让位） ----
        log_box = QWidget()
        log_layout = QVBoxLayout(log_box)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.setSpacing(4)
        log_header = QHBoxLayout()
        self._log_toggle_btn = QToolButton()
        self._log_toggle_btn.setText("▾")
        self._log_toggle_btn.setFixedSize(20, 20)
        T(self._log_toggle_btn, "收起/展开日志", attr="tooltip")
        self._log_toggle_btn.clicked.connect(self._on_toggle_log)
        log_header.addWidget(self._log_toggle_btn)
        log_header.addStretch(1)
        clear_btn = T(QPushButton(), "清空")
        T(clear_btn, "清空日志", attr="tooltip")
        clear_btn.clicked.connect(lambda: self._log_view.clear())
        log_header.addWidget(clear_btn)
        log_layout.addLayout(log_header)
        self._log_view = QPlainTextEdit()
        self._log_view.setObjectName("LogView")
        self._log_view.setReadOnly(True)
        self._log_view.setMinimumHeight(scaled(90))
        self._log_collapsed = False
        log_layout.addWidget(self._log_view, 1)
        log_panel = dock.add_docker("日志", log_box, icon_kind="console", stretch=2)
        log_panel.set_icon("console")

        # 默认展开（旧版默认收起，进 IDE 什么都看不到，是主要的「反人类」来源）
        self._params_collapsed = False
        self._apply_params_collapsed()
        return dock

    # ------------------------------------------------------------------ #
    # 分步骤参数面板
    # ------------------------------------------------------------------ #
    def _build_step_text_panel(self) -> QWidget:
        box = T(QGroupBox(), "步骤 1 · 文本生成")
        f = QFormLayout(box)
        f.setContentsMargins(12, 18, 12, 12)
        f.setVerticalSpacing(8)
        self._desc_edit = QTextEdit()
        T(self._desc_edit, "例如：一只拿着剑的橙色小猫，Q 版，侧身站立", attr="placeholder")
        self._desc_edit.setMaximumHeight(80)
        f.addRow(T(QLabel(), "文本描述"), self._desc_edit)
        self._action_combo = QComboBox()
        self._action_combo.setEditable(True)
        self._action_combo.setPlaceholderText(T(None, "选择或输入动作…"))
        populate_action_combo(self._action_combo)
        f.addRow(T(QLabel(), "动作类型(可选)"), self._action_combo)
        tip = T(QLabel(), "生成图片/动画提示词（LLM 失败自动用本地模板）")
        tip.setObjectName("HintLabel")
        tip.setWordWrap(True)
        f.addRow("", tip)
        return box

    def _build_step_image_panel(self) -> QWidget:
        box = T(QGroupBox(), "步骤 2 · 图片生成")
        f = QFormLayout(box)
        f.setContentsMargins(12, 18, 12, 12)
        f.setVerticalSpacing(8)
        self._aspect_combo = QComboBox()
        for ratio in ASPECT_RATIOS:
            self._aspect_combo.addItem(ratio)
        self._aspect_combo.setCurrentText("1:1")
        f.addRow(T(QLabel(), "宽高比"), self._aspect_combo)
        self._size_combo = QComboBox()
        for size in PIXEL_SIZES:
            self._size_combo.addItem(str(size))
        self._size_combo.setCurrentText("128")
        f.addRow(T(QLabel(), "像素尺寸"), self._size_combo)
        tip = T(QLabel(), "添加参考图后即走图生图（i2i）；像素风自动按像素分辨率出图")
        tip.setObjectName("HintLabel")
        tip.setWordWrap(True)
        f.addRow("", tip)
        return box

    def _build_step_anim_panel(self) -> QWidget:
        box = T(QGroupBox(), "步骤 3 · 动画生成")
        f = QFormLayout(box)
        f.setContentsMargins(12, 18, 12, 12)
        f.setVerticalSpacing(8)
        self._frames_spin = QSpinBox()
        self._frames_spin.setRange(2, 120)
        self._frames_spin.setValue(DEFAULT_FRAME_COUNT)
        f.addRow(T(QLabel(), "帧数"), self._frames_spin)
        self._fps_spin = QSpinBox()
        self._fps_spin.setRange(1, 30)
        self._fps_spin.setValue(DEFAULT_FPS)
        f.addRow(T(QLabel(), "帧率(fps)"), self._fps_spin)
        self._speed_combo = QComboBox()
        for label, value in [("0.5x", 0.5), ("1x（原速）", 1.0), ("1.5x", 1.5), ("2x", 2.0)]:
            self._speed_combo.addItem(tr(label), userData=value)
        self._speed_combo.setCurrentIndex(1)
        f.addRow(T(QLabel(), "播放速度"), self._speed_combo)
        self._loop_chk = T(QCheckBox(), "首尾帧一致（循环闭合）")
        self._loop_chk.setChecked(True)
        f.addRow(self._loop_chk)
        tip = T(QLabel(), "动画生成用左栏「首帧图」那张图送入视频 API；没设首帧时会自动用帧序列第 1 帧")
        tip.setObjectName("HintLabel")
        tip.setWordWrap(True)
        f.addRow("", tip)
        return box

    def _build_step_pixel_panel(self) -> QWidget:
        box = T(QGroupBox(), "步骤 4 · 像素化")
        f = QFormLayout(box)
        f.setContentsMargins(12, 18, 12, 12)
        f.setVerticalSpacing(8)
        self._pixelate_chk = T(QCheckBox(), "完美像素化")
        self._pixelate_chk.setChecked(True)
        f.addRow(self._pixelate_chk)
        self._colors_spin = QSpinBox()
        self._colors_spin.setRange(2, 64)
        self._colors_spin.setValue(DEFAULT_MAX_COLORS)
        f.addRow(T(QLabel(), "最大颜色数"), self._colors_spin)
        tip = T(QLabel(), "首帧自动检测网格大小，全部帧按同一网格精确采样；非像素风自动跳过")
        tip.setObjectName("HintLabel")
        tip.setWordWrap(True)
        f.addRow("", tip)
        return box

    def _build_step_bg_panel(self) -> QWidget:
        box = T(QGroupBox(), "步骤 5 · 背景去除")
        f = QFormLayout(box)
        f.setContentsMargins(12, 18, 12, 12)
        f.setVerticalSpacing(8)
        self._bg_chk = T(QCheckBox(), "去除背景")
        self._bg_chk.setChecked(True)
        f.addRow(self._bg_chk)
        self._whiten_chk = T(QCheckBox(), "背景强制纯色")
        self._whiten_chk.setChecked(True)
        f.addRow(self._whiten_chk)
        self._bg_tolerance_spin = QSpinBox()
        self._bg_tolerance_spin.setRange(0, 200)
        self._bg_tolerance_spin.setValue(30)
        f.addRow(T(QLabel(), "背景容差"), self._bg_tolerance_spin)
        self._bg_feather_spin = QSpinBox()
        self._bg_feather_spin.setRange(0, 30)
        self._bg_feather_spin.setValue(8)
        f.addRow(T(QLabel(), "羽化(px)"), self._bg_feather_spin)
        self._bg_erode_spin = QSpinBox()
        self._bg_erode_spin.setRange(0, 12)
        self._bg_erode_spin.setValue(0)
        T(self._bg_erode_spin, "前景内缩像素：消掉对象边缘残留的白边/白晕", attr="tooltip")
        f.addRow(T(QLabel(), "内缩(px)"), self._bg_erode_spin)
        preview_btn = T(QPushButton(), "预览抠图效果…")
        T(preview_btn, "实时预览背景扣除效果并调整容差/内缩/羽化", attr="tooltip")
        preview_btn.clicked.connect(self._on_preview_background)
        f.addRow("", preview_btn)
        tip = T(QLabel(), "颜色键 + 容差 + 内缩去白边 + 羽化；强制纯色影响动画生成时的背景稳定约束")
        tip.setObjectName("HintLabel")
        tip.setWordWrap(True)
        f.addRow("", tip)
        return box

    def _on_preview_background(self) -> None:
        """打开背景扣除预览弹窗；确认后应用参数并重跑背景步骤。"""
        from ui.dialogs.background_key_dialog import BackgroundKeyDialog

        src = None
        if self._session.frames:
            src = self._session.frames[0]
        elif self._session.first_frame is not None:
            src = self._session.first_frame
        if src is None:
            QMessageBox.information(self, tr("提示"), tr("请先生成动画或导入首帧图再预览抠图"))
            return
        dialog = BackgroundKeyDialog(
            src,
            tolerance=self._bg_tolerance_spin.value(),
            feather=self._bg_feather_spin.value(),
            erode=self._bg_erode_spin.value(),
            force_pure_bg=self._whiten_chk.isChecked(),
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        p = dialog.params()
        self._bg_tolerance_spin.setValue(p["tolerance"])
        self._bg_feather_spin.setValue(p["feather"])
        self._bg_erode_spin.setValue(p["erode"])
        self._whiten_chk.setChecked(p["force_pure_bg"])
        self.set_current_step(4)
        self._on_run_step()  # 用新参数重跑背景去除

    def _build_step_export_panel(self) -> QWidget:
        box = T(QGroupBox(), "步骤 6 · 导出")
        ef = QFormLayout(box)
        ef.setContentsMargins(12, 18, 12, 12)
        out_row = QHBoxLayout()
        self._output_edit = QLineEdit(str(DEFAULT_OUTPUT_DIR))
        out_row.addWidget(self._output_edit, 1)
        browse = T(QPushButton(), "浏览…")
        browse.clicked.connect(self._on_browse_output)
        out_row.addWidget(browse)
        ef.addRow(T(QLabel(), "输出目录"), out_row)
        self._btn_open_out = T(QPushButton(), "打开输出目录")
        self._btn_open_out.clicked.connect(self._on_open_output)
        ef.addRow("", self._btn_open_out)
        tip = T(QLabel(), "导出 GIF / APNG / PNG 序列 / 雪碧图 / JSON 元数据 / 项目文件")
        tip.setObjectName("HintLabel")
        tip.setWordWrap(True)
        ef.addRow("", tip)
        return box

    # ------------------------------------------------------------------ #
    def _restore_settings(self) -> None:
        out = self._ctx.ui_settings.get("output_dir")
        if out:
            self._output_edit.setText(str(out))
        self._log(tr("IDE 模式：逐步执行或直接编辑帧序列"), "info")

    # ------------------------------------------------------------------ #
    # 会话同步
    # ------------------------------------------------------------------ #
    def _sync_session(self) -> None:
        """把表单参数写回 session（运行步骤前调用）。"""
        s = self._session
        # 帧列表为空时编辑器里显示的可能是首帧图：先把它同步回去，
        # 否则用户改了首帧、动画步骤却用旧图（旧版的实际 bug）
        if self._editing_first_frame and not s.frames and hasattr(self, "_editor"):
            s.first_frame = self._editor.frame().copy()
        s.description = self._desc_edit.toPlainText().strip()
        s.action = self._action_combo.currentData() or self._action_combo.currentText().strip()
        s.aspect_ratio = self._aspect_combo.currentText()
        s.pixel_size = int(self._size_combo.currentText())
        s.max_colors = self._colors_spin.value()
        s.frame_count = self._frames_spin.value()
        s.fps = self._fps_spin.value()
        s.speed = float(self._speed_combo.currentData() or DEFAULT_SPEED)
        s.pixelate = self._pixelate_chk.isChecked()
        s.remove_bg = self._bg_chk.isChecked()
        s.force_pure_bg = self._whiten_chk.isChecked()
        s.loop_close = self._loop_chk.isChecked()
        s.bg_tolerance = self._bg_tolerance_spin.value()
        s.bg_feather = self._bg_feather_spin.value()
        s.bg_erode = self._bg_erode_spin.value()
        s.output_dir = Path(self._output_edit.text().strip() or str(DEFAULT_OUTPUT_DIR))

    def _load_session_to_form(self) -> None:
        s = self._session
        self._desc_edit.setPlainText(s.description)
        self._action_combo.setCurrentText(s.action)
        self._aspect_combo.setCurrentText(s.aspect_ratio)
        if str(s.pixel_size) in [self._size_combo.itemText(i) for i in range(self._size_combo.count())]:
            self._size_combo.setCurrentText(str(s.pixel_size))
        self._colors_spin.setValue(s.max_colors)
        self._frames_spin.setValue(s.frame_count)
        self._fps_spin.setValue(s.fps)
        idx = self._speed_combo.findData(s.speed)
        self._speed_combo.setCurrentIndex(idx if idx >= 0 else 1)
        self._pixelate_chk.setChecked(s.pixelate)
        self._bg_chk.setChecked(s.remove_bg)
        self._whiten_chk.setChecked(s.force_pure_bg)
        self._loop_chk.setChecked(s.loop_close)
        self._bg_tolerance_spin.setValue(s.bg_tolerance)
        self._bg_feather_spin.setValue(s.bg_feather)
        self._bg_erode_spin.setValue(s.bg_erode)
        self._output_edit.setText(str(s.output_dir))
        self._load_prompts_to_form()

    def _load_prompts_to_form(self) -> None:
        for key, edit in self._prompt_edits.items():
            edit.setPlainText(self._session.prompts.get(key, ""))

    def _on_apply_prompts(self) -> None:
        for key, edit in self._prompt_edits.items():
            self._session.prompts[key] = edit.toPlainText().strip()
        self._log(tr("提示词已应用到工作区"), "info")
        self._mark_dirty()

    # ------------------------------------------------------------------ #
    # 刷新
    # ------------------------------------------------------------------ #
    def _refresh_all(self) -> None:
        self._refresh_preview()
        self._refresh_editor()
        self._refresh_timeline()
        self._ref_box.set_image(self._session.reference_image)
        self._refresh_first_frame_row()
        self._update_play_button()
        self._update_status()

    def _refresh_preview(self) -> None:
        frames = self._session.frames
        if frames:
            self._show_frame(frames[min(self._play_index, len(frames) - 1)])
        elif self._session.first_frame is not None:
            self._preview.show_image(_pil_to_qpixmap(self._session.first_frame))
        else:
            self._preview.clear()

    def _refresh_editor(self) -> None:
        frames = self._session.frames
        if frames and 0 <= self._current < len(frames):
            self._editing_first_frame = False
            self._editor.set_frame(frames[self._current])
            prev = frames[self._current - 1] if self._current > 0 else None
            nxt = frames[self._current + 1] if self._current < len(frames) - 1 else None
            self._editor.set_onion(prev, nxt)
            self._editor_hint.setText(tr("正在编辑帧 {cur}/{total}").format(cur=self._current + 1, total=len(frames)))
        elif self._session.first_frame is not None:
            # 还没有帧序列时，编辑器直接编辑首帧图（旧版这里是空白画布，用户以为图片丢了）
            self._editing_first_frame = True
            self._editor.set_frame(self._session.first_frame)
            self._editor.set_onion(None, None)
            self._editor_hint.setText(
                tr("正在编辑首帧图（改完点「+ 当前图」即可加入帧序列，或直接走「动画生成」）")
            )
        else:
            self._editing_first_frame = False
            self._editor.set_frame(Image.new("RGBA", self._session.target_size(), (0, 0, 0, 0)))
            self._editor.set_onion(None, None)
            self._editor_hint.setText(tr("暂无帧，先生成动画或添加空白帧"))

    def _refresh_first_frame_row(self) -> None:
        """刷新「首帧图」缩略图/说明与「用当前帧作为首帧」按钮状态。"""
        first = self._session.first_frame
        if first is not None:
            self._first_frame_thumb.setPixmap(_pil_to_qpixmap(first).scaled(
                scaled(44), scaled(44),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation,
            ))
            self._first_frame_label.setText(
                tr("首帧图：{0}×{1}（动画生成将用它）").format(first.width, first.height)
            )
        else:
            self._first_frame_thumb.clear()
            self._first_frame_label.setText(tr("首帧图：未设置（生成首帧图片，或用当前帧作为首帧）"))
        self._btn_first_from_current.setEnabled(bool(self._session.frames))

    def _refresh_timeline(self) -> None:
        self._timeline.set_frames(self._session.frames, select=self._current)

    def _show_frame(self, img: Image.Image) -> None:
        self._preview.show_image(_pil_to_qpixmap(img))

    def _on_preview_zoom_changed(self, rel: float) -> None:
        """预览缩放百分比同步（1.0 = 适应容器）。"""
        self._zoom_label.setText(tr("适应") if abs(rel - 1.0) < 1e-9 else f"{round(rel * 100)}%")

    def _preview_speed(self) -> float:
        return float(self._preview_speed_combo.currentData() or 1.0)

    def _on_preview_speed_changed(self) -> None:
        """播放中调整倍速：按新倍速重启定时器。"""
        if self._playing:
            interval = max(30, int(1000 / (max(1, self._session.fps) * self._preview_speed())))
            self._play_timer.start(interval)

    def _update_play_button(self) -> None:
        self._btn_play.setText(tr("暂停") if self._playing else tr("播放"))

    def _update_status(self) -> None:
        s = self._session
        n = len(s.frames)
        size = s.target_size()
        if n:
            self._status_label.setText(
                tr("帧 {cur}/{n}（共 {n} 帧）· {w}×{h} · {fps}fps").format(
                    cur=self._current + 1, n=n, w=size[0], h=size[1], fps=s.fps
                )
            )
        else:
            # 没有帧序列时说明当前「手上有什么」，避免用户以为图片丢了
            if s.first_frame is not None:
                hint = tr("首帧图 {w}×{h}").format(w=s.first_frame.width, h=s.first_frame.height)
            elif s.reference_image is not None:
                hint = tr("仅有参考图")
            else:
                hint = tr("就绪")
            self._status_label.setText(f"{hint} · {size[0]}×{size[1]}")
        next_hint = ""
        if self._current_step + 1 < len(STEP_ACTIONS_ZH):
            next_hint = tr("下一步：{0}").format(tr(STEP_ACTIONS_ZH[self._current_step + 1]))
        self._dirty_label.setText(
            (tr("● 未保存") if self._dirty else "") + (f"　{next_hint}" if next_hint else "")
        )

    def _mark_dirty(self) -> None:
        self._dirty = True
        self._update_status()

    # ------------------------------------------------------------------ #
    # 步骤执行
    # ------------------------------------------------------------------ #
    def set_current_step(self, row: int) -> None:
        """设置当前步骤（主窗口侧栏 / 页面步骤条调用），同步按钮文案与分步参数。"""
        if 0 <= row < len(STEP_ACTIONS_ZH):
            self._current_step = row
            self._btn_run.setText(tr(STEP_ACTIONS_ZH[row]))
            if hasattr(self, "_btn_run_top"):
                self._btn_run_top.setText(tr(STEP_ACTIONS_ZH[row]))
            if hasattr(self, "_step_bar"):
                self._step_bar.set_current(row)
            self._step_params.setCurrentIndex(row)
            self._update_status()
            self.step_changed.emit(row)

    def _mark_step_done(self, step: int) -> None:
        """把步骤标注为已完成（步骤条打勾），并在状态行提示下一步。"""
        self._step_done.add(int(step))
        if hasattr(self, "_step_bar"):
            self._step_bar.mark_done(step)
        if step + 1 < len(STEP_ACTIONS_ZH):
            self._log(tr("下一步：{0}").format(tr(STEP_ACTIONS_ZH[step + 1])), "info")
        self._update_status()

    def _reset_steps_done(self) -> None:
        self._step_done.clear()
        if hasattr(self, "_step_bar"):
            self._step_bar.clear_done()

    def _on_run_step(self) -> None:
        self._sync_session()
        step = self._current_step
        if step == 0:
            fn = lambda wf: wf.step_prompts(self._session)
        elif step == 1:
            fn = lambda wf: wf.step_image(self._session)
        elif step == 2:
            fn = lambda wf: wf.step_animation(self._session)
        elif step == 3:
            fn = lambda wf: wf.step_pixelize(self._session)
        elif step == 4:
            fn = lambda wf: wf.step_background(self._session)
        elif step == 5:
            fn = lambda wf: wf.export(
                self._session, self._export_dir(), fps=self._session.fps,
                formats=("gif", "png", "json", "apng", "sprite"),
            )
        else:
            return
        self._run_step(fn, step)

    def _run_step(self, fn, step: int) -> None:
        def job(log_cb):
            clients = self._create_clients()
            try:
                wf = IdeWorkflow(clients["llm"], clients["image"], clients["video"], log=log_cb)
                return fn(wf)
            finally:
                for c in clients.values():
                    try:
                        c.close()
                    except Exception:  # noqa: BLE001
                        pass

        self._worker = IdeStepWorker(job)
        self._worker.log.connect(self._on_log)
        self._worker.succeeded.connect(lambda result: self._on_step_success(step, result))
        self._worker.failed.connect(self._on_step_failed)
        self._set_busy(True)
        self._log(tr("执行步骤：{0} …").format(IDE_STEPS[step]), "info")
        self._worker.start()

    def _create_clients(self) -> dict:
        clients = {}
        for kind in ("llm", "image", "video"):
            cfg = self._ctx.api.get_default(kind)
            if cfg is None:
                raise WorkflowError(tr("未配置{0} API，请在「设置」中配置或开启模拟 API").format(kind))
            clients[kind] = create_api_client(kind, cfg)
        return clients

    def _on_log(self, level: str, message: str) -> None:
        self._log(message, level)

    def _on_step_success(self, step: int, result) -> None:
        self._set_busy(False)
        self._mark_dirty()
        self._mark_step_done(step)
        if step == 0:
            self._load_prompts_to_form()
            self._tabs.setCurrentIndex(2)
            self._log(tr("提示词已生成，可在「提示词」页签编辑"), "info")
        elif step == 1:
            self._refresh_preview()
            self._tabs.setCurrentIndex(0)
            self._log(tr("首帧图片已生成"), "info")
        elif step == 2:
            self._current = 0
            self._play_index = 0
            self._refresh_all()
            self._tabs.setCurrentIndex(0)
            self._log(tr("动画已生成：{0} 帧").format(len(self._session.frames)), "info")
        elif step == 3:
            self._refresh_all()
            self._log(tr("像素化完成"), "info")
        elif step == 4:
            self._refresh_all()
            self._log(tr("背景去除完成"), "info")
        elif step == 5:
            self._log(tr("导出完成"), "info")
            msg = "\n".join(f"{k}: {v}" for k, v in (result or {}).items())
            QMessageBox.information(self, tr("导出完成"), msg or tr("已导出"))

    def _on_step_failed(self, message: str) -> None:
        self._set_busy(False)
        self._log(message, "error")
        QMessageBox.critical(self, tr("步骤失败"), message)

    def _set_busy(self, busy: bool) -> None:
        self._btn_run.setEnabled(not busy)
        if hasattr(self, "_btn_run_top"):
            self._btn_run_top.setEnabled(not busy)
        self._btn_new.setEnabled(not busy)
        self._btn_open.setEnabled(not busy)
        self._btn_save.setEnabled(not busy)

    def _export_dir(self) -> Path:
        return Path(self._output_edit.text().strip() or str(DEFAULT_OUTPUT_DIR)) / "export"

    # ------------------------------------------------------------------ #
    # 时间轴事件
    # ------------------------------------------------------------------ #
    def _on_frame_selected(self, index: int) -> None:
        self._current = index
        self._play_index = max(0, min(index, max(0, len(self._session.frames) - 1)))
        self._refresh_editor()
        self._refresh_preview()
        self._update_status()

    def _on_editor_edited(self) -> None:
        if self._session.frames and 0 <= self._current < len(self._session.frames):
            self._session.frames[self._current] = self._editor.frame()
            self._timeline.update_thumbnail(self._current, self._session.frames[self._current])
            self._mark_dirty()
            if not self._playing:
                self._refresh_preview()
        elif self._editing_first_frame:
            # 帧列表还空着时编辑器编辑的是首帧图：写回首帧，动画步骤才会用上改后的图
            self._session.first_frame = self._editor.frame().copy()
            self._mark_dirty()
            self._refresh_preview()
            self._refresh_first_frame_row()

    def _on_insert_frame(self) -> None:
        if not self._session.frames:
            self._on_add_frame()
            return
        img = self._session.frames[max(0, min(self._current, len(self._session.frames) - 1))].copy()
        self._session.insert_frame(self._current, img)
        self._mark_dirty()
        self._refresh_all()

    def _on_duplicate_frame(self) -> None:
        if not self._session.frames:
            return
        idx = max(0, min(self._current, len(self._session.frames) - 1))
        self._session.duplicate_frame(idx)
        self._current = idx + 1
        self._mark_dirty()
        self._refresh_all()

    def _on_delete_frame(self) -> None:
        if len(self._session.frames) <= 1:
            QMessageBox.information(self, tr("提示"), tr("至少保留一帧"))
            return
        idx = max(0, min(self._current, len(self._session.frames) - 1))
        self._session.delete_frame(idx)
        self._current = max(0, min(idx, len(self._session.frames) - 1))
        self._mark_dirty()
        self._refresh_all()

    def _on_add_frame(self) -> None:
        size = self._session.frames[0].size if self._session.frames else self._session.target_size()
        self._session.frames.append(Image.new("RGBA", size, (0, 0, 0, 0)))
        self._current = len(self._session.frames) - 1
        self._mark_dirty()
        self._refresh_all()

    def _current_source_image(self):
        """「当前图」：优先时间轴选中帧，其次首帧图，再其次参考图。"""
        s = self._session
        if s.frames:
            idx = max(0, min(self._current, len(s.frames) - 1))
            return s.frames[idx], tr("当前帧")
        if s.first_frame is not None:
            return s.first_frame, tr("首帧图")
        if s.reference_image is not None:
            return s.reference_image, tr("参考图")
        return None, ""

    def _on_add_current_frame(self) -> None:
        """把「当前图」追加到帧列表（生图完成后想把它变成第 1 帧，就点这里）。

        旧版只有「+ 空白帧」，生成完首帧图后没有任何入口把它放进帧列表，
        这正是「完成生图后无法添加到序列帧列表」的原因。
        """
        img, source = self._current_source_image()
        if img is None:
            self._on_add_frame()
            self._log(tr("暂无图片可添加（先生成首帧图片 / 导入图片 / 添加空白帧）"), "warn")
            return
        frame = img.convert("RGBA").copy()
        if self._session.frames:
            # 尺寸与既有帧对齐，避免混入不同画布尺寸
            base = self._session.frames[0].size
            if frame.size != base:
                frame = frame.resize(base, Image.Resampling.NEAREST)
        self._session.frames.append(frame)
        self._current = len(self._session.frames) - 1
        self._mark_dirty()
        self._refresh_all()
        self._log(tr("已把{0}添加为第 {1} 帧").format(source, len(self._session.frames)), "info")

    def _set_first_frame_from_current(self) -> None:
        """把时间轴当前选中的帧设为动画生成的首帧图。"""
        if not self._session.frames:
            return
        idx = max(0, min(self._current, len(self._session.frames) - 1))
        self._session.first_frame = self._session.frames[idx].convert("RGBA").copy()
        self._editing_first_frame = False
        self._mark_dirty()
        self._refresh_all()
        self._log(tr("已把第 {0} 帧设为动画生成的首帧图").format(idx + 1), "info")

    def _on_reordered(self, order: List[int]) -> None:
        if len(order) != len(self._session.frames):
            return
        # Qt InternalMove 已把选中项跟随到新位置，先记录再重建
        new_sel = self._timeline.current_index()
        self._session.frames = [self._session.frames[i] for i in order]
        self._current = new_sel if new_sel >= 0 else 0
        self._mark_dirty()
        self._refresh_all()

    # ------------------------------------------------------------------ #
    # 播放
    # ------------------------------------------------------------------ #
    def _on_toggle_play(self) -> None:
        if self._playing:
            self._playing = False
            self._play_timer.stop()
        else:
            if not self._session.frames:
                return
            self._playing = True
            self._play_index = self._current
            interval = max(30, int(1000 / (max(1, self._session.fps) * self._preview_speed())))
            self._play_timer.start(interval)
        self._update_play_button()

    def _on_play_tick(self) -> None:
        n = len(self._session.frames)
        if n:
            self._play_index = (self._play_index + 1) % n
            self._show_frame(self._session.frames[self._play_index])

    # ------------------------------------------------------------------ #
    # 项目
    # ------------------------------------------------------------------ #
    def _on_new(self) -> None:
        if self._dirty and not self._confirm_discard():
            return
        self._session = IdeSession(output_dir=Path(self._output_edit.text().strip() or str(DEFAULT_OUTPUT_DIR)))
        self._current = 0
        self._play_index = 0
        self._dirty = False
        self._editing_first_frame = False
        self._reset_steps_done()
        self._stop_play()
        self._refresh_all()
        self._log(tr("已新建工作区"), "info")

    def _on_open_project(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, tr("打开 IDE 项目"), "", tr("IDE 项目 (*.json);;所有文件 (*)"))
        if not path:
            return
        try:
            self._session = load_ide_project(Path(path).parent)
            self._current = 0
            self._play_index = 0
            self._dirty = False
            self._editing_first_frame = False
            # 按已恢复的产物推断步骤进度，打开项目后步骤条不该是空的
            self._reset_steps_done()
            if self._session.prompts:
                self._mark_step_done(0)
            if self._session.first_frame is not None:
                self._mark_step_done(1)
            if self._session.frames:
                self._mark_step_done(2)
            self._load_session_to_form()
            self._refresh_all()
            self._log(tr("已打开项目：{0}").format(path), "info")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, tr("打开失败"), str(exc))

    def _on_save_project(self) -> None:
        self._sync_session()
        base = self._session.output_dir
        path = QFileDialog.getExistingDirectory(self, tr("选择项目保存目录"), str(base))
        if not path:
            return
        try:
            proj_dir = Path(path)
            # 直接存到所选目录（而非嵌套 untitled 子目录）
            if not self._session.name or self._session.name in ("untitled", "project"):
                self._session.name = proj_dir.name or "project"
            save_ide_project(self._session, proj_dir)
            self._dirty = False
            self._update_status()
            self._log(tr("项目已保存到：{0}").format(proj_dir), "info")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, tr("保存失败"), str(exc))

    def _confirm_discard(self) -> bool:
        return QMessageBox.question(self, tr("未保存"), tr("当前工作区有未保存修改，确定丢弃吗？")) == QMessageBox.StandardButton.Yes

    # ------------------------------------------------------------------ #
    # 参考图 / 首帧图
    # ------------------------------------------------------------------ #
    def _on_ref_changed(self, img) -> None:
        """参考图卡片变更：设置图生图参考；首帧为空时同时作为首帧图。"""
        if img is not None:
            self._session.reference_image = img
            if self._session.first_frame is None:
                self._session.first_frame = img.copy()
            self._log(tr("已添加参考图（图生图 + 首帧图）"), "info")
        else:
            self._session.reference_image = None
            self._log(tr("已移除参考图"), "info")
        self._refresh_preview()
        self._refresh_first_frame_row()
        self._mark_dirty()

    # ------------------------------------------------------------------ #
    # 从 Solo 同步
    # ------------------------------------------------------------------ #
    def set_first_frame(self, img) -> None:
        """外部导入图片作为首帧 + 图生图参考（像素板块同步用）。"""
        self._session.first_frame = img.convert("RGBA")
        self._session.reference_image = img.convert("RGBA")
        self._current = 0
        self._play_index = 0
        self._mark_dirty()
        self._refresh_all()
        self._tabs.setCurrentIndex(0)
        self._log(tr("已导入图片（首帧 + 图生图参考），可直接走「动画生成」步骤"), "info")

    def import_from_solo(self, result: SoloResult) -> None:
        """把 Solo 生成的首帧图与最终帧序列同步到 IDE 工作区。"""
        s = self._session
        # 首帧图
        if getattr(result, "first_frame", None) and Path(result.first_frame).exists():
            s.first_frame = fu.load_image(result.first_frame)
        # 最终帧序列（优先 frames_dir，其次 png_dir）
        src = getattr(result, "frames_dir", None) or getattr(result, "png_dir", None)
        frames: List[Image.Image] = []
        if src and Path(src).exists():
            frames = [fu.load_image(p) for p in sorted(Path(src).glob("*.png"))]
        if frames:
            s.frames = frames
            s.fps = int(result.fps or s.fps)
            s.frame_count = len(frames)
            w, h = frames[0].size
            s.aspect_ratio = self._aspect_from_size(w, h)
            s.pixel_size = max(w, h)
        self._current = 0
        self._play_index = 0
        self._dirty = True
        self._refresh_all()
        self._tabs.setCurrentIndex(0)  # 切到预览
        self._log(
            tr("已从 Solo 同步：{0} 帧").format(len(frames)) + (tr("（含首帧图）") if s.first_frame is not None else ""),
            "info",
        )

    def import_from_sprite(self, result) -> None:
        """把精灵图生成的结果（底图 + 帧序列）同步到 IDE 工作区。"""
        s = self._session
        if getattr(result, "base_image", None) and Path(result.base_image).exists():
            s.first_frame = fu.load_image(result.base_image)
        src = getattr(result, "frames_dir", None)
        frames: List[Image.Image] = []
        if src and Path(src).exists():
            frames = [fu.load_image(p) for p in sorted(Path(src).glob("*.png"))]
        if frames:
            s.frames = frames
            s.frame_count = len(frames)
            w, h = frames[0].size
            s.aspect_ratio = self._aspect_from_size(w, h)
            s.pixel_size = max(w, h)
        self._current = 0
        self._play_index = 0
        self._dirty = True
        self._refresh_all()
        self._tabs.setCurrentIndex(0)
        self._log(tr("已从精灵图同步：{0} 帧").format(len(frames)) + (tr("（含对象底图）") if s.first_frame is not None else ""), "info")

    @staticmethod
    def _aspect_from_size(w: int, h: int) -> str:
        """按宽高反向匹配最接近的预设比例。"""
        from config.settings import ASPECT_RATIOS

        best, best_err = "1:1", float("inf")
        for name, (rw, rh) in ASPECT_RATIOS.items():
            err = abs(rw / rh - w / max(1, h))
            if err < best_err:
                best_err, best = err, name
        return best

    def _stop_play(self) -> None:
        self._playing = False
        self._play_timer.stop()
        self._update_play_button()

    # ------------------------------------------------------------------ #
    # 其他
    # ------------------------------------------------------------------ #
    def _on_browse_output(self) -> None:
        path = QFileDialog.getExistingDirectory(self, tr("选择输出目录"), self._output_edit.text())
        if path:
            self._output_edit.setText(path)

    def _on_open_output(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._output_edit.text().strip() or str(DEFAULT_OUTPUT_DIR))))

    def _log(self, message: str, level: str = "info") -> None:
        color = _LOG_COLORS.get(level, "#8b949e")
        safe = message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        self._log_view.appendHtml(f'<span style="color:{color};">[{level.upper()}] {safe}</span>')

    # ------------------------------------------------------------------ #
    # 键盘快捷键（设置 → 快捷键 → IDE 可自定义；固定使用 ide 键位）
    # ------------------------------------------------------------------ #
    def keyPressEvent(self, event) -> None:  # noqa: N802
        from ui import shortcuts as sc

        if sc.match(event, sc.get("preview_play", "ide")):
            self._on_toggle_play()
            event.accept()
            return
        if sc.match(event, sc.get("preview_fit", "ide")):
            self._preview.reset_zoom()
            event.accept()
            return
        if sc.match(event, sc.get("timeline_insert", "ide")):
            self._on_insert_frame()
            event.accept()
            return
        if sc.match(event, sc.get("timeline_duplicate", "ide")):
            self._on_duplicate_frame()
            event.accept()
            return
        if sc.match(event, sc.get("timeline_delete", "ide")):
            self._on_delete_frame()
            event.accept()
            return
        super().keyPressEvent(event)

    # ------------------------------------------------------------------ #
    # 语言切换：刷新常驻下拉框项（动作预设分组 + 倍速等文本）
    # ------------------------------------------------------------------ #
    def retranslate_ui(self) -> None:
        populate_action_combo(self._action_combo)
        # 步骤条（✓/▶ 记号 + 步骤文案）与主执行按钮随语言重刷
        bar = getattr(self, "_step_bar", None)
        if bar is not None:
            bar.retranslate_ui()
        if hasattr(self, "_btn_run_top"):
            self._btn_run_top.setText(tr(STEP_ACTIONS_ZH[max(0, min(self._current_step, len(STEP_ACTIONS_ZH) - 1))]))
        if hasattr(self, "_first_frame_label"):
            self._refresh_first_frame_row()
        # 编辑器里的动态提示（色族色块、对称/环绕开关）也要跟着换语言
        editor = getattr(self, "_editor", None)
        if editor is not None and hasattr(editor, "retranslate_ui"):
            editor.retranslate_ui()
        # 参数栏三角钮的图标/提示随语言重刷
        self._apply_params_collapsed()
        current = self._preview_speed_combo.currentData()
        self._preview_speed_combo.blockSignals(True)
        self._preview_speed_combo.clear()
        for label, value in [("0.5x", 0.5), ("1x（原速）", 1.0), ("1.5x", 1.5), ("2x", 2.0), ("3x", 3.0)]:
            self._preview_speed_combo.addItem(tr(label), userData=value)
        idx = self._preview_speed_combo.findData(current)
        self._preview_speed_combo.setCurrentIndex(idx if idx >= 0 else 1)
        self._preview_speed_combo.blockSignals(False)
        # 状态行/时间轴提示是「帧 {cur}/{n} · {w}×{h}」这类拼接文案，需重算
        self._update_status()
        hint = getattr(self._timeline, "retranslate_ui", None)
        if callable(hint):
            hint()
