"""IDE 模式「生图 → 加入帧序列 → 首帧生成视频」链路的回归测试。

对应两个真实反馈：
1. 生成首帧图片后，想把它加进序列帧列表却没有入口（只有「+ 空白帧」）；
2. 用首帧生成视频时报「请先生成或导入首帧图片」——首帧图与帧序列没有打通。
"""
import numpy as np
import pytest
from PIL import Image

from config.api_config import APIConfig, APIConfigManager
from core.storage.keyring import Keyring
from core.workflow.ide_workflow import IdeSession, IdeWorkflow
from core.workflow.solo_workflow import WorkflowError
from ui.app_context import AppContext, UISettings


@pytest.fixture()
def ctx(tmp_path):
    """带默认 mock 配置的应用上下文（与 test_gui.py 一致）。"""
    api = APIConfigManager(
        config_file=tmp_path / "api_config.json",
        keyring=Keyring(tmp_path / ".keyring"),
    )
    for kind in ("llm", "image", "video"):
        api.add(
            APIConfig(
                kind=kind,
                name=f"mock-{kind}",
                base_url="mock",
                model="mock-model",
                params={"mock": True, "frames": 8, "fps": 8},
            )
        )
    return AppContext(api=api, ui_settings=UISettings(tmp_path / "ui_settings.json"))


def _page(ctx):
    from ui.pages.ide_page import IdePage

    return IdePage(ctx)


# --------------------------------------------------------------------------- #
# 1) 把当前图加入帧序列
# --------------------------------------------------------------------------- #
def test_add_current_frame_uses_first_frame(qtbot, ctx):
    """只有首帧图时，「+ 当前图」把首帧图追加为第 1 帧。"""
    page = _page(ctx)
    qtbot.addWidget(page)
    page.show()
    page._session.first_frame = Image.new("RGBA", (16, 16), (10, 200, 30, 255))
    page._refresh_all()
    assert page._timeline.frame_count() == 0

    page._on_add_current_frame()

    assert len(page._session.frames) == 1
    assert page._timeline.frame_count() == 1
    assert page._current == 0
    assert page._session.frames[0].getpixel((0, 0)) == (10, 200, 30, 255)


def test_add_current_frame_prefers_selected_frame(qtbot, ctx):
    """已有帧序列时，「+ 当前图」复制的是当前选中的帧。"""
    page = _page(ctx)
    qtbot.addWidget(page)
    page.show()
    red = Image.new("RGBA", (8, 8), (255, 0, 0, 255))
    blue = Image.new("RGBA", (8, 8), (0, 0, 255, 255))
    page._session.frames = [red, blue]
    page._current = 1
    page._refresh_all()

    page._on_add_current_frame()

    assert len(page._session.frames) == 3
    assert page._session.frames[2].getpixel((0, 0)) == (0, 0, 255, 255)
    assert page._session.frames[0] is red  # 原帧未被改动


def test_add_current_frame_without_any_image_appends_blank(qtbot, ctx):
    """什么图都没有时退化为空白帧并给出提示（不崩、不静默失败）。"""
    page = _page(ctx)
    qtbot.addWidget(page)
    page.show()
    page._on_add_current_frame()
    assert len(page._session.frames) == 1
    assert page._session.frames[0].getbbox() is None  # 全透明


def test_editor_edits_first_frame_when_no_frames(qtbot, ctx):
    """帧序列为空但已有首帧图时，编辑器打开的是首帧图（而不是空白画布）。"""
    page = _page(ctx)
    qtbot.addWidget(page)
    page.show()
    page._session.first_frame = Image.new("RGBA", (12, 12), (0, 0, 0, 255))
    page._refresh_all()

    assert page._editing_first_frame is True
    assert page._editor.frame().size == (12, 12)
    assert page._editor.frame().getpixel((1, 1)) == (0, 0, 0, 255)

    # 编辑后写回首帧（动画步骤用的就是它）
    page._editor.canvas().set_pixel(2, 2, (250, 250, 0, 255))
    page._on_editor_edited()
    assert page._session.first_frame.getpixel((2, 2)) == (250, 250, 0, 255)

    # 运行步骤前的 _sync_session 也要把它带过去
    page._editor.canvas().set_pixel(3, 3, (0, 250, 250, 255))
    page._sync_session()
    assert page._session.first_frame.getpixel((3, 3)) == (0, 250, 250, 255)


