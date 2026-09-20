"""端点自适应测试：Base URL 规整、探测结论分类、常见端点探测（httpx.MockTransport，不联网）。

覆盖四块：
1. ``normalize_base_url``：用户把文档里的完整地址粘进 Base URL 时的拆分规则；
2. ``ProbeOutcome.classify``：404 Invalid URL / 400 / 401 / 200 的归类；
3. ``probe_video_endpoints``：候选路径探测、择优、轮询端点推断、服务商建议、
   默认只发 GET（绝不 POST）、allow_post 的 POST 结论、网络异常不抛异常；
4. 结果对话框与设置页按钮的写回流程（Qt，离屏渲染）。
"""
import json
from typing import List

import httpx
import pytest

from core.api.endpoint_probe import (
    POST_PROBE_PAYLOAD,
    SUMMARY_NONE,
    VIDEO_SUBMIT_CANDIDATES,
    EndpointProbeReport,
    ProbeOutcome,
    guess_poll_url_template,
    normalize_base_url,
    probe_video_endpoints,
    provider_for_path,
)

# --------------------------------------------------------------------------- #
# 1) normalize_base_url
# --------------------------------------------------------------------------- #
def test_normalize_plain_host_unchanged():
    parts = normalize_base_url("https://api.x.com")
    assert parts.base_url == "https://api.x.com"
    assert parts.endpoint == ""
    assert parts.changed is False
    assert "干净" in parts.note


def test_normalize_host_with_version_prefix_unchanged():
    parts = normalize_base_url("https://api.x.com/v1")
    assert parts.base_url == "https://api.x.com/v1"
    assert parts.endpoint == ""
    assert parts.changed is False


def test_normalize_full_endpoint_url_is_split():
    """粘贴完整请求地址：端点拆到「提交端点」，/v1 留 Base URL。"""
    parts = normalize_base_url("https://api.x.com/v1/videos/image2video")
    assert parts.base_url == "https://api.x.com/v1"
    assert parts.endpoint == "/videos/image2video"
    assert parts.changed is True
    assert parts.note.startswith("已把末尾的端点路径 /videos/image2video")
    assert "https://api.x.com/v1" in parts.note
    # 拼回去必须等于用户粘贴的地址（信息不丢）
    assert parts.base_url + parts.endpoint == "https://api.x.com/v1/videos/image2video"


def test_normalize_without_scheme_assumes_https():
    parts = normalize_base_url("api.x.com/v1/videos/generations")
    assert parts.base_url == "https://api.x.com/v1"
    assert parts.endpoint == "/videos/generations"
    assert "https://" in parts.note


def test_normalize_strips_trailing_slash_and_query():
    parts = normalize_base_url("  https://api.x.com/v1/?foo=1#bar  ")
    assert parts.base_url == "https://api.x.com/v1"
    assert parts.endpoint == ""
    assert parts.changed is True
    assert "查询参数" in parts.note


def test_normalize_keeps_non_endpoint_path_in_base():
    """路径不像接口端点（自定义前缀）时整段留在 Base URL 里。"""
    parts = normalize_base_url("https://relay.example.com/openai-proxy/v1")
    assert parts.base_url == "https://relay.example.com/openai-proxy/v1"
    assert parts.endpoint == ""
    assert parts.changed is False


def test_normalize_non_api_root_prefix_keeps_whole_path_as_endpoint():
    """前缀不像 API 根路径时：Base URL 只留主机，整段路径进端点（拼接后仍是原地址）。"""
    url = "https://relay.example.com/some/weird/videos/generations"
    parts = normalize_base_url(url)
    assert parts.base_url == "https://relay.example.com"
    assert parts.endpoint == "/some/weird/videos/generations"
    assert parts.base_url + parts.endpoint == url


