# 在线兼容策略回归，覆盖带尾部的有效查询扩展槽位未知字段及警告落盘

import struct

import pytest

from core import query_protocol as q
from core.gamestate import DuelState
from core.online_state import OnlineStateProjection
from core.protocol_warnings import LOGGER, configure_protocol_warning_log
from tests.test_online_state import query_row, start_payload
from tests.online_server_fixtures import padded_code_row


# 八十字节仅卡号刷新不能断开，尾部不进入实体且后续查询仍正确解析
@pytest.mark.parametrize("code", [0, 14558127])
@pytest.mark.parametrize("tail", [bytes(68), b"\xA5" * 68])
def test_padded_code_row_keeps_framing_and_ignores_tail(code, tail):
    state = DuelState(model_protocol_version=4)
    state.update(4, start_payload())
    row = padded_code_row(code, tail=tail)
    with pytest.raises(q.CoreQueryError):
        q.parse_legacy_card_query(row)
    state.update(6, bytes([0, 2]) + row + query_row(0, 2, 1, atk=1800))
    assert len(state.field_map[0][2]) == 2
    assert state.field_map[0][2][0]["code"] == code
    assert "current_atk" not in state.field_map[0][2][0]
    assert state.field_map[0][2][1]["current_atk"] == 1800
    assert state.online_projection.compatibility_warning_counts["query_extra_bytes"] == 1


# 未知高位扩展保留已知字段，跨行推进使用原始长度而不误读扩展字节
def test_unknown_extension_does_not_shift_following_cards():
    view = OnlineStateProjection()
    view.start(start_payload())
    row = bytearray(query_row(zone=2))
    flags = struct.unpack_from("<I", row, 4)[0] | 0x1000000
    struct.pack_into("<I", row, 4, flags)
    row += bytes(range(12))
    struct.pack_into("<I", row, 0, len(row))
    view.refresh(6, bytes([0, 2]) + row + query_row(0, 2, 1, atk=1700))
    assert view.cards[(0, 2, 0)]["code"] == 14558127
    assert view.cards[(0, 2, 1)]["current_atk"] == 1700
    assert view.compatibility_warning_counts == {"query_unknown_flags": 1, "query_extra_bytes": 1}


# 服务端外层定位和可解析占位优先，槽位数量与内层定位差异不终止游戏
def test_slot_and_position_variations_only_warn():
    view = OnlineStateProjection()
    view.start(start_payload())
    view.refresh(6, bytes([0, 4]) + query_row(index=3) + struct.pack("<I", 4) * 4)
    assert view.cards[(0, 4, 0)]["code"] == 14558127
    view.refresh(7, bytes([0, 4, 7]) + query_row(index=7))
    assert (0, 4, 7) in view.cards
    view.refresh(6, bytes([0, 2]) + struct.pack("<I", 4) + query_row(zone=2, index=1))
    assert (0, 2, 1) in view.cards
    for name in ("query_zone_slot_count", "query_position_mismatch", "query_extended_slot", "query_continuous_empty_slot"):
        assert view.compatibility_warning_counts[name] == 1


# 未适配的辅助区域可以忽略，不能改写已有场面或妨碍后续有效查询
def test_unknown_query_region_is_skipped_without_state_loss():
    state = DuelState()
    state.update(4, start_payload())
    state.update(7, bytes([0, 4, 0]) + query_row())
    state.update(7, bytes([0, 128, 0]) + padded_code_row())
    state.update(7, bytes([9, 2, 0]) + padded_code_row())
    assert state.field_map[0][4][0]["code"] == 14558127
    assert state.online_projection.compatibility_warning_counts["query_unsupported_region"] == 2


# 无法确定行边界的截断仍属于解析异常，不提交半包状态
def test_truncated_record_boundary_remains_an_error():
    view = OnlineStateProjection()
    view.start(start_payload())
    with pytest.raises(ValueError, match="截断"):
        view.refresh(6, bytes([0, 2]) + padded_code_row()[:-1])
    assert view.cards == {}


# 兼容记录有界且日志真正保存到本地文件，反复出现不会无限刷屏
def test_compatibility_warnings_are_bounded_and_saved(tmp_path):
    previous_level = LOGGER.level
    previous_handlers = list(LOGGER.handlers)
    configure_protocol_warning_log(tmp_path)
    try:
        view = OnlineStateProjection()
        view.start(start_payload())
        for _ in range(20):
            view.refresh(7, bytes([0, 2, 0]) + padded_code_row())
        for handler in LOGGER.handlers:
            handler.flush()
        text = (tmp_path / "logs/protocol_warnings.log").read_text(encoding="utf-8")
        assert text.count("query_extra_bytes") == 3
        assert "ignored_bytes" in text and "68" in text
        assert "14558127" not in text
        report = view.coverage()
        assert report["compatibility_warning_counts"]["query_extra_bytes"] == 20
        assert len(report["latest_compatibility_warnings"]) == 16
    finally:
        for handler in list(LOGGER.handlers):
            if handler not in previous_handlers:
                LOGGER.removeHandler(handler)
                handler.close()
        LOGGER.setLevel(previous_level)


# 重复开局通知只记警告，不能抹掉进行中的查询场面
def test_repeated_start_preserves_current_field():
    state = DuelState()
    state.update(4, start_payload())
    state.update(7, bytes([0, 4, 0]) + query_row())
    state.update(4, start_payload())
    assert state.field_map[0][4][0]["code"] == 14558127
    assert state.online_projection.cards[(0, 4, 0)]["code"] == 14558127
    assert state.online_projection.compatibility_warning_counts["start_repeated"] == 1
