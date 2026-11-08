# 修订十五观测回归，核对旧图兼容提示生命周期对象追踪及只读 Core 原生编码

import ast
from collections import defaultdict
from copy import copy, deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import traceback
from types import SimpleNamespace

import numpy as np
import pytest

from core.branch_retry import CoreBranchRetry
from core.observation import LlmObservationBuilder
from data_types import ActionOperation
from game_constants import LocationInfo
from model_protocols import inspect_model_checkpoint, load_model_backend
from model_protocols.v4.encoder import GalateaEncoder
from model_protocols.v4.protocol_schema import _revision_14_schema_hash
from tests.test_reload_field_protocol import reload_packet
from tests.test_v4_onnx_compatibility import frozen_graph
from tests.test_v4_prompt_projection import prompt_payload
from tests.v4_fixtures import write_v4_assets, write_v4_checkpoint, v4_snapshot
from tests.v4_online_fixtures import FixtureCardReader, full_query, make_state


# 固定卡库资料，避免读取开发环境里的真实卡片数据库
@pytest.fixture(autouse=True)
def fixed_reader(monkeypatch):
    monkeypatch.setattr('core.gamestate.card_db', FixtureCardReader())


# 询问使用完整四字节参数而不假定参数一定是某类用途编号
def select_hint(player, value=0xFE123456):
    return struct.pack('<BBI', 3, player, value)


# 以明确位置和原始链号构造真实连锁建立消息
def chain_payload(player, number=1):
    return struct.pack('<IIBBBIB', 14558127, LocationInfo.encode(player, 4, 0, 1), player, 4, 0, 0x12345678, number)


# 从只读源码内存执行独立 Core 状态类，不导入 Core 或写出字节码
def core_state_reference(root, state):
    from model_protocols.v4 import data_types, game_constants, deck_protocol, event_history, public_observation, core_message_protocol
    source = (Path(root) / 'gamestate.py').read_text(encoding='utf-8')
    tree = ast.parse(source)
    nodes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'DuelState'
        or isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id.startswith(('_IDLE_', '_SELECT_HINT_', '_DECISION_')) for target in node.targets)]
    namespace = {'copy': copy, 'deepcopy': deepcopy, 'struct': struct, 'io': io, 'traceback': traceback,
        'defaultdict': defaultdict, 'card_db': FixtureCardReader(), 'resolve_runtime_effect_slot': lambda *_: None}
    for module in (data_types, game_constants, deck_protocol, event_history, public_observation, core_message_protocol):
        namespace.update({key: value for key, value in vars(module).items() if not key.startswith('__')})
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<readonly Core context state>', 'exec'), namespace)
    brain = namespace['DuelState'](audit_enabled=False)
    brain.field_map = deepcopy(state.field_map)
    brain.active_player = brain.decision_player = state.active_player
    return brain