def test_set_first_frame_from_current_frame(qtbot, ctx):
    """把时间轴当前帧设为动画首帧。"""
    page = _page(ctx)
    qtbot.addWidget(page)
    page.show()
    frames = [Image.new("RGBA", (8, 8), (i * 40, 0, 0, 255)) for i in range(3)]
    page._session.frames = frames
    page._session.first_frame = None
    page._current = 2
    page._refresh_all()
    assert page._btn_first_from_current.isEnabled()

    page._set_first_frame_from_current()

    assert page._session.first_frame.getpixel((0, 0)) == (80, 0, 0, 255)
    assert not page._editing_first_frame


def test_first_frame_row_reflects_state(qtbot, ctx):
    """左栏「首帧图」行显示尺寸与按钮可用性。"""
    page = _page(ctx)
    qtbot.addWidget(page)
    page.show()
    assert not page._btn_first_from_current.isEnabled()
    page._session.first_frame = Image.new("RGBA", (24, 32), (1, 2, 3, 255))
    page._refresh_all()
    assert "24×32" in page._first_frame_label.text()
    assert not page._first_frame_thumb.pixmap().isNull()


# --------------------------------------------------------------------------- #
# 2) 首帧 → 视频：工作流侧的兜底
# --------------------------------------------------------------------------- #
def _workflow(monkeypatch):
    from core.api.mock_clients import MockLLMAPI, MockVideoAPI

    return IdeWorkflow(MockLLMAPI({}), None, MockVideoAPI({"params": {"frames": 4}}))


def test_step_animation_falls_back_to_first_sequence_frame(monkeypatch):
    """没单独设首帧、但帧序列非空时，用第 1 帧当首帧继续跑（旧版直接报错卡住）。"""
    wf = _workflow(monkeypatch)
    session = IdeSession(frame_count=4, fps=8, force_pure_bg=False)
    session.frames = [Image.new("RGBA", (64, 64), (200, 30, 30, 255))]
    session.first_frame = None

    frames = wf.step_animation(session)

    assert session.first_frame is not None, "应把第 1 帧回填为首帧"
    assert len(frames) == 4
    assert all(isinstance(f, Image.Image) for f in frames)


def test_step_animation_error_is_actionable(monkeypatch):
    """彻底没有图时，错误信息要告诉用户怎么修（而不是一句「请先生成首帧图片」）。"""
    wf = _workflow(monkeypatch)
    session = IdeSession(force_pure_bg=False)
    try:
        wf.step_animation(session)
    except WorkflowError as exc:
        msg = str(exc)
        assert "首帧" in msg
        assert "当前图" in msg or "首帧图片" in msg
    else:  # pragma: no cover - 必须抛错
        raise AssertionError("缺少首帧时应抛 WorkflowError")


def test_add_current_frame_then_animation(qtbot, ctx, monkeypatch):
    """端到端：生图 → 「+ 当前图」→ 帧序列有 1 帧 → 动画步骤能跑通。"""
    page = _page(ctx)
    qtbot.addWidget(page)
    page.show()
    page._session.first_frame = Image.new("RGBA", (64, 64), (120, 90, 200, 255))
    page._refresh_all()
    page._on_add_current_frame()
    assert len(page._session.frames) == 1

    wf = _workflow(monkeypatch)
    page._session.force_pure_bg = False
    frames = wf.step_animation(page._session)
    assert len(frames) == page._session.frame_count
    # 帧数据与像素数组同形，可直接进时间轴
    assert np.asarray(frames[0]).shape[2] == 4
