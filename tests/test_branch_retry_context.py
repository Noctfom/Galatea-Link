# 选择用途切换的闭环回归，覆盖真实素材菜单查询刷新和 Core 只读差异

import ast
import asyncio
import os
from pathlib import Path
import struct
from unittest.mock import AsyncMock

import pytest

from core.branch_retry import CoreBranchRetry
from core.gamestate import DuelState
from core.network import build_packet
from data_types import ActionOperation
from game_constants import LocationInfo
from app_config import DecisionConfig
from galatea_link import GalateaLink
from tests.test_online_state import start_payload
from tests.v4_online_fixtures import FixtureCardReader, full_query
from tests.v4_fixtures import write_v4_assets, write_v4_checkpoint
from tests.test_v4_onnx_compatibility import frozen_graph


# 构造与现场相同的十候选主菜单，特殊召唤第五项返回原始索引四
def main_prompt(player):
    codes = (23689428, 23689428, 10547580, 146746, 22423493)
    payload = bytes([player, 0, len(codes)])
    payload += b''.join(struct.pack('<IBBB', code, player, 64, index) for index, code in enumerate(codes))
    payload += bytes([0, 0, 1]) + struct.pack('<IBBB', 84797028, player, 2, 0)
    payload += b'\1' + struct.pack('<IBBBI', 84797028, player, 2, 0, 123)
    return payload + b'\1\1\1'


# 构造二十四字节完整 Type26 帧的载荷，允许取消并提供两个素材
def material_prompt(player):
    return bytes([player, 0, 1, 1, 2, 2]) + b''.join(
        struct.pack('<II', code, LocationInfo.encode(player, 4, index, 1))
        for index, code in enumerate((76830505, 52481437))) + b'\0'


# 通过真实状态更新建立固定局面，后续查询只有采集序号改变
def prepare_state(player):
    state = DuelState(model_protocol_version=4)
    state.update(4, start_payload(player))
    state.update(40, bytes([player]))
    state.update(41, struct.pack('<H', 4))
    for index, code in enumerate((76830505, 52481437)):
        state.update(7, bytes([player, 4, index]) + full_query(player, 4, index, code=code))
    state.update(11, main_prompt(player))
    return state


# 临时素材用途不构成真实场面进展，取消返回仍累计到默认五次阈值
@pytest.mark.parametrize('player', [0, 1])
def test_real_material_cancel_cycle_survives_hint_and_query_refresh(monkeypatch, player):
    monkeypatch.setattr('core.gamestate.card_db', FixtureCardReader())
    state = prepare_state(player)
    retry = CoreBranchRetry()
    main = state.get_snapshot()
    retry.enter(main, 11, main_prompt(player))
    epoch = retry._epoch
    for repeat in range(5):
        retry.stage(main, 4)
        state.update(2, struct.pack('<BBI', 3, player, 512))
        state.update(26, material_prompt(player))
        leaf = state.get_snapshot()
        retry.enter(leaf, 26, material_prompt(player))
        assert leaf.prompt_context and leaf.valid_actions[0].operation_id == ActionOperation.CANCEL
        retry.stage(leaf, 0)
        state._note_prompt_response(player, 26, -1, leaf.valid_actions[0])
        # 服务器取消后刷新场面但不改变实际数值，不能清空闭环计数
        for index, code in enumerate((76830505, 52481437)):
            state.update(7, bytes([player, 4, index]) + full_query(player, 4, index, code=code))
        state.update(11, main_prompt(player))
        main = state.get_snapshot()
        changes, _ = retry.enter(main, 11, main_prompt(player))
        assert not main.prompt_context
        assert bool(changes) == (repeat == 4)
    assert retry._epoch == epoch
    assert [item['message_type'] for item in changes] == [26]
    assert main.core_soft_banned_choice_ids == ()
    retry.stage(main, 4)
    state.update(2, struct.pack('<BBI', 3, player, 512))
    state.update(26, material_prompt(player))
    leaf = state.get_snapshot()
    retry.enter(leaf, 26, material_prompt(player))
    assert leaf.core_soft_banned_choice_ids == (0,)


