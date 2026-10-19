"""语言包目录：每种语言一个模块。

两种约定都支持（见 ui/i18n.py 的 `_load_pack`）：

- `PACK: dict = {id: text}` —— 由 ui/i18n.py 直接 import（en 就是这样注册的）；
- `STRINGS: dict = {id: text}` —— `_load_pack` 会自动合并到 LANG_PACKS 之上。

id 为中文原文（稳定 ID），text 为该语言译文。新增语言无需改代码：
加一个文件 + 在 LANG_PACKS/_LANG_NAMES 注册（或只放文件）即可，
设置页「语言」下拉会自动出现该语言。
"""
