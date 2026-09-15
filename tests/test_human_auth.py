"""人类专属工具的身份校验回归测试。

背景：2026-09-15 发现一个**可利用的漏洞**。`spine.db.admit` 只校验
`approved_by` —— 那是调用方自己传的字符串。实测公网 `/wb/mcp`（服务当时无任何
鉴权）：

    approved_by='intruder' → 被拒
    approved_by='hjx'      → **通过主体校验**，只因 eval_run 不存在才没落库

即凑出真实的 (factor_id, eval_run_id) 就能从公网批准因子。修复：人类专属工具
必须同时提供 WB_HUMAN_TOKEN，且 token 未配置时 fail-closed。
"""

import pytest

import spine.server as srv


def test_human_tools_are_the_privileged_three():
    assert srv.HUMAN_TOOLS == {"wb_admit", "wb_reject", "wb_retire"}


def test_fail_closed_when_token_unset(monkeypatch):
    """服务端没配 token 时必须**全部拒绝**，不能退化成无鉴权。"""
    monkeypatch.setattr(srv, "HUMAN_TOKEN", "")
    with pytest.raises(PermissionError) as e:
        srv._require_human_token("wb_admit", {"human_token": "anything"})
    assert "fail-closed" in str(e.value)


def test_spoofed_principal_does_not_help(monkeypatch):
    """★ 漏洞本体：只传自报的 approved_by 不足以通过。"""
    monkeypatch.setattr(srv, "HUMAN_TOKEN", "s3cret")
    with pytest.raises(PermissionError) as e:
        srv._require_human_token(
            "wb_admit",
            {"approved_by": "hjx", "rejected_by": "hjx", "retired_by": "hjx"})
    assert "不可作为身份凭据" in str(e.value)


def test_wrong_or_empty_token_rejected(monkeypatch):
    monkeypatch.setattr(srv, "HUMAN_TOKEN", "s3cret")
    for bad in ({"human_token": "wrong"}, {"human_token": ""}, {"human_token": None}, {}):
        with pytest.raises(PermissionError):
            srv._require_human_token("wb_reject", bad)


def test_correct_token_passes(monkeypatch):
    monkeypatch.setattr(srv, "HUMAN_TOKEN", "s3cret")
    srv._require_human_token("wb_admit", {"human_token": "s3cret"})  # 不抛
    srv._require_human_token("wb_retire", {"human_token": "s3cret"})


def test_token_compared_in_constant_time():
    """用 secrets.compare_digest 而非 ==，避免时序侧信道。"""
    import inspect
    src = inspect.getsource(srv._require_human_token)
    assert "compare_digest" in src