def test_normalize_ark_and_openai_relay_prefixes_are_api_roots():
    ark = normalize_base_url("https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks")
    assert ark.base_url == "https://ark.cn-beijing.volces.com/api/v3"
    assert ark.endpoint == "/contents/generations/tasks"

    relay = normalize_base_url("https://relay.example.com/openai/v1/videos/generations")
    assert relay.base_url == "https://relay.example.com/openai/v1"
    assert relay.endpoint == "/videos/generations"

    zhipu = normalize_base_url("https://open.bigmodel.cn/api/paas/v4/images/generations")
    assert zhipu.base_url == "https://open.bigmodel.cn/api/paas/v4"
    assert zhipu.endpoint == "/images/generations"


def test_normalize_empty_input():
    parts = normalize_base_url("   ")
    assert parts.base_url == ""
    assert parts.endpoint == ""
    assert "Base URL 为空" in parts.note


# --------------------------------------------------------------------------- #
# 2) ProbeOutcome.classify
# --------------------------------------------------------------------------- #
KLING_404_BODY = json.dumps(
    {"error": {"message": "Invalid URL (POST /v1/videos/image2video)", "type": "invalid_request_error"}}
)


def test_classify_404_invalid_url_is_missing():
    assert ProbeOutcome.classify(404, KLING_404_BODY) == ProbeOutcome.MISSING


def test_classify_400_is_routed():
    assert ProbeOutcome.classify(400, '{"error": {"message": "prompt is required"}}') == ProbeOutcome.ROUTED
    assert ProbeOutcome.classify(422, '{"detail": "missing field"}}') == ProbeOutcome.ROUTED


def test_classify_401_and_403_are_auth():
    assert ProbeOutcome.classify(401, '{"error": {"message": "invalid api key"}}') == ProbeOutcome.AUTH
    assert ProbeOutcome.classify(403, "") == ProbeOutcome.AUTH


def test_classify_200_is_ok():
    assert ProbeOutcome.classify(200, '{"data": []}') == ProbeOutcome.OK
    assert ProbeOutcome.classify(201, "") == ProbeOutcome.OK


def test_classify_405_and_not_found_phrases_are_missing():
    assert ProbeOutcome.classify(405, "") == ProbeOutcome.MISSING
    assert ProbeOutcome.classify(400, "No such route") == ProbeOutcome.MISSING
    assert ProbeOutcome.classify(500, '{"error": {"message": "unknown request url"}}') == ProbeOutcome.MISSING


def test_classify_unknown_without_response():
    assert ProbeOutcome.classify(None, "") == ProbeOutcome.UNKNOWN
    assert ProbeOutcome.classify(500, "<html>gateway error</html>") == ProbeOutcome.UNKNOWN


# --------------------------------------------------------------------------- #
# 3) probe_video_endpoints
# --------------------------------------------------------------------------- #
def _not_found_handler(request: httpx.Request) -> httpx.Response:
    """一切路径都 404 + Invalid URL（中转站上端点不存在时的典型响应）。"""
    return httpx.Response(
        404,
        json={"error": {"message": f"Invalid URL ({request.method} {request.url.path})"}},
    )


def test_probe_all_404_reports_nothing_found():
    report = probe_video_endpoints(
        "https://relay.example.com", api_key="k", transport=httpx.MockTransport(_not_found_handler)
    )
    assert report.best is None
    assert report.submit_url_template == ""
    assert report.poll_url_template == ""
    assert report.summary == SUMMARY_NONE
    assert "没有探测到" in report.summary
    assert len(report.rows) == len(VIDEO_SUBMIT_CANDIDATES)
    assert all(row.get_outcome == ProbeOutcome.MISSING for row in report.rows)


def test_probe_finds_endpoint_returning_400():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/videos/generations":
            return httpx.Response(400, json={"error": {"message": "model is required"}})
        return _not_found_handler(request)

    report = probe_video_endpoints(
        "https://relay.example.com", api_key="k", transport=httpx.MockTransport(handler)
    )
    assert report.best is not None
    assert report.best.path == "/v1/videos/generations"
    assert report.best.get_status == 400
    assert report.best.get_outcome == ProbeOutcome.ROUTED
    assert report.submit_url_template == "{base}/v1/videos/generations"
    assert report.submit_url_absolute == "https://relay.example.com/v1/videos/generations"
    assert report.poll_url_template == "{base}/v1/videos/generations/{id}"
    assert report.provider_suggestion == "generic"
    assert report.summary.startswith("已找到可用端点：")
    assert "/v1/videos/generations" in report.summary


