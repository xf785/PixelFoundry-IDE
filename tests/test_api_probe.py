"""接口探测对话框测试（注入假 probe_fn，不联网）。

覆盖：
- 构造不联网（probe_fn 只应在点「发送测试请求」时被调用）；
- 请求预览区展示方法/URL/请求头（Key 已打码）；
- 成功响应 → 建议行出现、值预览正确、点「使用」回调 (字段, 路径)；
- 失败响应 → 显示 HTTP 状态与错误信息，且提示「未检测到可用字段」。
"""
import httpx
from PySide6.QtWidgets import QLabel

from core.api.base import APIResult
from core.api.video_api import VideoAPI
from ui.dialogs.api_probe_dialog import ApiProbeDialog

BODY = {"data": {"task_id": "abc", "status": "running", "video": {"url": "https://x/v.mp4"}}}


def _video_api(body=None, status_code: int = 200, text: str = "") -> VideoAPI:
    def handler(request: httpx.Request) -> httpx.Response:
        if text:
            return httpx.Response(status_code, text=text)
        return httpx.Response(status_code, json=body)

    cfg = {
        "base_url": "http://video.local/v1",
        "api_key": "secret-key",
        "model": "m",
        "params": {"provider": "generic", "timeout": 5, "max_retries": 0},
    }
    return VideoAPI(cfg, transport=httpx.MockTransport(handler))


def _preview(api: VideoAPI) -> dict:
    return api.preview_request(b"\x89PNG", "a small red cube rotating", frames=8, fps=8)


def test_dialog_constructs_without_calling_probe(qtbot):
    api = _video_api(BODY)
    calls = {"n": 0}

    def probe_fn():
        calls["n"] += 1
        return APIResult(ok=True, data={})

    dialog = ApiProbeDialog("video", _preview(api), probe_fn)
    qtbot.addWidget(dialog)
    assert calls["n"] == 0, "构造对话框不应发起探测"
    text = dialog._request_view.toPlainText()
    assert "POST" in text and "http://video.local/v1/videos/generations" in text
    assert "Bearer ***" in text and "secret-key" not in text
    assert dialog._btn_probe.text() == "发送测试请求"


def test_dialog_lists_suggestions_and_applies_field(qtbot):
    api = _video_api(BODY)
    dialog = ApiProbeDialog("video", _preview(api), lambda: api.probe(b"\x89PNG", "p"))
    qtbot.addWidget(dialog)

    applied = []
    dialog.field_applied.connect(lambda key, path: applied.append((key, path)))
    dialog._on_probe()          # 相当于点击「发送测试请求」

    # 响应区显示 JSON、状态行显示成功
    assert "abc" in dialog._response_view.toPlainText()
    assert "200" in dialog._response_view.toPlainText()
    assert "✓" in dialog._status.text()

    buttons = dialog.suggestion_buttons()
    labels = [b.property("field_key") for b in buttons]
    assert labels.count("job_id_path") >= 1
    assert "status_path" in labels
    assert "result_video_url_path" in labels

    # 值预览：路径旁边应显示当前响应里取到的值
    previews = [w.text() for w in dialog.findChildren(QLabel)]
    assert "https://x/v.mp4" in previews
    assert "running" in previews

    target = next(b for b in buttons if b.property("path") == "data.task_id")
    target.click()
    assert applied == [("job_id_path", "data.task_id")]
    assert "已写入" in dialog._status.text()


def test_dialog_reports_failure_without_suggestions(qtbot):
    api = _video_api(status_code=500, text="upstream boom")
    dialog = ApiProbeDialog("video", _preview(api), lambda: api.probe(b"\x89PNG", "p"))
    qtbot.addWidget(dialog)

    dialog._on_probe()
    assert "500" in dialog._response_view.toPlainText()
    assert "upstream boom" in dialog._response_view.toPlainText()
    assert "✗" in dialog._status.text()
    assert dialog.suggestion_buttons() == []
    assert "未检测到可用字段" in dialog._suggest_hint.text()


def test_dialog_survives_probe_exception(qtbot):
    """probe_fn 抛异常时对话框也不能崩（兜底显示错误）。"""

    def boom():
        raise RuntimeError("probe exploded")

    api = _video_api(BODY)
    dialog = ApiProbeDialog("video", _preview(api), boom)
    qtbot.addWidget(dialog)
    dialog._on_probe()
    assert "probe exploded" in dialog._status.text()
    assert dialog._btn_probe.isEnabled()


def test_dialog_shows_placeholder_without_probe_fn(qtbot):
    api = _video_api(BODY)
    dialog = ApiProbeDialog("video", _preview(api), lambda: None)
    qtbot.addWidget(dialog)
    assert "点「发送测试请求」" in dialog._suggest_hint.text()


def test_curl_import_dialog_smoke(qtbot):
    from ui.widgets.api_config_widget import CurlImportDialog

    dialog = CurlImportDialog()
    qtbot.addWidget(dialog)
    assert dialog.windowTitle() == "从 curl 导入"
    dialog.set_text("curl 'https://x/v1/a' -d '{\"prompt\":\"p\"}'")
    assert dialog.text().startswith("curl")


