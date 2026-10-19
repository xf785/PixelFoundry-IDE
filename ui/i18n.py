"""中英文适配：轻量 i18n（ID 序列 + 语言包解耦架构）。

设计：
- 所有界面文案以「中文原文」为稳定 ID（即文案序列本身）；
- 每个语言是一个 {id: text} 语言包（LANG_PACKS / ui/lang/xx.py）；
- tr(id) 按当前语言取词：中文直接返回 ID（默认包为空），
  其它语言查对应语言包，未收录回退中文（便于增量迁移）；
- 新增语言：复制任意语言包按序翻译，注册到 LANG_PACKS，
  并（可选）放入 ui/lang/xx.py 让 _load_pack 自动加载；设置页语言下拉
  自动读取 available_languages()。

用法：
    from ui.i18n import tr
    btn.setText(tr("开始生成"))
    btn = T(QPushButton(), "开始生成")   # 注册后可立即重译
"""
from __future__ import annotations

_LANG: str = "zh"  # 当前语言（LANG_PACKS 的 key）

# 语言包注册表：{语言代码: {id: text}}。zh 为默认（ID 即原文，包可为空）。
# 语言包体积很大（1100+ 条），en 单独放在 ui/lang/en.py —— 这里只保留注册表与查表逻辑。
from .lang.en import PACK as _EN_PACK

LANG_PACKS: dict = {
    "zh": {},
    "en": _EN_PACK,
}

# 语言显示名（新增语言包时在此补充）
_LANG_NAMES = {"zh": "中文", "en": "English"}


def _load_pack(name: str) -> dict:
    """从 ui/lang/<name>.py 自动加载语言包（STRINGS: {id: text}），失败则用注册表。"""
    pack = dict(LANG_PACKS.get(name, {}))
    try:
        mod = __import__(f"ui.lang.{name}", fromlist=["STRINGS"])
        pack.update(dict(getattr(mod, "STRINGS", {})))
    except ImportError:
        pass
    return pack


def available_languages() -> list:
    """可用语言列表 [(code, 显示名)]：注册表 + ui/lang/ 目录下的包。"""
    langs = [("zh", _LANG_NAMES.get("zh", "中文"))]
    for code in LANG_PACKS:
        if code == "zh":
            continue
        langs.append((code, _LANG_NAMES.get(code, code)))
    from pathlib import Path

    lang_dir = Path(__file__).resolve().parent / "lang"
    if lang_dir.is_dir():
        for p in sorted(lang_dir.glob("*.py")):
            if p.name.startswith("_") or p.stem in ("zh",) or p.stem in dict(langs):
                continue
            langs.append((p.stem, _LANG_NAMES.get(p.stem, p.stem)))
    return langs


def set_language(lang: str) -> None:
    """设置语言（zh / en / 其它语言包 key）；未知语言回退中文。"""
    global _LANG, _REVERSE_LANG
    lang = str(lang or "zh").lower()
    if lang in LANG_PACKS or lang == "zh":
        _LANG = lang
    else:
        _LANG = "zh"
    _REVERSE_LANG = ""      # 让反向表跟着语言重建


def language() -> str:
    return _LANG


def tr(text: str) -> str:
    """按当前语言翻译；未收录的 ID 回退中文原文（中文即默认包）。"""
    if _LANG == "zh":
        return text
    pack = _load_pack(_LANG)
    return pack.get(text, text)


# --------------------------------------------------------------------------- #
# 反向表：把「已经是译文」的文案还原成中文 ID
# --------------------------------------------------------------------------- #
# 有些地方会把 tr() 的结果再交给 T()（例如 _icon_btn(kind, tr("…"))）。
# 若界面在英文状态下构建，T() 就会把英文当 ID 注册进表，切回中文再也还原不回来。
# 这里在 T() 里做一次还原：拿到的是译文就换回它对应的中文 ID。
_REVERSE: dict = {}
_REVERSE_LANG: str = ""


def canonical_id(text: str) -> str:
    """把当前语言的译文还原为中文 ID（不是译文则原样返回）。"""
    global _REVERSE, _REVERSE_LANG
    if _LANG == "zh" or not text:
        return text
    if _REVERSE_LANG != _LANG:
        pack = _load_pack(_LANG)
        _REVERSE = {}
        for key, value in pack.items():
            if isinstance(value, str) and value and value not in _REVERSE:
                _REVERSE[value] = key
        _REVERSE_LANG = _LANG
    return _REVERSE.get(text, text)


def translations_of(key: str) -> set:
    """某个中文 ID 在所有语言包里的写法（含中文原文），用于识别「未被用户改过的默认值」。"""
    out = {key}
    for code, pack in LANG_PACKS.items():
        if code == "zh":
            continue
        value = pack.get(key)
        if isinstance(value, str) and value:
            out.add(value)
    return out


_ALL_REVERSE: dict = {}


def _all_reverse() -> dict:
    """所有语言包的「译文 -> 中文 ID」总表（用于兜底重译，见 retranslate_tree）。"""
    global _ALL_REVERSE
    if not _ALL_REVERSE:
        table: dict = {}
        for code, pack in LANG_PACKS.items():
            if code == "zh":
                continue
            for key, value in pack.items():
                if isinstance(value, str) and value and value not in table:
                    table[value] = key
        _ALL_REVERSE = table
    return _ALL_REVERSE