def test_probe_suggests_doubao_for_ark_shape():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/contents/generations/tasks"):
            return httpx.Response(400, json={"error": {"message": "model is required"}})
        return _not_found_handler(request)

    report = probe_video_endpoints(
        "https://ark.cn-beijing.volces.com/api/v3",
        api_key="k",
        transport=httpx.MockTransport(handler),
    )
    assert report.best is not None
    assert report.best.path == "/contents/generations/tasks"
    assert report.submit_url_template == "{base}/contents/generations/tasks"
    assert report.poll_url_template == "{base}/contents/generations/tasks/{id}"
    assert report.provider_suggestion == "doubao"


def test_probe_suggests_gptge_for_volces_task_shape():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/task/volces/seedance":
            return httpx.Response(400, json={"error": {"message": "model is required"}})
        return _not_found_handler(request)

    report = probe_video_endpoints(
        "https://api.gpt.ge", api_key="k", transport=httpx.MockTransport(handler)
    )
    assert report.best is not None
    assert report.best.path == "/task/volces/seedance"
    assert report.provider_suggestion == "gptge"
    # gpt.ge 的任务查询在根路径 /task/{id}
    assert report.poll_url_template == "{base}/task/{id}"


def test_probe_base_url_prefix_is_not_duplicated():
    """Base URL 已带 /v1 时，候选 /v1/... 要退化成相对路径，不能拼出 /v1/v1/…。"""
    urls = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return _not_found_handler(request)

    probe_video_endpoints(
        "https://relay.example.com/v1", api_key="k", transport=httpx.MockTransport(handler)
    )
    assert urls, "应当发出探测请求"
    assert all("/v1/v1/" not in url for url in urls)
    assert "https://relay.example.com/v1/videos/generations" in urls
    assert len(urls) == len(set(urls)), "重复候选应去重"


def test_probe_sends_only_get_by_default():
    """默认只发 GET：宽松站点即使对任何请求都 201，也不会被记成 ok。"""
    methods = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "POST":
            return httpx.Response(201, json={"id": "task-created"})
        return httpx.Response(200, json={"data": []})

    report = probe_video_endpoints(
        "https://relay.example.com", api_key="k", transport=httpx.MockTransport(handler)
    )
    assert set(methods) == {"GET"}
    assert all(row.post_status is None and row.post_outcome == "" for row in report.rows)
    assert "POST" not in report.rows[0].note


def test_probe_allow_post_records_post_outcome():
    methods = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "POST":
            return httpx.Response(201, json={"id": "task-created"})
        return httpx.Response(405, text="method not allowed")

    report = probe_video_endpoints(
        "https://relay.example.com",
        api_key="k",
        transport=httpx.MockTransport(handler),
        allow_post=True,
    )
    assert "POST" in methods
    assert report.best is not None
    assert report.best.post_status == 201
    assert report.best.post_outcome == ProbeOutcome.OK
    assert "POST 探测被接受" in report.best.note


def test_post_probe_body_is_the_invalid_marker():
    """POST 探测体故意用不存在的模型名，避免真的跑一次生成。"""
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            bodies.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(400, json={"error": {"message": "model not exists"}})
        return httpx.Response(405, text="method not allowed")

    report = probe_video_endpoints(
        "https://relay.example.com", transport=httpx.MockTransport(handler), allow_post=True
    )
    assert bodies and all(body == POST_PROBE_PAYLOAD for body in bodies)
    assert report.best is not None and report.best.post_outcome == ProbeOutcome.ROUTED