# 同一已核对旧合同的 PTH 保留全部权重和原文件，空上下文不改变输出
def test_revision14_pth_migrates_in_memory_and_preserves_weights(tmp_path):
    import torch
    assets = write_v4_assets(tmp_path / 'assets')
    path, checkpoint = write_v4_checkpoint(tmp_path, assets)
    old = {'protocol_schema_revision': 14, 'protocol_schema_hash': _revision_14_schema_hash(assets.vocabulary)}
    checkpoint.update(old)
    checkpoint['net_config'].update(old)
    checkpoint['model_state_dict']['public_context_encoder.notification_gate'].fill_(0.5)
    torch.save(checkpoint, path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert inspect_model_checkpoint(path)['protocol_schema_revision'] == 14
    backend = load_model_backend(path, asset_path=assets.root)
    assert backend.metadata['protocol_schema_revision'] == 14
    assert backend.metadata['runtime_protocol_schema_revision'] == 15
    assert backend.metadata['observation_compatibility']['status'] == 'weights_preserved_extension'
    assert set(backend.net.state_dict()) == set(checkpoint['model_state_dict'])
    for key, value in checkpoint['model_state_dict'].items():
        assert torch.equal(value, backend.net.state_dict()[key]), key
    snapshot = v4_snapshot()
    old_inputs = GalateaEncoder(assets, schema_revision=14).encode(snapshot, 0)
    new_inputs = backend.encoder.encode(snapshot, 0)
    assert len(old_inputs) == 108 and len(new_inputs) == 116
    old_output = backend.inference_runtime.infer(old_inputs)
    new_output = backend.inference_runtime.infer(new_inputs)
    assert np.array_equal(old_output[0], new_output[0]) and np.array_equal(old_output[1], new_output[1])
    snapshot.prompt_context = ({'kind': 2, 'subtype': 3, 'wire_player': 0, 'visibility_mask': 1,
        'value': 0xFE123456, 'prompt_type': 15},)
    changed = backend.inference_runtime.infer(backend.encoder.encode(snapshot, 0))
    assert not np.array_equal(changed[0], new_output[0])
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


# 冻结旧图继续接收108项，新图接收116项，不把改清单当作改计算图
@pytest.mark.parametrize('revision', [14, 15])
def test_onnx_keeps_real_revision_input_signature(tmp_path, revision):
    assets = write_v4_assets(tmp_path / 'assets')
    path, _ = frozen_graph(tmp_path, assets, schema_revision=revision)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    backend = load_model_backend(path, asset_path=assets.root)
    snapshot = v4_snapshot()
    if revision == 14:
        # 老图不编码自己没有的可选分支，其超容量不影响旧合同
        snapshot.prompt_context = ({'visibility_mask': 1},) * 300
    arrays = backend.encoder.encode(snapshot, 0)
    assert len(arrays) == (108 if revision == 14 else 116)
    logits, values, _ = backend.inference_runtime.infer(arrays)
    assert np.isfinite(values).all() and logits.shape == (1, 120)
    assert backend.metadata['observation_compatibility']['current_context_supported'] == (revision == 15)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


# 提示在Retry及逐项选撤内延续，离开选择流程后消费且新提示不被旧响应删除
@pytest.mark.parametrize('player', [0, 1])
def test_select_hint_lifetime_retry_continuation_and_replacement(player):
    state, _ = make_state(player)
    state.update(2, select_hint(player))
    state.update(15, prompt_payload(15, player))
    assert state.get_snapshot().prompt_context[-1]['value'] == 0xFE123456
    state._note_prompt_response(player, 15, b'\1\0', None)
    state.update(1, b'')
    assert state.get_snapshot().prompt_context[-1]['subtype'] == 3
    state._note_prompt_response(player, 15, b'\1\0', None)
    state.update(2, select_hint(player, 0x12345678))
    state.update(15, prompt_payload(15, player))
    assert state.get_snapshot().prompt_context[-1]['value'] == 0x12345678
    state._note_prompt_response(player, 15, b'\1\0', None)
    state.update(13, struct.pack('<BI', player, 123))
    assert not state.get_snapshot().prompt_context


# 每个已知子类型按真实投递角色过滤，未知与超宽参数只警告并保留合法菜单
@pytest.mark.parametrize('player', [0, 1])
def test_hint_visibility_and_unknown_hint_compatibility(player, tmp_path):
    state, _ = make_state(player)
    for subtype in (1, 2, 4, 5, 6, 7, 8, 9, 10, 11):
        state.update(2, struct.pack('<BBI', subtype, 0, 14558127))
    view = LlmObservationBuilder().build(state.get_snapshot(), player)
    seen = {hint['subtype'] for hint in view['public_context']['prompt_notifications']}
    assert seen == ({1, 2, 5, 10, 11} if player == 0 else {4, 6, 7, 8, 9, 10, 11})
    old = deepcopy(state.current_valid_actions)
    for payload in (struct.pack('<BBI', 250, player, 12), struct.pack('<BBQ', 3, player, 1 << 48)):
        state.update(2, payload)
    assert state.current_valid_actions == old
    assert 'current_hint_semantics' in state.online_projection.compatibility_warning_counts
    arrays = GalateaEncoder(write_v4_assets(tmp_path / 'assets')).encode_online(state.get_snapshot(), player)
    assert arrays['current_context_mask'].any()


# 无链逐卡指示累积到下一决策，Retry延用窗口而新决策清理旧对象
def test_unlinked_indications_accumulate_and_clear_at_new_window():
    state, _ = make_state()
    for sequence in (0, 1):
        state.update(83, bytes([1]) + struct.pack('<I', LocationInfo.encode(0, 4, sequence, 1)))
    state.update(13, struct.pack('<BI', 0, 123))
    assert len(state.get_snapshot().recent_chain_targets) == 2
    state.update(1, b'')
    state.update(13, struct.pack('<BI', 0, 123))
    assert len(state.get_snapshot().recent_chain_targets) == 2
    state.update(13, struct.pack('<BI', 0, 456))
    assert not state.get_snapshot().recent_chain_targets


# 两类无效与已处理状态分别记录，原始链号保留且旧快照不被后续修改
@pytest.mark.parametrize('player', [0, 1])
def test_chain_status_targets_visibility_and_lifecycle(player):
    state, _ = make_state(player)
    state.update(70, chain_payload(player, 1))
    state.update(71, b'\1')
    state.update(70, chain_payload(player, 2))
    raw = LocationInfo.encode(player, 4, 1, 0)
    state.update(83, b'\1' + struct.pack('<I', raw))
    frozen = state.get_snapshot()
    for kind, number in ((75, 1), (76, 2), (72, 2), (73, 2), (72, 1)):
        state.update(kind, bytes([number]))
    snap = state.get_snapshot()
    first, second = snap.chain_stack
    assert first['ct'] == 1 and first['activation_negated'] and not first['effect_disabled']
    assert first['solving'] and not second['solving'] and second['solved'] and second['effect_disabled']
    assert snap.resolving_chain_index == 1
    assert not frozen.chain_stack[1]['solved']
    view = LlmObservationBuilder().build(snap, player)
    assert view['chain'][1]['indicated_cards'][0]['code'] == 14558127
    assert view['public_context']['resolving_chain_number'] == 1
    state.update(74, b'')
    assert not state.chain_stack and state.resolving_chain_index == 0


# 公开对象跟随真实移动，洗牌失联后不绑定旧槽位上的新卡
def test_indication_moves_then_shuffle_loses_tracking_and_preserves_announcement():
    state, _ = make_state()
    old = LocationInfo.encode(0, 4, 0, 1)
    new = LocationInfo.encode(0, 2, 0, 0)
    state.update(83, b'\1' + struct.pack('<I', old))
    state.update(50, struct.pack('<IIII', 14558127, old, new, 0))
    target = state.get_snapshot().recent_chain_targets[0]
    assert target['announcement_location'] == old and target['current_location'] == new
    state.update(33, b'\0\0')
    state.update(7, bytes([0, 2, 0]) + full_query(0, 2, 0, code=89631139))
    target = state.get_snapshot().recent_chain_targets[0]
    assert not target['tracked'] and target['current_location'] == -1 and target['code'] == 14558127


# 单卡查询只校准对应槽位，不能因别处没有刷新就断言旧对象已经消失
@pytest.mark.parametrize('player', [0, 1])
def test_invalidated_query_sequence_is_unknown_until_real_refresh(player):
    state, _ = make_state(player)
    state.update(7, bytes([player, 2, 0]) + full_query(player, 2, 0, code=89631139))
    state.update(50, struct.pack('<IIII', 14558127,
        LocationInfo.encode(player, 4, 0, 1), LocationInfo.encode(player, 2, 1, 0), 0))
    assert state.field_map[player][2][0]['query_sequence'] is None
    state.update(83, b'\1' + struct.pack('<I', LocationInfo.encode(player, 2, 0, 0)))
    target = state.get_snapshot().recent_chain_targets[0]
    assert target['code'] == 0 and target['tracked']
    state.update(7, bytes([player, 2, 0]) + full_query(player, 2, 0, code=89631139))
    target = state.get_snapshot().recent_chain_targets[0]
    assert target['code'] == 89631139 and target['visibility_mask'] == (1 << player)
    state.update(13, struct.pack('<BI', player, 123))
    assert state.current_valid_actions


# 单卡查询只校准对应槽位，不能因别处没有刷新就断言旧对象已经消失
def test_partial_query_refreshes_only_its_indicated_instance():
    state, _ = make_state()
    state.update(83, b'\2' + struct.pack('<II', LocationInfo.encode(0, 4, 0, 1), LocationInfo.encode(0, 4, 1, 1)))
    state.update(7, bytes([0, 4, 0]) + full_query(0, 4, 0, code=89631139))
    first, second = state.get_snapshot().recent_chain_targets
    assert not first['tracked'] and second['tracked']


# 隐藏指示不泄露身份，新用途只改变提示节点而不重置实际场面证据
def test_hidden_indication_identity_and_context_sensitive_retry_key():
    state, _ = make_state()
    state.field_map[1][2][0] = {'code': 89631139, 'pos': 8}
    state.update(83, b'\1' + struct.pack('<I', LocationInfo.encode(1, 2, 0, 8)))
    view = LlmObservationBuilder().build(state.get_snapshot(), 0)
    assert 'code' not in view['public_context']['recent_indicated_cards'][0]
    retry = CoreBranchRetry()
    before = retry.world_key(state.get_snapshot())
    before_prompt = retry.prompt_key(state.get_snapshot(), 15, prompt_payload(15, 0))
    state.update(2, select_hint(0))
    state.update(15, prompt_payload(15, 0))
    assert retry.world_key(state.get_snapshot()) == before
    assert retry.prompt_key(state.get_snapshot(), 15, prompt_payload(15, 0)) != before_prompt


# 重建后保留明确未知标记而不沿用旧提示或旧对象
def test_reload_clears_current_context_and_marks_chain_status_unknown():
    state, _ = make_state()
    state.update(2, select_hint(0))
    state.update(70, chain_payload(0))
    state.update(83, b'\1' + struct.pack('<I', LocationInfo.encode(0, 4, 0, 1)))
    packet = reload_packet(chains=[(14558127, LocationInfo.encode(0, 4, 0, 1), 0, 4, 0, 123)])
    state.update(162, packet[1:])
    snap = state.get_snapshot()
    assert not snap.prompt_context and not snap.recent_chain_targets and snap.resolving_chain_index == 0
    assert not snap.chain_stack[0]['status_known'] and not snap.chain_stack[0]['targets_known']


# 逐项选撤的中间响应保留用途，明确完成后消费
@pytest.mark.parametrize('player', [0, 1])
def test_unselect_continues_hint_until_finished(player):
    state, _ = make_state(player)
    state.update(2, select_hint(player))
    payload = bytes([player, 1, 1, 1, 2, 1]) + struct.pack('<II', 14558127, LocationInfo.encode(player, 4, 0, 1)) + b'\0'
    state.update(26, payload)
    state._note_prompt_response(player, 26, b'\1\0', SimpleNamespace(operation_id=ActionOperation.SELECT))
    state.update(26, payload)
    assert state.get_snapshot().prompt_context[-1]['subtype'] == 3
    state._note_prompt_response(player, 26, -1, SimpleNamespace(operation_id=ActionOperation.FINISH))
    state.update(13, struct.pack('<BI', player, 123))
    assert not state.get_snapshot().prompt_context


# 使用 Core 实际状态更新与原始编码方法独立对照新增八项输入
@pytest.mark.skipif(not os.environ.get('GALATEA_CORE_READONLY_ROOT'), reason='需显式指定只读 Core')
@pytest.mark.parametrize('player', [0, 1])
def test_current_context_matches_core_state_and_original_encoder(tmp_path, player):
    import torch
    from model_protocols.v4 import feature_encoder, public_observation
    from model_protocols.v4.live_adapter import prepare_online_input
    root = Path(os.environ['GALATEA_CORE_READONLY_ROOT'])
    state, _ = make_state(player)
    brain = core_state_reference(root, state)
    messages = [(70, chain_payload(player, 1)), (71, b'\1'), (70, chain_payload(player, 2)),
        (83, b'\1' + struct.pack('<I', LocationInfo.encode(player, 4, 1, 0))),
        (75, b'\1'), (76, b'\2'), (72, b'\2'), (73, b'\2'), (72, b'\1'),
        (2, struct.pack('<BBI', 11, player, 9001)), (2, select_hint(player)), (15, prompt_payload(15, player))]
    for kind, payload in messages:
        state.update(kind, payload)
        brain.update(kind, payload)
    target = prepare_online_input(state.get_snapshot(), player).snapshot
    reference = SimpleNamespace(chain_stack=deepcopy(brain.chain_stack), resolving_chain_index=brain.resolving_chain_index,
        recent_chain_targets=deepcopy(brain.recent_chain_targets), prompt_context=brain._snapshot_prompt_context())
    tree = ast.parse((root / 'feature_encoder.py').read_text(encoding='utf-8'))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'GalateaEncoder')
    node = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == '_encode_current_context')
    namespace = {**vars(feature_encoder), **vars(public_observation), 'torch': torch}
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<readonly Core current encoder>', 'exec'), namespace)
    assets = write_v4_assets(tmp_path / 'assets')
    encoder = GalateaEncoder(assets)
    expected = namespace['_encode_current_context'](encoder.encoder, reference, player)
    actual = encoder.encode(target, player)
    for key, value in expected.items():
        assert np.array_equal(value.numpy(), actual[key]), key
    assert actual['current_context_mask'].sum() == 5


# 结构摘要依照 Core 当前合同计算，增加事实容量而不改变既有参数形状
@pytest.mark.skipif(not os.environ.get('GALATEA_CORE_READONLY_ROOT'), reason='需显式指定只读 Core')
def test_revision15_schema_hash_matches_core_descriptor(tmp_path):
    from model_protocols.v4 import protocol_schema
    root = Path(os.environ['GALATEA_CORE_READONLY_ROOT'])
    tree = ast.parse((root / 'protocol_schema.py').read_text(encoding='utf-8'))
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == '_schema_descriptor']
    namespace = dict(vars(protocol_schema))
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<readonly Core descriptor>', 'exec'), namespace)
    assets = write_v4_assets(tmp_path / 'assets')
    payload = namespace['_schema_descriptor'](assets.vocabulary)
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    assert assets.identity['protocol_schema_hash'] == digest and assets.identity['protocol_schema_revision'] == 15