# 相同报文的不同用途分别计数，临时上下文不再重置全部分支证据
@pytest.mark.parametrize('field', ['prompt_context', 'recent_chain_targets', 'resolving_chain_index'])
def test_different_prompt_contexts_are_separate_nodes_without_reset(field):
    from copy import deepcopy
    from tests.test_branch_retry import MAIN, snapshot

    retry = CoreBranchRetry(threshold=1)
    first = snapshot(MAIN)
    changed = deepcopy(first)
    setattr(changed, field, 1 if field == 'resolving_chain_index' else ({'value': 512},))
    retry.enter(first, 11, b'same-menu')
    epoch = retry._epoch
    retry.stage(first, 0)
    assert retry.enter(changed, 11, b'same-menu')[0] == []
    assert retry._epoch == epoch
    assert first._core_branch_context[2] != changed._core_branch_context[2]
    retry.stage(changed, 0)
    returned = deepcopy(first)
    changes, _ = retry.enter(returned, 11, b'same-menu')
    assert changes and changes[0]['depth'] == 2


# 在内存执行 Core 竞技场原始方法，确认其场面键不会因选择用途切换重置
def test_readonly_core_arena_does_not_share_context_reset(monkeypatch):
    root = os.getenv('GALATEA_CORE_READONLY_ROOT')
    if not root:
        pytest.skip('未配置只读 Core 对照目录')
    monkeypatch.setattr('core.gamestate.card_db', FixtureCardReader())
    source = Path(root, 'model_versus.py').read_text(encoding='utf-8')
    tree = ast.parse(source)
    names = {'_freeze_arena_state', 'ArenaBranchRetry'}
    module = ast.Module(body=[node for node in tree.body if getattr(node, 'name', None) in names], type_ignores=[])
    namespace = {'ARENA_LOOP_SOFT_BAN_THRESHOLD': 5, 'MAX_ACTIONS': 120,
        'ActionOperation': ActionOperation, 'torch': pytest.importorskip('torch')}
    exec(compile(module, '<readonly-core-branch-retry>', 'exec'), namespace)
    retry = namespace['ArenaBranchRetry']()
    state = prepare_state(0)
    main = state.get_snapshot()
    retry.enter((11,), main, payload=main_prompt(0))
    for repeat in range(5):
        retry.record((11,), main, 4, namespace['torch'].zeros(10))
        state.update(2, struct.pack('<BBI', 3, 0, 512))
        state.update(26, material_prompt(0))
        leaf = state.get_snapshot()
        retry.enter((26,), leaf, payload=material_prompt(0))
        retry.record((26,), leaf, 0, namespace['torch'].zeros(3))
        state._note_prompt_response(0, 26, -1, leaf.valid_actions[0])
        state.update(11, main_prompt(0))
        main = state.get_snapshot()
        changes = retry.enter((11,), main, payload=main_prompt(0))
        assert bool(changes) == (repeat == 4)
    assert [item['msg_type'] for item in changes] == [26]