def test_probe_uses_configured_auth_style():
    headers = []

    def handler(request: httpx.Request) -> httpx.Response:
        headers.append({k.lower(): v for k, v in request.headers.items()})
        return _not_found_handler(request)

    probe_video_endpoints(
        "https://relay.example.com",
        api_key="relay-key",
        params={"auth_style": "x-api-key"},
        transport=httpx.MockTransport(handler),
    )
    assert headers
    assert all(h.get("x-api-key") == "relay-key" for h in headers)
    assert all("authorization" not in h for h in headers)


def test_probe_never_raises_on_network_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    report = probe_video_endpoints(
        "https://relay.example.com", api_key="k", transport=httpx.MockTransport(handler)
    )
    assert report.best is None
    assert report.summary == SUMMARY_NONE
    assert report.rows
    assert all(row.get_outcome == ProbeOutcome.UNKNOWN for row in report.rows)
    assert "connection refused" in report.rows[0].note
    assert report.rows[0].get_status is None


def test_probe_auth_only_relay_falls_back_to_first_candidate():
    """中转站对任何路径都先鉴权（401）：无更好信号时取候选里第一条。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "unauthorized"}})

    report = probe_video_endpoints(
        "https://relay.example.com", api_key="bad", transport=httpx.MockTransport(handler)
    )
    assert report.best is not None
    assert report.best.path == VIDEO_SUBMIT_CANDIDATES[0]
    assert report.best.get_outcome == ProbeOutcome.AUTH
    assert "鉴权" in report.best.note


def test_probe_empty_base_url_returns_empty_report():
    report = probe_video_endpoints("", transport=httpx.MockTransport(_not_found_handler))
    assert isinstance(report, EndpointProbeReport)
    assert report.rows == [] and report.best is None
    assert "Base URL 为空" in report.summary


def test_probe_normalizes_full_endpoint_url():
    """用户把完整端点地址粘进 Base URL：探测前先规整，不会拼出双层路径。"""
    urls = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return _not_found_handler(request)

    report = probe_video_endpoints(
        "https://relay.example.com/v1/videos/generations",
        api_key="k",
        transport=httpx.MockTransport(handler),
    )
    assert report.base_url == "https://relay.example.com/v1"
    assert all("videos/generations/videos" not in url for url in urls)


def test_probe_recommends_post_only_endpoint_without_sending_post():
    """POST-only 的提交端点（GET 返回 405）在默认「不发 POST」的探测里也要能被推荐。

    真实中转站里很常见：`/v1/videos/generations` 只接受 POST，GET 回 405。
    旧行为把 405 当 missing，结果默认探测什么都找不到（用户报的 404 场景就是如此）。
    """
    methods: List[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.url.path == "/v1/videos/generations":
            return httpx.Response(405, text="method not allowed")
        return _not_found_handler(request)

    report = probe_video_endpoints(
        "https://relay.example.com", api_key="k", transport=httpx.MockTransport(handler)
    )
    assert report.best is not None, "405 说明路径存在，应作为候选推荐"
    assert report.best.path == "/v1/videos/generations"
    assert report.submit_url_template == "{base}/v1/videos/generations"
    assert report.poll_url_template == "{base}/v1/videos/generations/{id}"
    assert "POST" in report.summary, report.summary
    # 默认流程绝不发 POST（只发 GET，不产生真实任务）
    assert set(methods) == {"GET"}, methods


def test_guess_poll_url_template_shapes():
    assert guess_poll_url_template("{base}/v1/videos/generations") == "{base}/v1/videos/generations/{id}"
    assert guess_poll_url_template("{base}/v1/tasks") == "{base}/v1/tasks/{id}"
    assert guess_poll_url_template("{base}/v1/videos/image2video") == "{base}/v1/videos/image2video/{id}"
    assert guess_poll_url_template("{base}/task/volces/seedance") == "{base}/task/{id}"
    assert guess_poll_url_template("{base}/v1/video_generation") == "{base}/v1/query/video_generation?task_id={id}"
    assert guess_poll_url_template("") == ""


def test_provider_for_path():
    assert provider_for_path("/api/v3/contents/generations/tasks") == "doubao"
    assert provider_for_path("/task/volces/seedance") == "gptge"
    assert provider_for_path("/v1/videos/generations") == "generic"


def test_localized_note_and_summary_use_translate():
    """界面用 tr 渲染中文说明：语言包缺词时回退中文，绝不抛异常。"""
    parts = normalize_base_url("https://api.x.com/v1/videos/image2video")
    assert parts.localized_note(None) == parts.note
    assert parts.localized_note(lambda text: "EN") == "EN"  # 模板命中翻译

    report = probe_video_endpoints(
        "https://relay.example.com", transport=httpx.MockTransport(_not_found_handler)
    )
    assert report.localized_summary(lambda text: text.upper()) == SUMMARY_NONE.upper()


# --------------------------------------------------------------------------- #
# 4) 结果对话框 + 设置页按钮（Qt，离屏渲染）
# --------------------------------------------------------------------------- #
def _report_for_relay_v1() -> EndpointProbeReport:
    """Base URL 带 /v1 时的一份真实探测报告（/v1/videos/generations 返回 400）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/videos/generations":
            return httpx.Response(400, json={"error": {"message": "prompt is required"}})
        return httpx.Response(404, json={"error": {"message": f"Invalid URL ({request.url.path})"}})

    return probe_video_endpoints(
        "https://relay.example.com/v1", api_key="k", transport=httpx.MockTransport(handler)
    )


