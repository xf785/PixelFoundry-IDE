"""cURL 导入解析测试（浏览器「Copy as cURL」→ 配置 params）。"""
import json

from core.api.curl_import import CurlImport, parse_curl, to_params

# 1) bash 多行 + 续行符 + 单引号（中转站最常见的复制结果）
BASH_MULTILINE = r"""curl 'https://relay.example.com/v1/videos/generations' \
  -H 'accept: application/json' \
  -H 'content-type: application/json' \
  -H 'x-api-key: sk-abc123' \
  -H 'user-agent: Mozilla/5.0' \
  --data-raw '{"model":"doubao-seedance-1-5-pro","prompt":"a cat walks","first_frame_image":"data:image/png;base64,AAAA","duration":5,"ratio":"16:9"}'"""

# 2) 一行式 --json（curl 7.82+）
JSON_FLAG = (
    "curl --json '{\"model\":\"sora-2\",\"prompt\":\"run\",\"input_image\":\"https://cdn.example.com/a.png\",\"seconds\":4}' "
    "https://relay.example.com/v1/videos"
)

# 3) 非 JSON 请求体（表单）
FORM_BODY = "curl -X POST https://relay.example.com/upload -d 'prompt=hello&model=x'"

# 4) 双引号 + 多行 JSON + 嵌套一层 + --url
QUOTED_PRETTY = """curl -X PUT --url "https://relay.example.com/openai/v1/videos" \\
  -H "Authorization: Bearer k" -H "Content-Type: application/json" \\
  -d '{
  "model": "veo-3",
  "prompt": "sunset over the sea",
  "image": "https://img.example.com/x.jpg",
  "negative_prompt": "blur",
  "seed": 42,
  "resolution": "1080p",
  "params": {"mode": "pro", "frames": 24}
}'"""


def test_bash_multiline_extracts_url_headers_body():
    imp = parse_curl(BASH_MULTILINE)
    assert isinstance(imp, CurlImport)
    assert imp.method == "POST"
    assert imp.url == "https://relay.example.com/v1/videos/generations"
    assert imp.base_url == "https://relay.example.com"
    assert imp.path == "/v1/videos/generations"
    assert imp.headers["x-api-key"] == "sk-abc123"
    assert imp.headers["content-type"] == "application/json"
    # 已知字段映射为占位符
    assert imp.placeholders == {
        "model": "$model",
        "prompt": "$prompt",
        "first_frame_image": "$image",
        "duration": "$duration",
        "ratio": "$ratio",
    }
    assert imp.image_mode == "data_uri"
    template = json.loads(imp.body_template)
    assert template["prompt"] == "$prompt"
    assert template["duration"] == "$duration"
    assert imp.warnings == []


def test_json_flag_and_url_image_placeholder():
    """--json 一行式：URL 首帧 -> $image_url，image_mode=url。"""
    imp = parse_curl(JSON_FLAG)
    assert imp.method == "POST"
    assert imp.url == "https://relay.example.com/v1/videos"
    assert imp.placeholders["input_image"] == "$image_url"
    assert imp.image_mode == "url"
    assert "https://cdn.example.com/a.png" not in imp.body_template
    # 没有请求头时提示鉴权需手选
    assert any("请求头" in w for w in imp.warnings)


def test_non_json_body_warns_and_has_no_template():
    imp = parse_curl(FORM_BODY)
    assert imp.method == "POST"
    assert imp.url == "https://relay.example.com/upload"
    assert imp.body is None
    assert imp.body_template == ""
    assert imp.raw_body == "prompt=hello&model=x"
    assert any("不是 JSON" in w for w in imp.warnings)


def test_quoted_pretty_json_nested_and_put():
    imp = parse_curl(QUOTED_PRETTY)
    assert imp.method == "PUT"
    assert imp.url == "https://relay.example.com/openai/v1/videos"
    assert imp.headers["Authorization"] == "Bearer k"
    # 嵌套一层的字段也能映射（路径用点号）
    assert imp.placeholders["params.mode"] == "$mode"
    assert imp.placeholders["params.frames"] == "$frames"
    assert imp.placeholders["image"] == "$image_url"
    body = json.loads(imp.body_template)
    assert body["params"]["frames"] == "$frames"
    assert body["seed"] == "$seed"


def test_to_params_maps_config_fields():
    imp = parse_curl(BASH_MULTILINE)
    params = to_params(imp)
    assert params["provider"] == "custom"
    assert params["submit_url"] == "https://relay.example.com/v1/videos/generations"
    assert params["request_method"] == "POST"
    assert params["submit_method"] == "POST"
    # 额外请求头保留业务头，丢掉自动生成的 Content-Type / Accept / User-Agent
    headers = json.loads(params["extra_headers"])
    assert headers == {"x-api-key": "sk-abc123"}
    assert json.loads(params["payload_template"])["prompt"] == "$prompt"
    # 猜出的轮询端点带 {base}/{id}
    assert params["poll_url"] == "{base}/v1/videos/generations/{id}"


def test_to_params_injects_api_key_into_query_placeholder():
    """URL 里带 ?key=… 时，可用 API Key 直接替换（避免手改 URL）。"""
    imp = parse_curl("curl 'https://relay.example.com/v1/videos?key=YOUR_API_KEY' -d '{\"prompt\":\"a\"}'")
    params = to_params(imp, api_key="sk-live-1")
    assert "key=sk-live-1" in params["submit_url"]


def test_task_query_url_is_flagged_and_not_used_for_poll():
    """看起来是「按任务 ID 查询」的 URL：给出警告，且不再猜轮询端点。"""
    imp = parse_curl("curl 'https://relay.example.com/v1/videos/generations/abc123def456'")
    assert any("轮询端点" in w for w in imp.warnings)
    params = to_params(imp)
    assert params["submit_url"].endswith("/generations/abc123def456")
    assert "poll_url" not in params


def test_empty_and_garbage_input():
    empty = parse_curl("")
    assert empty.url == "" and empty.warnings
    garbage = parse_curl("not a curl command at all")
    # 容错：没有 curl 前缀时，第一个 token 会被当成 URL（并在解析阶段校验完整性）
    assert garbage.url == "not"
    assert any("URL 不完整" in w for w in garbage.warnings)


def test_unknown_option_values_do_not_become_url():
    """未知的带值选项（如 -o out.json）不能把它的值当成 URL。"""
    imp = parse_curl("curl -o out.json -s https://relay.example.com/v1/x -d '{\"prompt\":\"p\"}'")
    assert imp.url == "https://relay.example.com/v1/x"


def test_shell_wrapper_and_command_substitution_warn():
    imp = parse_curl("curl 'https://relay.example.com/v1/x?t=$(date)'")
    assert imp.base_url == "https://relay.example.com"
    assert any("命令替换" in w for w in imp.warnings)