# 真实模型固定同分偏好首项，通过完整 TCP 验证取消五次后改选素材并继续响应
@pytest.mark.asyncio
@pytest.mark.parametrize('player', [0, 1])
@pytest.mark.parametrize('backend', ['pytorch', 'onnx15', 'onnx14'])
async def test_greedy_tcp_breaks_material_cancel_loop(tmp_path, monkeypatch, player, backend):
    assets = write_v4_assets(tmp_path / 'assets', semantic_number=0.0)
    if backend == 'pytorch':
        import torch
        path, checkpoint = write_v4_checkpoint(tmp_path, assets)
        for tensor in checkpoint['model_state_dict'].values():
            if tensor.is_floating_point():
                tensor.zero_()
        torch.save(checkpoint, path)
    else:
        path, _ = frozen_graph(tmp_path, assets, schema_revision=14 if backend == 'onnx14' else 15)
    (tmp_path / 'decks').mkdir()
    (tmp_path / 'decks/test.ydk').write_text('#main\n14558127\n#extra\n89631139\n!side\n', encoding='utf-8')
    monkeypatch.setattr('galatea_link.LINK_DATA_ROOT', tmp_path)
    monkeypatch.setattr('galatea_link.card_db.reload', lambda *_args, **_kwargs: None)
    monkeypatch.setattr('core.gamestate.card_db', FixtureCardReader())
    # 只有特殊召唤入口及两个阶段按钮，真实零权重模型首先尝试特殊召唤
    idle = bytes([player, 0, 1]) + struct.pack('<IBBB', 89631139, player, 64, 0) + bytes([0, 0, 0, 0, 1, 1, 0])
    responses, events = [], []
    result = asyncio.get_running_loop().create_future()
    release = asyncio.Event()
    committed = asyncio.Event()

    # 读取实际出站响应，跳过入房准备等非决策包
    async def read_response(reader):
        while True:
            size, = struct.unpack('<H', await reader.readexactly(2))
            packet = await reader.readexactly(size)
            if packet[0] == 1:
                responses.append(packet[1:])
                return packet[1:]

    # 模拟合法取消回主菜单，不发送 Retry，场面刷新不改变卡片属性
    async def serve(reader, writer):
        try:
            while True:
                size, = struct.unpack('<H', await reader.readexactly(2))
                if (await reader.readexactly(size))[0] == 0x12:
                    break
            writer.write(build_packet(0x13, bytes([player])) + build_packet(0x15))
            writer.write(build_packet(1, b'\4' + start_payload(player)))
            writer.write(build_packet(1, bytes([40, player])) + build_packet(1, b'\x29' + struct.pack('<H', 4)))
            writer.write(build_packet(1, bytes([7, player, 64, 0]) + full_query(player, 64, 0, code=89631139)))
            for index, code in enumerate((76830505, 52481437)):
                writer.write(build_packet(1, bytes([7, player, 4, index]) + full_query(player, 4, index, code=code)))
            writer.write(build_packet(1, b'\x0b' + idle))
            await writer.drain()
            for repeat in range(8):
                assert await read_response(reader) == struct.pack('<i', 1)
                writer.write(build_packet(1, b'\2' + struct.pack('<BBI', 3, player, 512)))
                writer.write(build_packet(1, b'\x1a' + material_prompt(player)))
                await writer.drain()
                response = await read_response(reader)
                if response != b'\xff' * 4:
                    assert repeat == 5 and response == b'\1\0'
                    writer.write(build_packet(1, b'\x0d' + struct.pack('<BI', player, 123)))
                    await writer.drain()
                    assert await read_response(reader) == struct.pack('<i', 1)
                    result.set_result(True)
                    await release.wait()
                    return
                for index, code in enumerate((76830505, 52481437)):
                    writer.write(build_packet(1, bytes([7, player, 4, index]) + full_query(player, 4, index, code=code)))
                writer.write(build_packet(1, b'\x0b' + idle))
                await writer.drain()
            raise AssertionError('贪心策略仍在反复取消，没有执行末端回退')
        except BaseException as error:
            if not result.done():
                result.set_exception(error)
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(serve, '127.0.0.1', 0)
    link = GalateaLink('127.0.0.1', server.sockets[0].getsockname()[1], '', 'test',
        weights_path=str(path), model_assets_path=str(assets.root),
        decision_config=DecisionConfig(mode='core_only', core_policy_mode='greedy', core_time_budget=10))
    rule = AsyncMock(side_effect=AssertionError('闭环回退应继续使用 Core，不应调用规则替代'))
    link._compute_rule_decision = rule
    original_publish = link._publish_event

    # 保留原事件总线，另外收集软避让证据和实际决策来源
    def capture(kind, payload=None):
        events.append((kind, payload))
        if kind == 'decision.committed' and payload['request_id'] == 13:
            committed.set()
        return original_publish(kind, payload)

    link._publish_event = capture
    task = asyncio.create_task(link.start())
    try:
        assert await asyncio.wait_for(asyncio.shield(result), 20)
        await asyncio.wait_for(committed.wait(), 5)
        assert responses.count(b'\xff' * 4) == 5 and len(responses) == 13
        adjustments = [payload for kind, payload in events if kind == 'core.loop.adjusted']
        assert len(adjustments) == 1
        assert adjustments[0]['changes'] == [{'message_type': 26, 'choice_id': 0, 'depth': 2, 'repeat_count': 5}]
        assert not any(kind in ('message.processing_failed', 'core.loop.unavailable') for kind, _ in events)
        commits = [payload for kind, payload in events if kind == 'decision.committed']
        assert len(commits) == 13 and all(item['source'] == 'core' for item in commits)
        rule.assert_not_awaited()
    finally:
        release.set()
        await link.close()
        await asyncio.wait_for(task, 5)
        server.close()
        await server.wait_closed()