def test_dialog_lists_rows_and_returns_selection(qtbot):
    from PySide6.QtWidgets import QDialog

    from ui.dialogs.endpoint_adapt_dialog import EndpointAdaptDialog

    report = _report_for_relay_v1()
    dialog = EndpointAdaptDialog(report)
    qtbot.addWidget(dialog)

    assert "已找到可用端点" in dialog._summary.text()
    buttons = dialog.row_buttons()
    assert len(buttons) == len(report.rows)
    target = next(b for b in buttons if b.property("path") == "/videos/generations")
    target.click()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.choice() is report.best
    assert dialog.selection() == {
        "submit_url": "{base}/videos/generations",
        "poll_url": "{base}/videos/generations/{id}",
        "provider": "generic",
    }
    text = dialog.diagnostic_text()
    assert "端点路径" in text and "/videos/generations" in text


def test_dialog_warns_when_nothing_found(qtbot):
    from ui.dialogs.endpoint_adapt_dialog import EndpointAdaptDialog

    report = probe_video_endpoints(
        "https://relay.example.com", transport=httpx.MockTransport(_not_found_handler)
    )
    dialog = EndpointAdaptDialog(report)
    qtbot.addWidget(dialog)
    assert dialog.choice() is None
    assert dialog.selection() == {}
    assert "没有探测到可用端点" in dialog._mode_hint.text()
    assert "从 curl 导入" in dialog._mode_hint.text()


def _video_widget(tmp_path, base_url: str, extra_params: dict | None = None):
    from config.api_config import APIConfig, APIConfigManager
    from core.storage.keyring import Keyring
    from ui.widgets.api_config_widget import ApiConfigWidget

    manager = APIConfigManager(config_file=tmp_path / "api.json", keyring=Keyring(tmp_path / ".keyring"))
    params = {"provider": "generic"}
    params.update(extra_params or {})
    manager.add(
        APIConfig(kind="video", name="relay", base_url=base_url, api_key="relay-key", model="m", params=params)
    )
    return ApiConfigWidget(manager, "video"), manager


def test_widget_adapt_button_exists_for_video_only(qtbot, tmp_path):
    from ui.widgets.api_config_widget import ApiConfigWidget

    widget, _manager = _video_widget(tmp_path, "https://relay.example.com/v1")
    qtbot.addWidget(widget)
    assert widget._btn_adapt.text() == "一键适配端点…"
    assert not widget._btn_adapt.isHidden()

    from config.api_config import APIConfigManager
    from core.storage.keyring import Keyring

    llm_manager = APIConfigManager(config_file=tmp_path / "llm.json", keyring=Keyring(tmp_path / ".k2"))
    llm_widget = ApiConfigWidget(llm_manager, "llm")
    qtbot.addWidget(llm_widget)
    assert llm_widget._btn_adapt.isHidden()


