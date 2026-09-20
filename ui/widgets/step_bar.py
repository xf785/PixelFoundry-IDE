"""横向步骤条：一眼看清「流水线走到哪一步」，点一下就切过去。

IDE 模式的 6 个步骤原先只藏在主窗口侧栏里，页面自身看不出进度，也不知道下一步该干什么。
本控件把步骤平铺成一行状态片：

    ① 生成提示词  →  ② 生成首帧图片  →  …

每个片前的记号表示状态：`✓` 已完成、`▶` 当前、`○` 未开始；点击即切换步骤（发 step_selected）。
文案用 `tr()` 现算，`retranslate_ui()` 可在语言切换后刷新。
"""
from __future__ import annotations

from typing import Iterable, List, Optional, Set

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QWidget

from ui.i18n import tr
from ui.layout import scaled

MARK_DONE = "✓"
MARK_CURRENT = "▶"
MARK_TODO = "○"


class StepBar(QWidget):
    """步骤进度条（可点击切换）。"""

    step_selected = Signal(int)

    def __init__(self, labels: Iterable[str], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("StepBar")
        self._labels: List[str] = list(labels)
        self._done: Set[int] = set()
        self._current = 0
        self._buttons: List[QPushButton] = []

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(scaled(6))
        for index in range(len(self._labels)):
            btn = QPushButton()
            btn.setObjectName("StepChip")
            btn.setCheckable(True)
            btn.setAutoExclusive(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setMinimumHeight(scaled(28))
            btn.clicked.connect(lambda _=False, i=index: self.step_selected.emit(i))
            row.addWidget(btn)
            self._buttons.append(btn)
        row.addStretch(1)
        self._refresh()

    # ------------------------------------------------------------------ #
    def set_current(self, index: int) -> None:
        """设置当前步骤（越界自动收敛）。"""
        if not self._buttons:
            return
        self._current = max(0, min(int(index), len(self._buttons) - 1))
        self._refresh()

    def current(self) -> int:
        return self._current

    def set_done(self, done: Iterable[int]) -> None:
        """设置已完成步骤集合。"""
        self._done = {int(i) for i in done}
        self._refresh()

    def mark_done(self, index: int) -> None:
        self._done.add(int(index))
        self._refresh()

    def clear_done(self) -> None:
        self._done.clear()
        self._refresh()

    def is_done(self, index: int) -> bool:
        return int(index) in self._done

    def retranslate_ui(self) -> None:
        self._refresh()

    # ------------------------------------------------------------------ #
    def _refresh(self) -> None:
        for index, btn in enumerate(self._buttons):
            if index in self._done:
                mark, state = MARK_DONE, "done"
            elif index == self._current:
                mark, state = MARK_CURRENT, "current"
            else:
                mark, state = MARK_TODO, "todo"
            btn.setText(f"{mark} {index + 1}. {tr(self._labels[index])}")
            btn.setChecked(index == self._current)
            if btn.property("stepState") != state:
                btn.setProperty("stepState", state)
                btn.style().unpolish(btn)
                btn.style().polish(btn)
        self.setMinimumHeight(scaled(30))
        self.setMaximumHeight(scaled(40))

    def sizeHint(self) -> QSize:  # noqa: N802
        hint = super().sizeHint()
        return QSize(hint.width(), scaled(32))
