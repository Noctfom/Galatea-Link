# V4 最小完整运行资产和快照夹具，测试文件仅写入调用方提供的临时目录

import hashlib
import json
from pathlib import Path

import numpy as np

from model_protocols.v4.card_vocab import CardVocabulary
from model_protocols.v4.semantic_lookup import StaticSemanticLookup
from model_protocols.v4.assets import load_v4_assets
from model_protocols.v4.data_types import GlobalFeature, CardEntity, GameAction, GameSnapshot


# 生成结构及逻辑身份完整的最小编译语义资产
def write_v4_assets(root, *, semantic_number=0.0, code_contract=None, cards=None, categories=None):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    vocabulary = CardVocabulary(cards if cards is not None else {14558127: 10, 89631139: 11}, capacity=32)
    (root / "card_vocab.json").write_text(json.dumps(vocabulary.to_payload()), encoding="utf-8")
    arrays = {
        "category": np.zeros((32, 8, 8), np.int16),
        "requirement": np.full((32, 8, 16), -1, np.int8),
        "setcode": np.zeros((32, 8, 4), np.int16),
        "number": np.zeros((32, 8, 4), np.float16),
        "reference": np.zeros((32, 8, 4), np.int32),
        "race": np.zeros((32, 8, 4), np.int16),
        "attribute": np.zeros((32, 8, 4), np.int16),
        "code_index": np.zeros((32, 8), np.int32),
        "effect_mask": np.zeros((32, 8), np.bool_),
        "code_dictionary": np.zeros((2, 384), np.float32),
    }
    arrays["effect_mask"][10, 0] = True
    arrays["number"][10, 0, 0] = semantic_number
    arrays["code_index"][10, 0] = 1
    arrays["code_dictionary"][1] = np.linspace(-1, 1, 384)
    lookup = StaticSemanticLookup(card_vocabulary=vocabulary, category_to_index=categories or {"<PAD>": 0, "<UNK>": 1}, requirement_to_index={}, semantic_card_slots={14558127: (0,)}, runtime_effect_bindings={(14558127, 14558127 * 16): 0}, **arrays)
    table = root / "semantic_lookup_v1.npz"
    np.savez(table, **arrays)
    catalog = {
        "format_version": 1, "table_file": table.name, "table_size": table.stat().st_size,
        "table_sha256": hashlib.sha256(table.read_bytes()).hexdigest(),
        "card_vocab_hash": vocabulary.vocabulary_hash, "card_vocab_size": 32,
        "card_vocab_card_count": vocabulary.card_count, "semantic_lookup_card_count": vocabulary.card_count,
        "semantic_lookup_hash": lookup.prefix_hash(),
        "arrays": {name: {"dtype": array.dtype.str, "shape": list(array.shape)} for name, array in arrays.items()},
        "category_to_index": lookup.category_to_index, "requirement_to_index": {},
        "semantic_card_slots": {"14558127": [0]},
        "runtime_effect_bindings": [[14558127, 14558127 * 16, 0]],
        "public_hint_binding_format": 1, "public_hint_bindings": [],
    }
    if code_contract is not None:
        catalog["code_encoder_contract"] = code_contract
    (root / "semantic_lookup_v1.json").write_text(json.dumps(catalog), encoding="utf-8")
    return load_v4_assets(root)


# 构造能覆盖卡片动作卡组与动态属性的完整 V4 离线快照
def v4_snapshot(player=0):
    global_data = GlobalFeature(turn_count=3, phase_id=4, to_play=player, my_lp=7000, op_lp=8000,
        my_hand_len=1, op_hand_len=2, my_deck_len=2, op_deck_len=35, my_grave_len=0, op_grave_len=0,
        my_removed_len=0, op_removed_len=0, my_extra_len=0, op_extra_len=0,
        decision_player=player, turn_player=1, starting_player=0, p0_turn_count=2, p1_turn_count=1)
    card = CardEntity(code=14558127, owner=0, location=2, sequence=0, position=1,
        current_atk=0, current_def=1800, type_mask=33, race=16, attribute=16, level=3,
        base_atk=0, base_def=1800, is_public=False)
    actions = [GameAction(action_type=13, index=0, operation_id=2, target_entity_idx=0,
        target_location_raw=0 | 2 << 8 | 0 << 16 | 1 << 24),
        GameAction(action_type=13, index=1, operation_id=1)]
    from model_protocols.v4.data_types import StateTransitionEvent
    from model_protocols.v4.public_observation import PublicHint, KnownDeckCard
    location = 0 | 2 << 8 | 0 << 16 | 1 << 24
    event = StateTransitionEvent(sequence_id=1, actor=0, turn_count=3, phase_id=4,
        prompt_type=13, operation_id=2, summon_method_id=0, source_code=14558127,
        source_location_raw=location, effect_slot=-1, target_locations=(location,),
        target_codes=(14558127,), target_count=1, event_visibility_mask=3,
        intent_visibility_mask=3, source_visibility_mask=3, target_visibility_mask=3,
        boundary=1, result_flags=1, message_types=(70, 120), message_count=2,
        lp_delta_p0=-500, lp_delta_p1=0, zone_deltas=(0,) * 16,
        chain_depth_before=0, chain_depth_after=0, max_chain_depth=1,
        public_notifications=((120, 14558127, location, 1, 0, 0, 0),))
    return GameSnapshot(global_data, [card], actions, p0_deck_codes=[14558127, 89631139],
        p0_initial_deck_codes=[14558127, 14558127, 89631139], p1_initial_deck_codes=[],
        p0_initial_extra_codes=[], p1_initial_extra_codes=[], transition_history=[event],
        public_hints=(PublicHint(0, location, 1, 3, turn=2),),
        known_deck_cards=(KnownDeckCard(0, 0, 89631139),))


# 创建同一轮次且使用固定小网络的安全 PTH 检查点
def write_v4_checkpoint(root, assets):
    import torch
    from model_protocols.v4.galatea_net import GalateaNet
    from model_protocols.v4.runtime_context import use_assets
    config = {**assets.identity, "d_model": 32, "n_heads": 4, "n_layers": 1, "vocab_size": 32}
    from model_protocols.v4.semantic_compatibility import semantic_compatibility_descriptor
    compatibility = semantic_compatibility_descriptor(assets.lookup)
    if compatibility is not None:
        config["semantic_compatibility"] = compatibility
    with use_assets(assets):
        net = GalateaNet(config).cpu().eval()
    metadata = {**assets.identity, "checkpoint_format_version": 3, "model_id": "00000000-0000-0000-0000-000000000404",
                "model_prefix": "galatea", "iteration": 1, "run_id": "v4-test", "net_config": config,
                "model_state_dict": net.state_dict()}
    path = Path(root) / "galatea_iter_1.pth"
    torch.save(metadata, path)
    return path, metadata