def test_widget_adapt_writes_back_form_fields(qtbot, tmp_path, monkeypatch):
    """完整流程：规整 Base URL → 探测（假的）→ 选中端点写回表单 → 保存落盘。"""
    from PySide6.QtWidgets import QDialog

    from ui.dialogs import endpoint_adapt_dialog as dialog_mod
    from ui.widgets import api_config_widget as mod

    widget, manager = _video_widget(
        tmp_path, "https://relay.example.com/v1/videos/generations"
    )
    qtbot.addWidget(widget)

    captured = {}

    def fake_probe(base_url, api_key="", params=None, transport=None, allow_post=False, timeout=10.0):
        captured["base_url"] = base_url
        captured["api_key"] = api_key
        captured["allow_post"] = allow_post
        return _report_for_relay_v1()

    monkeypatch.setattr(mod, "probe_video_endpoints", fake_probe)

    def fake_exec(self):
        self._on_use(self.report().best)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(dialog_mod.EndpointAdaptDialog, "exec", fake_exec)

    widget._on_adapt_endpoint()

    # 粘贴的完整地址被拆开：Base URL 只剩 …/v1，原路径进了提交端点
    assert captured["base_url"] == "https://relay.example.com/v1"
    assert captured["allow_post"] is False, "默认不得触发 POST 探测"
    assert widget._get_field("base_url") == "https://relay.example.com/v1"
    assert widget._get_field("submit_url") == "{base}/videos/generations"
    assert widget._get_field("poll_url") == "{base}/videos/generations/{id}"
    assert widget._get_field("provider") == "generic"
    assert "已写入端点" in widget._test_result.text()

    # 「保存配置」能持久化这三项
    widget._on_save()
    cfg = manager.get_default("video")
    assert cfg.base_url == "https://relay.example.com/v1"
    assert cfg.params["submit_url"] == "{base}/videos/generations"
    assert cfg.params["poll_url"] == "{base}/videos/generations/{id}"


def test_widget_adapt_shows_split_note_when_dialog_closed(qtbot, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QDialog

    from ui.dialogs import endpoint_adapt_dialog as dialog_mod
    from ui.widgets import api_config_widget as mod

    widget, _manager = _video_widget(
        tmp_path, "https://relay.example.com/v1/videos/generations"
    )
    qtbot.addWidget(widget)
    monkeypatch.setattr(
        mod, "probe_video_endpoints",
        lambda *a, **k: _report_for_relay_v1(),
    )
    monkeypatch.setattr(
        dialog_mod.EndpointAdaptDialog, "exec",
        lambda self: QDialog.DialogCode.Rejected,
    )
    widget._on_adapt_endpoint()
    # 关闭对话框后仍把「拆分了什么」讲清楚
    assert "拆到「提交端点」" in widget._test_result.text()


def test_widget_adapt_mock_config_is_safe(qtbot, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from ui.widgets import api_config_widget as mod

    widget, _manager = _video_widget(tmp_path, "mock", extra_params={"mock": True})
    qtbot.addWidget(widget)
    messages = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: messages.append(a[2]))

    def boom(*_args, **_kwargs):
        raise AssertionError("模拟配置不应发起探测")

    monkeypatch.setattr(mod, "probe_video_endpoints", boom)
    widget._on_adapt_endpoint()
    assert messages == ["模拟 API 无需预览/探测请求"]


def test_widget_adapt_requires_base_url(qtbot, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    widget, _manager = _video_widget(tmp_path, "https://relay.example.com")
    qtbot.addWidget(widget)
    widget._set_field("base_url", "")
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a[2]))
    widget._on_adapt_endpoint()
    assert warnings == ["请先填写 Base URL"]


@pytest.mark.parametrize("path", ["/v1/videos/generations", "/api/v3/contents/generations/tasks"])
def test_candidates_include_documented_paths(path):
    assert path in VIDEO_SUBMIT_CANDIDATES