# --------------------------------------------------------------------------- #
# 配置控件里的中转站辅助（不弹窗的部分）
# --------------------------------------------------------------------------- #
def _video_widget(tmp_path):
    from config.api_config import APIConfig, APIConfigManager
    from core.storage.keyring import Keyring
    from ui.widgets.api_config_widget import ApiConfigWidget

    manager = APIConfigManager(config_file=tmp_path / "api.json", keyring=Keyring(tmp_path / ".keyring"))
    manager.add(
        APIConfig(
            kind="video", name="relay", base_url="http://video.local/v1", api_key="secret-key",
            model="m", params={"provider": "generic"},
        )
    )
    return ApiConfigWidget(manager, "video")


def test_video_widget_has_relay_buttons(qtbot, tmp_path):
    widget = _video_widget(tmp_path)
    qtbot.addWidget(widget)
    assert widget._btn_preview.text() == "预览请求…"
    assert widget._btn_probe.text() == "测试并检测字段…"
    assert widget._btn_curl.text() == "从 curl 导入…"
    assert not widget._btn_preview.isHidden()


def test_llm_widget_hides_video_only_buttons(qtbot, tmp_path):
    from config.api_config import APIConfigManager
    from core.storage.keyring import Keyring
    from ui.widgets.api_config_widget import ApiConfigWidget

    manager = APIConfigManager(config_file=tmp_path / "api.json", keyring=Keyring(tmp_path / ".keyring"))
    widget = ApiConfigWidget(manager, "llm")
    qtbot.addWidget(widget)
    assert widget._btn_preview.isHidden() and widget._btn_probe.isHidden()
    assert widget._btn_curl.text() == "从 curl 导入…"


def test_test_frame_is_a_256_png():
    from PIL import Image

    import io as _io

    from ui.widgets.api_config_widget import _TEST_FRAME_SIDE, ApiConfigWidget

    data = ApiConfigWidget._test_frame_png()
    image = Image.open(_io.BytesIO(data))
    assert image.size == (_TEST_FRAME_SIDE, _TEST_FRAME_SIDE)
    assert data.startswith(b"\x89PNG")


def test_widget_builds_preview_from_form(qtbot, tmp_path):
    """「预览请求…」用的素材：合成帧 + 客户端 preview_request（不联网）。"""
    widget = _video_widget(tmp_path)
    qtbot.addWidget(widget)
    client = widget._make_client_or_warn()
    assert client is not None
    try:
        preview, frame = widget._build_preview_frame(client)
    finally:
        client.close()
    assert preview["method"] == "POST"
    assert preview["url"] == "http://video.local/v1/videos/generations"
    assert preview["headers"]["Authorization"] == "Bearer ***"
    assert frame and preview["body"]["image"].startswith("data:image/png;base64,")


def test_apply_detected_field_writes_form_and_client(qtbot, tmp_path):
    """「使用」建议路径：写回表单字段并同步到客户端（保存后持久化）。"""
    widget = _video_widget(tmp_path)
    qtbot.addWidget(widget)
    client = widget._make_client_or_warn()
    try:
        widget._apply_detected_field(client, "job_id_path", "data.task_id")
        assert widget._get_field("job_id_path") == "data.task_id"
        assert "已写入" in widget._test_result.text()
        # 同步到客户端后，继续探测会用新的路径
        assert client._find_job_id({"data": {"task_id": "t-1"}}) == "t-1"
        # 表单值会随 _collect_config 一起保存
        assert widget._collect_config().params["job_id_path"] == "data.task_id"
    finally:
        client.close()


def test_curl_import_fills_form_fields(qtbot, tmp_path):
    """从 curl 填表：Base URL / 提交端点 / 额外请求头 / 请求体模板。"""
    from core.api.curl_import import parse_curl, to_params

    widget = _video_widget(tmp_path)
    qtbot.addWidget(widget)
    imported = parse_curl(
        "curl 'https://relay.example.com/v1/videos/generations' "
        "-H 'x-api-key: sk-1' -H 'content-type: application/json' "
        "--data-raw '{\"model\":\"m\",\"prompt\":\"a cat\"}'"
    )
    widget._fill_from_curl(imported, to_params(imported))
    assert widget._get_field("base_url") == "https://relay.example.com"
    assert widget._get_field("submit_url") == "https://relay.example.com/v1/videos/generations"
    assert widget._get_field("submit_method") == "POST"
    assert '"x-api-key"' in widget._get_field("extra_headers")
    assert "$prompt" in widget._get_field("payload_template")
    # 保存后落在配置里
    widget._on_save()
    cfg = widget._api.get_default("video")
    assert cfg.base_url == "https://relay.example.com"
    assert cfg.params["provider"] == "custom"


def test_import_curl_button_flow(qtbot, tmp_path, monkeypatch):
    """「从 curl 导入…」完整流程：解析 → 填表 → 弹注意事项。"""
    from PySide6.QtWidgets import QDialog, QMessageBox

    from ui.widgets import api_config_widget as mod

    widget = _video_widget(tmp_path)
    qtbot.addWidget(widget)
    messages = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: messages.append(a[2]))

    def fake_exec(self):
        self.set_text(
            "curl 'https://relay.example.com/v1/videos/generations' -H 'x-api-key: sk-1' "
            "-d '{\"prompt\":\"p\",\"model\":\"m\"}'"
        )
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(mod.CurlImportDialog, "exec", fake_exec)
    widget._on_import_curl()
    assert widget._get_field("submit_url").endswith("/v1/videos/generations")
    assert "$prompt" in widget._get_field("payload_template")
    assert messages and "curl" in messages[0]