# --------------------------------------------------------------------------- #
# 立即重译：T() 设置文本并注册，语言切换后 retranslate_all() 全局重刷。
# --------------------------------------------------------------------------- #
_REGISTRY: list = []  # (widget, attr, zh, index)


def T(widget, zh_text, attr: str = "text", index: int = None):
    """设置控件文本并注册（attr: text / tooltip / placeholder / tab）。

    widget=None 时仅返回翻译文本（等价 tr）；否则设置文本、注册并返回控件本身，
    便于链式创建：btn = T(QPushButton(), "开始生成"); f.addRow(T(QLabel(), "帧数"), spin)。

    传入的文案若已经是当前语言的译文（调用方多写了一次 ``tr()``，或控件是在英文
    状态下构建的），会自动还原成中文 ID 再注册，避免「切回中文还剩英文」。
    """
    zh_text = canonical_id(str(zh_text))
    if widget is None:
        return tr(zh_text)
    _apply_text(widget, attr, index, tr(zh_text))
    _REGISTRY.append((widget, attr, zh_text, index))
    return widget


def _apply_text(widget, attr: str, index, text: str) -> None:
    try:
        if attr == "text":
            if hasattr(widget, "setTitle") and not hasattr(widget, "setText"):
                widget.setTitle(text)  # QGroupBox 等用 setTitle
            else:
                widget.setText(text)
        elif attr == "tooltip":
            widget.setToolTip(text)
        elif attr == "placeholder":
            widget.setPlaceholderText(text)
        elif attr == "tab":
            widget.setTabText(index, text)
    except RuntimeError:
        pass


def retranslate_all() -> None:
    """按当前语言重刷所有已注册文本（语言切换后立即生效）。

    除了 T() 注册表，还会**整棵控件树再扫一遍**（含所有顶层对话框）：
    不少文案是构建时用 tr() 直接 setText/setToolTip/setPlaceholderText 设上去的、
    没有登记到注册表；只刷注册表的话，在英文状态下构建、再切回中文就会残留英文。
    兜底规则：把「当前文本是某个已知译文」的标签/按钮/提示/占位/标题/c类下拉项
    还原成中文 ID 再译一遍；**用户输入（QLineEdit 文本、日志正文、列表/树条目）不动**。
    """
    for widget, attr, zh, index in list(_REGISTRY):
        _apply_text(widget, attr, index, tr(zh))
    retranslate_tree()


def retranslate_tree(root=None) -> int:
    """遍历控件树重译界面文案，返回被改写的条目数（详见 retranslate_all 注释）。"""
    from PySide6.QtWidgets import (
        QAbstractButton,
        QApplication,
        QComboBox,
        QGroupBox,
        QLabel,
        QLineEdit,
        QPlainTextEdit,
        QTabWidget,
        QTextEdit,
        QWidget,
    )

    reverse = _all_reverse()
    if not reverse:
        return 0
    changed = 0
    # 已在注册表里的（T() 登记的）文案由注册表那一轮精确重译，这里跳过，
    # 免得被「同义译文」的反向映射覆盖成另一个近义中文（如 界面布局比例 -> 界面比例）
    registered = {(id(w), attr) for w, attr, _zh, _i in list(_REGISTRY)}

    def fix(widget, attr: str, getter, setter) -> None:
        nonlocal changed
        if (id(widget), attr) in registered:
            return
        try:
            current = getter()
        except RuntimeError:
            return
        if not isinstance(current, str) or not current.strip():
            return
        key = reverse.get(current)
        if key is None:
            return
        new = tr(key)
        if new == current:
            return
        try:
            setter(new)
        except RuntimeError:
            return
        changed += 1

    roots = []
    if root is not None:
        roots.append(root)
    else:
        roots.extend(w for w in QApplication.topLevelWidgets() if w.isVisible())

    seen = set()
    for top in roots:
        for w in [top] + top.findChildren(QWidget):
            if id(w) in seen:
                continue
            seen.add(id(w))
            # 文本类控件（不含用户输入框的正文）
            if isinstance(w, QLabel):
                fix(w, "text", w.text, w.setText)
            elif isinstance(w, QAbstractButton):
                fix(w, "text", w.text, w.setText)
            elif isinstance(w, QGroupBox):
                fix(w, "text", w.title, w.setTitle)
            if isinstance(w, (QLineEdit, QTextEdit, QPlainTextEdit)):
                fix(w, "placeholder", w.placeholderText, w.setPlaceholderText)
            elif isinstance(w, QComboBox):
                fix(w, "placeholder", w.placeholderText, w.setPlaceholderText)
                for i in range(w.count()):
                    fix(w, f"item{i}", (lambda i=i, c=w: c.itemText(i)),
                        (lambda text, i=i, c=w: c.setItemText(i, text)))
            if isinstance(w, QTabWidget):
                for i in range(w.count()):
                    fix(w, f"tab{i}", (lambda i=i, t=w: t.tabText(i)),
                        (lambda text, i=i, t=w: t.setTabText(i, text)))
            fix(w, "windowTitle", w.windowTitle, w.setWindowTitle)
            fix(w, "tooltip", w.toolTip, w.setToolTip)
            for act in getattr(w, "actions", lambda: [])():
                fix(act, "text", act.text, act.setText)
                fix(act, "tooltip", act.toolTip, act.setToolTip)
                menu = act.menu()
                if menu is not None:
                    for sub in menu.actions():
                        fix(sub, "text", sub.text, sub.setText)
    return changed
