import asyncio
import copy
import functools
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from app_config import (
    AppConfig,
    DecisionConfig,
    GameChatConfig,
    LINK_PROJECT_ROOT,
    LlmConfig,
    load_app_config,
    resolve_link_resource_path,
)
from agents.ai_bot import AiBot
from agents.decision_policy import DecisionOutcome, InterventionPolicy
from agents.llm_client import LlmDecisionSkipped, create_llm_client
from action_candidates import MACRO_ACTION_MSGS, build_macro_action_pool
from core.decision_runtime import DecisionCoordinator, DecisionRequest
from core.gamestate import DuelState, INTERACTION_MESSAGE_TYPES
from core.game_chat import (
    STOC_CHAT,
    GameChatHistory,
    classify_game_chat_role,
    decode_stoc_chat_payload,
)
from core.link_events import LinkEventBus
from core.llm_prompt_context import build_llm_static_context
from core.network import (
    CTOS_TIME_CONFIRM,
    PLAYERCHANGE_LEAVE,
    PLAYERCHANGE_NOTREADY,
    PLAYERCHANGE_OBSERVE,
    PLAYERCHANGE_READY,
    STOC_HS_PLAYER_CHANGE,
    STOC_SELECT_HAND,
    STOC_TIME_LIMIT,
    YgoNetClient,
    _console_print,
    build_tp_result,
    describe_win_reason,
    is_select_tp_request,
    parse_duel_player_id,
    parse_error_message,
    parse_lobby_player_change,
    parse_time_limit,
    parse_type_change,
    parse_win_message,
)
from core.observation import LlmObservationBuilder
from core.parser import OCGParser
from core.rule_bot import get_rule_decision
from core.runtime_api import GalateaRuntimeApi
from core.remote_decisions import RemoteDecisionBroker
from utils.card_reader import card_db
from utils.deck_utils import load_deck

class GalateaLink:
    def __init__(
        self,
        host,
        port,
        password,
        deck_name,
        *,
        protocol_version=0x1361,
        auto_negotiate_version=True,
        max_version_retries=1,
        game_id=0,
        connect_timeout=10.0,
        trace_packets=False,
        agent_name="Galatea_AI",
        prefer_second=None,
        model_device="cpu",
        weights_path="./models/galatea_iter_30.pth",
        expected_model_id="",
        model_protocol="auto",
        model_inference_backend="auto",
        model_onnx_providers=("CPUExecutionProvider",),
        model_onnx_intra_op_threads=0,
        model_assets_path="",
        strict_model_asset_hashes=True,
        net_config=None,
        llm_config=None,
        decision_config=None,
        game_chat_config=None,
    ):
        self.client = YgoNetClient(
            host,
            port,
            self.handle_server_msg,
            protocol_version=protocol_version,
            game_id=game_id,
            connect_timeout=connect_timeout,
            trace_packets=trace_packets,
        )
        self.password = password
        self.agent_name = agent_name
        self.configured_protocol_version = int(protocol_version)
        self.active_protocol_version = int(protocol_version)
        self.negotiated_protocol_version = None
        self.auto_negotiate_version = bool(auto_negotiate_version)
        self.max_version_retries = max(0, int(max_version_retries))
        self.protocol_version_retry_count = 0
        self._required_protocol_version = None
        self.event_bus = LinkEventBus()
        self.duel_active = False
        self.is_room_host = False
        self.room_duel_mode = 0
        self.room_ready = {0: False, 1: False, 2: False, 3: False}
        self._ready_seat = None
        self._start_requested = False
        self._pending_duel_result = None
        self.last_duel_result = None
        self._closed = False
        configured_asset_path = (
            Path(resolve_link_resource_path(model_assets_path))
            if model_assets_path
            else None
        )
        default_asset_path = LINK_PROJECT_ROOT / "model_assets" / "v3"
        self.model_assets_path = (
            configured_asset_path
            if configured_asset_path is not None
            else default_asset_path
            if default_asset_path.is_dir()
            else LINK_PROJECT_ROOT
        )

        _console_print("🤖 正在唤醒 Galatea AI...")
        self.ai = AiBot(
            device=model_device,
            net_config=net_config,
            initialize_network=False,
        )
        self.ai.env = None
        self.core_model_loaded = self.ai.load_model(
            resolve_link_resource_path(weights_path),
            expected_model_id=expected_model_id or None,
            protocol=model_protocol,
            inference_backend=model_inference_backend,
            onnx_providers=tuple(model_onnx_providers),
            onnx_intra_op_threads=model_onnx_intra_op_threads,
            asset_path=configured_asset_path,
            strict_asset_hashes=strict_model_asset_hashes,
        )
        if not self.core_model_loaded:
            _console_print("⚠️ Core 模型不可用，当前进程将使用 LLM 或 RuleBot 安全回退")

        # 让规则回退与可见观察读取当前模型包携带的卡库
        selected_card_database = self.model_assets_path / "cards.cdb"
        card_db.reload(
            selected_card_database
            if selected_card_database.is_file()
            else LINK_PROJECT_ROOT / "cards.cdb"
        )

        _console_print(f"🃏 正在加载卡组: {deck_name}")
        self.deck = load_deck(str(LINK_PROJECT_ROOT / "decks"), deck_name)
        if self.deck is None:
            raise FileNotFoundError(f"\\n❌ 找不到卡组文件！")
            
        second_hand_keywords = ["后手", "Going Second", "Second Hand", "后攻", "后手位", "后攻位"]
        inferred_preference = any(kw in deck_name for kw in second_hand_keywords)
        self.prefer_second = inferred_preference if prefer_second is None else prefer_second
        _console_print(f"🎲 战术偏好: {'后攻 (Going Second)' if self.prefer_second else '先攻 (Going First)'}")
        
        self.gamestate = DuelState(
            p0_main=self.deck.main, p0_extra=self.deck.extra,
            p1_main=[], p1_extra=[],
            model_protocol_version=self.ai.model_protocol_version,
            asset_dir=self.model_assets_path,
        )
        self.ignore_actions_blacklist = [] 
        
        # 动态座位 ID 默认设为 None 并在进房后由服务器指派
        self.ai_player_id = None
        self.ai_core_player_id = None
        self.time_player = None
        self.time_left = {0: None, 1: None}
        self.last_decision = None
        self.last_prompt_type = None
        self.last_prompt_msg = None
        self.observation_builder = LlmObservationBuilder()
        self.observation_sequence = 0
        self.latest_observation = None
        self.decision_policy = InterventionPolicy(decision_config or DecisionConfig())
        self.remote_decisions = RemoteDecisionBroker(self.event_bus)
        active_llm_config = llm_config or LlmConfig()
        self.llm_client = (
            create_llm_client(active_llm_config)
            if self.decision_policy.config.agent_backend == "local"
            else None
        )
        if self.llm_client is not None and active_llm_config.cache_static_context:
            self.llm_client.set_static_context(
                build_llm_static_context(
                    self.deck.main,
                    self.deck.extra,
                    deck_name=self.deck.name,
                    agent_name=self.agent_name,
                    include_card_text=active_llm_config.cache_deck_text,
                )
            )
        self.ignore_choice_ids_blacklist = []
        self.last_decision_choice_id = None
        self.last_decision_source = None
        self.latest_chat_suggestion = None
        self.game_chat_config = game_chat_config or GameChatConfig()
        self.game_chat_history = GameChatHistory()
        self.policy_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="galatea-policy",
        )
        self.decision_coordinator = DecisionCoordinator(
            self._compute_decision,
            self._commit_decision,
            self._handle_decision_error,
        )
        self.runtime = GalateaRuntimeApi(self, self.event_bus)

    async def start(self):
        attempted_versions = set()
        try:
            while not self._closed:
                active_version = int(self.client.protocol_version)
                self.active_protocol_version = active_version
                self._required_protocol_version = None
                attempted_versions.add(active_version)
                self._publish_event(
                    "connection.starting",
                    {
                        "host": self.client.host,
                        "port": self.client.port,
                        "protocol_version": active_version,
                        "version_retry": self.protocol_version_retry_count,
                    },
                )
                if not await self.client.connect():
                    self._publish_event(
                        "connection.failed",
                        {"protocol_version": active_version},
                    )
                    return
                self._publish_event(
                    "connection.connected",
                    {
                        "host": self.client.host,
                        "port": self.client.port,
                        "protocol_version": active_version,
                    },
                )
                await self.client.send_player_info(self.agent_name)
                await self.client.send_join_game(self.password)
                await self.client.wait_for_disconnect()
                if getattr(self, "duel_active", False):
                    await self._finish_duel("connection_lost")
                required_version = self._required_protocol_version
                can_retry = (
                    self.auto_negotiate_version
                    and required_version is not None
                    and 0 < required_version <= 0xFFFFFFFF
                    and required_version not in attempted_versions
                    and self.protocol_version_retry_count < self.max_version_retries
                )
                if not can_retry:
                    return
                self.protocol_version_retry_count += 1
                self.negotiated_protocol_version = required_version
                self.client.protocol_version = required_version
                await self.client.close()
                _console_print(
                    "🔁 服务端要求协议版本 "
                    f"0x{required_version:X}，正在自动重连 "
                    f"({self.protocol_version_retry_count}/{self.max_version_retries})"
                )
                self._publish_event(
                    "connection.protocol_version_retrying",
                    {
                        "previous_version": active_version,
                        "required_version": required_version,
                        "retry": self.protocol_version_retry_count,
                        "max_retries": self.max_version_retries,
                    },
                )
        finally:
            await self.close()

    # 关闭决策、网络和策略线程资源
    async def close(self):
        if self._closed:
            return
        self._closed = True
        self._publish_event("link.closing")
        try:
            if getattr(self, "duel_active", False):
                await self._finish_duel("link_stopped")
            await self.decision_coordinator.close()
            await self.client.close()
            if self.llm_client is not None:
                await self.llm_client.close()
            await asyncio.to_thread(
                self.policy_executor.shutdown,
                wait=True,
                cancel_futures=True,
            )
        finally:
            self.duel_active = False
            self._publish_event("link.closed")
            self.event_bus.close()

    async def handle_server_msg(self, msg_type, msg_data):
        try:
            if msg_type == 0x12: # STOC_JOIN_GAME
                _console_print("✅ 成功加入房间！正在上传卡组...")
                self.room_duel_mode = msg_data[5] if len(msg_data) >= 6 else 0
                self.room_ready = {0: False, 1: False, 2: False, 3: False}
                self._ready_seat = None
                self._start_requested = False
                self._publish_event(
                    "room.joined",
                    {"duel_mode": self.room_duel_mode},
                )
                await self.client.send_deck(self.deck.main, self.deck.extra)
                # 注意：这里先不要发 send_ready()，等服务器下发 0x13 之后再发

            elif msg_type == 0x13: # STOC_TYPE_CHANGE
                if len(msg_data) >= 1:
                    player_id, is_host = parse_type_change(msg_data)
                    previous_player_id = self.ai_player_id
                    self._assign_ai_player(player_id)
                    self.is_room_host = is_host
                    if previous_player_id != player_id:
                        self._ready_seat = None
                    _console_print(
                        f"🪑 服务器分配座位: Player {self.ai_player_id} "
                        f"({'房主' if self.is_room_host else '参与者'})"
                    )
                    self._publish_event(
                        "room.role.updated",
                        {
                            "player_id": player_id,
                            "is_host": is_host,
                        },
                    )

                # 拿到决斗座位后发送准备信号并等待房主启动
                if self.ai_player_id in (0, 1, 2, 3) and self._ready_seat != self.ai_player_id:
                    await self.client.send_ready()
                    self._ready_seat = self.ai_player_id

            elif msg_type == STOC_HS_PLAYER_CHANGE:
                player_id, state = parse_lobby_player_change(msg_data)
                if state < 8:
                    self.room_ready[player_id] = False
                    self.room_ready[state] = False
                    self._start_requested = False
                elif state == PLAYERCHANGE_READY:
                    self.room_ready[player_id] = True
                elif state in {
                    PLAYERCHANGE_OBSERVE,
                    PLAYERCHANGE_NOTREADY,
                    PLAYERCHANGE_LEAVE,
                }:
                    self.room_ready[player_id] = False
                    self._start_requested = False
                self._publish_event(
                    "room.player.updated",
                    {
                        "player_id": player_id,
                        "state": state,
                        "ready": self.room_ready.get(player_id, False),
                        "ready_players": [
                            seat for seat, ready in self.room_ready.items() if ready
                        ],
                    },
                )
                await self._start_duel_if_host_ready()
                
            elif msg_type == STOC_TIME_LIMIT:
                time_player, left_time = parse_time_limit(msg_data)
                self.time_player = time_player
                self.time_left[time_player] = left_time
                is_own_timer = time_player == getattr(
                    self,
                    "ai_core_player_id",
                    None,
                )
                self._publish_event(
                    "duel.time.updated",
                    {
                        "core_player_id": time_player,
                        "left_time": left_time,
                        "is_self": is_own_timer,
                    },
                )
                if is_own_timer:
                    await self.client.send_packet(CTOS_TIME_CONFIRM)
                
            elif msg_type == 0x02: # STOC_ERROR_MSG
                if not msg_data:
                    _console_print("🚨 [服务器错误] 收到空错误包")
                    self._publish_event("server.error", {"error_type": None})
                    return
                err_type, err_code = parse_error_message(msg_data)
                if err_type == 2 and err_code is not None:
                    _console_print(f"\n🚨 [卡组被拒] 违规卡片 Code: {err_code}\n")
                    self._publish_event(
                        "server.error",
                        {"error_type": err_type, "card_code": err_code},
                    )
                elif err_type == 4 and err_code is not None:
                    self._required_protocol_version = err_code
                    _console_print(
                        "⚠️ [协议版本被拒] "
                        f"当前 0x{self.active_protocol_version:X} "
                        f"服务端要求 0x{err_code:X}"
                    )
                    self._publish_event(
                        "connection.protocol_version_rejected",
                        {
                            "configured_version": self.configured_protocol_version,
                            "active_version": self.active_protocol_version,
                            "required_version": err_code,
                            "auto_retry_enabled": self.auto_negotiate_version,
                            "retry_count": self.protocol_version_retry_count,
                            "max_retries": self.max_version_retries,
                        },
                    )
                else:
                    self._publish_event(
                        "server.error",
                        {"error_type": err_type, "error_code": err_code},
                    )
                    
            # 决斗开始 (0x15)
            elif msg_type == 0x15 and len(msg_data) == 0:
                _console_print("⚔️ 决斗房间已锁定！进入战前准备阶段。")
                await self._reset_duel_state()
                await self.runtime.clear_game_chat_history(source="duel.started")
                self.duel_active = True
                self._pending_duel_result = None
                self._start_requested = False
                self._publish_event("duel.started")

            # 大厅退人 (0x14) 或决斗结束 (0x16)
            elif msg_type in [0x14, 0x16] and len(msg_data) == 0:
                if self.duel_active:
                    await self._finish_duel(
                        "server_duel_end" if msg_type == 0x16 else "server_leave",
                        server_message_type=msg_type,
                    )
                else:
                    self._publish_event(
                        "room.left",
                        {"server_message_type": msg_type},
                    )

            elif msg_type == STOC_SELECT_HAND and len(msg_data) == 0:
                import random
                await self.client.send_packet(0x03, bytes([random.choice([1, 2, 3])]))

            elif is_select_tp_request(msg_type, msg_data):
                _console_print(f"👑 猜拳获胜！AI 按偏好选择: {'后攻' if self.prefer_second else '先攻'}...")
                tp_choice = build_tp_result(self.prefer_second)
                await self.client.send_packet(0x04, bytes([tp_choice]))
            
            elif msg_type == STOC_CHAT:
                try:
                    player_type, text = decode_stoc_chat_payload(msg_data)
                    chat_ai_player_id = (
                        getattr(self, "ai_core_player_id", None)
                        if self.duel_active
                        else self.ai_player_id
                    )
                    role = classify_game_chat_role(
                        player_type,
                        chat_ai_player_id,
                    )
                    self.runtime.record_incoming_game_chat(
                        player_type=player_type,
                        role=role,
                        text=text,
                    )
                    _console_print(
                        f"💬 [游戏聊天] player_type={player_type} role={role}: {text}"
                    )
                except (TypeError, ValueError) as error:
                    _console_print(f"⚠️ 游戏聊天解析失败: {error}")
                    self._publish_event(
                        "game_chat.decode_failed",
                        {
                            "error": str(error),
                            "payload_size": len(msg_data),
                        },
                    )

            elif msg_type == 0x01:
                msgs = OCGParser.robust_parse(msg_data)

                for ocg_type, ocg_payload in msgs:
                    raw_msg = bytes([ocg_type]) + ocg_payload

                    if ocg_type == 5:
                        self._capture_duel_result(ocg_payload)

                    if ocg_type == 4:
                        duel_player_id = parse_duel_player_id(ocg_payload)
                        if duel_player_id is not None:
                            self._assign_ai_core_player(
                                duel_player_id,
                                source="msg_start",
                            )
                    
                    # === 修复 1：完美处理 RETRY 死锁 ===
                    if ocg_type == 1:
                        _console_print(f"⚠️ [引擎警告] 操作不合法，触发重试 (MSG_RETRY)")
                        if self.last_decision is not None:
                            self.ignore_actions_blacklist.append(self.last_decision)
                        if self.last_decision_choice_id is not None:
                            self.ignore_choice_ids_blacklist.append(
                                self.last_decision_choice_id
                            )

                        # 引擎在等你，必须立刻重新发起决策！
                        if (
                            self.gamestate.current_valid_actions
                            and self.last_prompt_type is not None
                            and self.last_prompt_msg is not None
                        ):
                            _console_print("🔄 正在触发重试决策...")
                            await self._schedule_decision(
                                self.last_prompt_type,
                                self.last_prompt_msg,
                            )
                        continue 
                    
                    # 只有非重试指令才重置黑名单（你原来的代码已有，保持即可）
                    if ocg_type in INTERACTION_MESSAGE_TYPES:
                        self.ignore_actions_blacklist = []
                        self.ignore_choice_ids_blacklist = []

                    self.gamestate.update(ocg_type, ocg_payload)
                    if (
                        ocg_type in INTERACTION_MESSAGE_TYPES
                        and self.ai_player_id in (0, 1)
                        and self.gamestate.active_player in (0, 1)
                    ):
                        self._assign_ai_core_player(
                            self.gamestate.active_player,
                            source=f"interaction_{ocg_type}",
                        )
                    snapshot = await self._prepare_prompt_snapshot(
                        ocg_type,
                        ocg_payload,
                    )
                    self._capture_llm_observation(ocg_type, snapshot)

                    if self.gamestate.current_valid_actions:
                        # 服务端只把完整交互消息发给需要作答的决斗客户端
                        if self.ai_player_id in (0, 1):
                            self.last_prompt_type = ocg_type # 记录当前提示类型，供 Retry 使用
                            self.last_prompt_msg = raw_msg
                            await self._schedule_decision(ocg_type, raw_msg, snapshot)
                        else:
                            _console_print("⏳ [观战隔离] 非决斗座位收到交互消息，AI 不会响应")
                            self.gamestate.current_valid_actions = []

        except Exception as e:
            _console_print(f"❌ 消息处理崩溃: {e}")
            self._publish_event(
                "message.processing_failed",
                {"server_message_type": msg_type, "error": str(e)},
            )
            traceback.print_exc()

    # 创建独立快照并提交后台决策任务
    async def _schedule_decision(self, msg_type, raw_msg, snapshot=None):
        snap = snapshot or self.gamestate.get_snapshot()
        if snapshot is None:
            self._capture_llm_observation(msg_type, snap)

        decision_observation = copy.deepcopy(self.latest_observation)
        if decision_observation is not None and self.ignore_choice_ids_blacklist:
            decision_observation["legal_actions"] = [
                action
                for action in decision_observation.get("legal_actions", [])
                if action.get("choice_id") not in self.ignore_choice_ids_blacklist
            ]
            decision_observation["decision_required"] = bool(
                decision_observation["legal_actions"]
            )

        _console_print(f"\\n--- 🎯 引擎提示 Type {msg_type}，当前可用选项 [{len(snap.valid_actions)} 个] ---")
        for idx, act in enumerate(snap.valid_actions):
            code_str = f" (Code: {act.code})" if hasattr(act, 'code') and act.code else ""
            _console_print(f"  [{idx}] 动作类型: {act.action_type}, YGO索引: {act.index}{code_str}, 描述: {act.desc_str}")
        _console_print("---------------------------------------------------------")

        request = await self.decision_coordinator.submit(
            msg_type,
            raw_msg,
            snap,
            tuple(self.ignore_actions_blacklist),
            observation=decision_observation,
        )
        if decision_observation is not None:
            decision_observation["decision_request_id"] = request.request_id
            self.latest_observation = decision_observation
        self._publish_event(
            "decision.requested",
            {
                "request_id": request.request_id,
                "message_type": msg_type,
                "legal_action_count": len(
                    decision_observation.get("legal_actions", [])
                    if decision_observation
                    else []
                ),
            },
        )
        _console_print(f"🧭 已提交异步决策请求 #{request.request_id}")

    # 异步构建普通或复杂宏动作提示快照
    async def _prepare_prompt_snapshot(self, msg_type, msg_payload):
        if (
            msg_type not in MACRO_ACTION_MSGS
            or self.ai_player_id not in (0, 1)
        ):
            return self.gamestate.get_snapshot()
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self.policy_executor,
            functools.partial(
                self._prepare_macro_snapshot,
                msg_type,
                msg_payload,
            ),
        )

    # 使用两阶段模型偏好生成可直接提交的复杂宏动作候选
    def _prepare_macro_snapshot(self, msg_type, msg_payload):
        base_actions = list(self.gamestate.current_valid_actions)
        base_snapshot = self.gamestate.get_snapshot()
        probabilities = [1.0] * len(base_actions)

        if self.decision_policy.should_run_core() and getattr(
            self.ai,
            "model_available",
            False,
        ):
            try:
                probabilities = self.ai.get_action_probabilities_from_snapshot(
                    base_snapshot
                )
            except Exception as error:
                _console_print(f"🧠 Core 宏动作意图计算异常，使用均匀候选权重: {error}")

        try:
            macro_actions = build_macro_action_pool(
                msg_type,
                msg_payload,
                self.gamestate,
                base_actions,
                probabilities,
                max_actions=self.ai.max_actions,
            )
        except Exception as error:
            _console_print(f"⚠️ 复杂宏动作候选生成失败并强制使用 RuleBot: {error}")
            base_snapshot.force_rule_fallback = True
            return base_snapshot
        if not macro_actions:
            _console_print("⚠️ 未生成复杂宏动作候选并强制使用 RuleBot")
            base_snapshot.force_rule_fallback = True
            return base_snapshot

        self.gamestate.current_valid_actions = macro_actions
        return self.gamestate.get_snapshot()

    # 保存每条游戏消息处理后的 LLM 可见观察
    def _capture_llm_observation(self, event_type, snapshot):
        self.observation_sequence += 1
        observation = self.observation_builder.build(
            snapshot,
            player_id=self._perspective_player_id(),
            event_type=event_type,
        )
        observation["observation_id"] = self.observation_sequence
        observation["own_deck_identity"] = {
            "deck_ref": getattr(self.deck, "reference", self.deck.name),
            "display_name": self.deck.name,
            "source": copy.deepcopy(
                getattr(
                    self.deck,
                    "source",
                    {"kind": "link_local", "label": "Link 本地卡组"},
                )
            ),
        }
        self.latest_observation = observation
        self._publish_event(
            "observation.updated",
            {
                "observation_id": observation["observation_id"],
                "event": copy.deepcopy(observation.get("event")),
                "turn": copy.deepcopy(observation.get("turn")),
                "decision_required": observation.get("decision_required", False),
                "legal_action_count": len(observation.get("legal_actions", [])),
            },
        )

    # 返回最新观察副本供后续 LLM 和 AstrBot 接口读取
    def get_latest_llm_observation(self):
        return copy.deepcopy(self.latest_observation)

    # 安全发布事件并兼容仅构造部分属性的单元测试实例
    def _publish_event(self, event_type, payload=None):
        event_bus = getattr(self, "event_bus", None)
        if event_bus is None:
            return None
        return event_bus.publish(event_type, payload)

    # 返回玩家视角并让观战或未分配状态安全回退
    def _perspective_player_id(self):
        core_player_id = getattr(self, "ai_core_player_id", None)
        return core_player_id if core_player_id in (0, 1) else 0

    # 同步服务器大厅座位
    def _assign_ai_player(self, player_id):
        self.ai_player_id = player_id
        self._publish_event(
            "room.seat.assigned",
            {"player_id": player_id, "is_duelist": player_id in (0, 1)},
        )

    # 同步对局内 Core 玩家编号并映射己方卡组
    def _assign_ai_core_player(self, player_id, *, source):
        if player_id not in (0, 1):
            raise ValueError(f"Core 玩家编号非法: {player_id}")
        previous_player_id = getattr(self, "ai_core_player_id", None)
        if previous_player_id == player_id:
            self.ai.player_id = player_id
            return
        self.ai_core_player_id = player_id
        self.ai.player_id = player_id
        if player_id == 0:
            self.gamestate.p0_deck = list(self.deck.main)
            self.gamestate.p0_extra = list(self.deck.extra)
            self.gamestate.p1_deck = []
            self.gamestate.p1_extra = []
        elif player_id == 1:
            self.gamestate.p0_deck = []
            self.gamestate.p0_extra = []
            self.gamestate.p1_deck = list(self.deck.main)
            self.gamestate.p1_extra = list(self.deck.extra)
        self._publish_event(
            "duel.player.assigned",
            {
                "core_player_id": player_id,
                "previous_core_player_id": previous_player_id,
                "source": source,
            },
        )
        _console_print(f"🧭 对局视角映射: Core Player {player_id} ({source})")

    # 在全部决斗者准备后由房主自动请求开始
    async def _start_duel_if_host_ready(self):
        if not getattr(self, "is_room_host", False) or self.duel_active:
            return
        required_players = (0, 1, 2, 3) if self.room_duel_mode == 2 else (0, 1)
        if self._start_requested or not all(
            self.room_ready.get(player_id, False)
            for player_id in required_players
        ):
            return
        self._start_requested = True
        _console_print("▶️ 双方已经准备，房主正在自动开始对局")
        self._publish_event(
            "room.start.requested",
            {
                "player_id": self.ai_player_id,
                "ready_players": list(required_players),
                "duel_mode": self.room_duel_mode,
            },
        )
        await self.client.send_start_duel()

    # 保存 Core 胜负消息并转换为己方视角结果
    def _capture_duel_result(self, payload):
        winner, reason = parse_win_message(payload)
        self_player_id = getattr(self, "ai_core_player_id", None)
        if winner == 2:
            outcome = "draw"
            winner_role = "draw"
        elif self_player_id not in (0, 1):
            outcome = "unknown"
            winner_role = "unknown"
        elif winner == self_player_id:
            outcome = "self_win"
            winner_role = "self"
        else:
            outcome = "opponent_win"
            winner_role = "opponent"
        result = {
            "schema_version": "galatea.duel_result.v1",
            "outcome": outcome,
            "winner_role": winner_role,
            "winner_core_player_id": winner,
            "self_core_player_id": self_player_id,
            "reason_code": reason,
            "reason": describe_win_reason(reason),
            "termination": "connection_lost" if reason == 0x04 else "engine_result",
        }
        self._pending_duel_result = result
        self.last_duel_result = copy.deepcopy(result)
        self._publish_event("duel.result", copy.deepcopy(result))

    # 统一发布正常结束、断线和主动停止的对局结果
    async def _finish_duel(self, termination, *, server_message_type=None):
        if not getattr(self, "duel_active", False):
            return
        self.duel_active = False
        result = copy.deepcopy(getattr(self, "_pending_duel_result", None))
        if result is None:
            reason_names = {
                "connection_lost": "连接中断",
                "link_stopped": "Link 会话被主动停止",
                "server_leave": "玩家离开或房间关闭",
                "server_duel_end": "服务端结束对局但未收到胜负消息",
            }
            result = {
                "schema_version": "galatea.duel_result.v1",
                "outcome": "unknown",
                "winner_role": "unknown",
                "winner_core_player_id": None,
                "self_core_player_id": getattr(self, "ai_core_player_id", None),
                "reason_code": None,
                "reason": reason_names.get(str(termination), "未知结束原因"),
                "termination": str(termination),
            }
        elif result.get("termination") == "engine_result":
            result["termination"] = str(termination)
        result["server_message_type"] = server_message_type
        self.last_duel_result = copy.deepcopy(result)
        self._pending_duel_result = None
        _console_print(f"🏁 决斗会话已经结束: {result['reason']}")
        self._publish_event("duel.ended", result)
        await self._reset_duel_state()

    # 重置单局状态并让未完成的旧决策失效
    async def _reset_duel_state(self):
        await self.decision_coordinator.cancel_active()
        self.gamestate.reset()
        self.ai_core_player_id = None
        self.time_player = None
        self.time_left = {0: None, 1: None}
        self.gamestate.p0_deck = list(self.deck.main)
        self.gamestate.p0_extra = list(self.deck.extra)
        self.gamestate.p1_deck = []
        self.gamestate.p1_extra = []
        self.ignore_actions_blacklist = []
        self.last_decision = None
        self.last_prompt_type = None
        self.last_prompt_msg = None
        self.latest_observation = None
        self.ignore_choice_ids_blacklist = []
        self.last_decision_choice_id = None
        self.last_decision_source = None
        self.latest_chat_suggestion = None

    # 按介入策略执行 Core、LLM 和规则兜底
    async def _compute_decision(self, request: DecisionRequest):
        if getattr(request.snapshot, "force_rule_fallback", False):
            rule_decision = await self._compute_rule_decision(request)
            return DecisionOutcome(response=rule_decision, source="rule")

        core_decision = None
        if self.decision_policy.should_run_core() and not request.ignore_actions:
            core_decision = await self._compute_core_decision(request)
            if core_decision is not None:
                self._publish_event(
                    "core.suggested",
                    {
                        "request_id": request.request_id,
                        "choice_id": core_decision.choice_id,
                        "confidence": core_decision.confidence,
                        "probability_margin": core_decision.probability_margin,
                        "policy_mode": core_decision.policy_mode,
                        "temperature": core_decision.temperature,
                    },
                )

        should_use_llm = self.decision_policy.should_use_llm(
            request.msg_type,
            core_decision,
        )
        remote_agent = self.decision_policy.config.agent_backend == "remote_astrbot"
        if should_use_llm and (remote_agent or self.llm_client is not None) and request.observation:
            self._publish_event(
                "llm.requested",
                {
                    "request_id": request.request_id,
                    "message_type": request.msg_type,
                    "core_available": core_decision is not None,
                    "agent_backend": self.decision_policy.config.agent_backend,
                    "time_budget": self.decision_policy.config.llm_time_budget,
                },
            )
            try:
                llm_observation = self.decision_policy.attach_core_suggestion(
                    copy.deepcopy(request.observation),
                    core_decision,
                )
                runtime = getattr(self, "runtime", None)
                if runtime is not None:
                    llm_observation["runtime_controls"] = runtime.get_controls()
                    chat_context = runtime.get_llm_game_chat_context()
                    if chat_context is not None:
                        llm_observation["game_chat_context"] = chat_context
                async with asyncio.timeout(
                    self.decision_policy.config.llm_time_budget
                ):
                    if remote_agent:
                        llm_observation["decision_request_id"] = request.request_id
                        llm_decision = await self.remote_decisions.decide(
                            request.request_id,
                            llm_observation,
                            self.decision_policy.config.llm_time_budget,
                        )
                    else:
                        llm_decision = await self.llm_client.decide(llm_observation)
                response = self.ai.pack_choice_from_snapshot(
                    request.snapshot,
                    llm_decision.choice_id,
                    request.msg_type,
                )
                _console_print(
                    f"💬 LLM 选择动作 [{llm_decision.choice_id}] "
                    f"理由: {llm_decision.reason or '未提供'}"
                )
                self._publish_event(
                    "llm.completed",
                    {
                        "request_id": request.request_id,
                        "choice_id": llm_decision.choice_id,
                        "reason": llm_decision.reason,
                        "has_chat_message": bool(llm_decision.chat_message),
                        "has_intervention_update": bool(
                            llm_decision.intervention_update
                        ),
                    },
                )
                return DecisionOutcome(
                    response=response,
                    source="astrbot" if remote_agent else "llm",
                    choice_id=llm_decision.choice_id,
                    reason=llm_decision.reason,
                    chat_message=llm_decision.chat_message,
                    intervention_update=llm_decision.intervention_update,
                    core_confidence=(
                        core_decision.confidence if core_decision is not None else None
                    ),
                )
            except TimeoutError:
                _console_print(
                    "💬 LLM 超过决策时间预算并进入安全回退: "
                    f"{self.decision_policy.config.llm_time_budget:.1f} 秒"
                )
                self._publish_event(
                    "llm.timed_out",
                    {
                        "request_id": request.request_id,
                        "time_budget": self.decision_policy.config.llm_time_budget,
                    },
                )
            except LlmDecisionSkipped as error:
                _console_print(f"💬 LLM 本地跳过无效交互: {error}")
                self._publish_event(
                    "llm.skipped",
                    {"request_id": request.request_id, "reason": str(error)},
                )
            except Exception as error:
                _console_print(f"💬 LLM 决策失败并进入安全回退: {error}")
                self._publish_event(
                    "llm.failed",
                    {"request_id": request.request_id, "error": str(error)},
                )

        if core_decision is not None:
            _console_print(
                f"🧠 Core 选择动作 [{core_decision.choice_id}] "
                f"置信度: {core_decision.confidence:.4f}"
            )
            return DecisionOutcome(
                response=core_decision.response,
                source="core",
                choice_id=core_decision.choice_id,
                core_confidence=core_decision.confidence,
            )

        rule_decision = await self._compute_rule_decision(request)
        return DecisionOutcome(response=rule_decision, source="rule")

    # 在线程池中计算带概率信息的 Core 建议
    async def _compute_core_decision(self, request: DecisionRequest):
        if not getattr(self.ai, "model_available", False):
            return None
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                self.policy_executor,
                functools.partial(
                    self.ai.get_scored_decision_from_snapshot,
                    request.snapshot,
                    request.msg_type,
                    policy_mode=self.decision_policy.config.core_policy_mode,
                    temperature=self.decision_policy.config.core_temperature,
                ),
            )
        except Exception as error:
            _console_print(f"🧠 Core 决策异常: {error}")
            return None

    # 在线程池中执行不依赖模型的规则兜底
    async def _compute_rule_decision(self, request: DecisionRequest):
        loop = asyncio.get_running_loop()
        decision = await loop.run_in_executor(
            self.policy_executor,
            functools.partial(
                get_rule_decision,
                self._perspective_player_id(),
                request.msg_type,
                request.raw_msg,
                request.snapshot,
                request.ignore_actions,
                request.snapshot.valid_actions,
            ),
        )
        _console_print(f"⚙️ RuleBot 接管运算 Cosmic Result -> 原始返回: {decision}")
        return decision

    # 提交仍然有效的决策结果并清理当前动作池
    async def _commit_decision(self, request: DecisionRequest, decision):
        outcome = decision
        if not isinstance(outcome, DecisionOutcome):
            outcome = DecisionOutcome(response=decision, source="unknown")
        self.last_decision = outcome.response
        self.last_decision_choice_id = outcome.choice_id
        self.last_decision_source = outcome.source
        game_chat_config = getattr(self, "game_chat_config", GameChatConfig())
        chat_message = None
        if (
            outcome.chat_message
            and game_chat_config.enabled
            and game_chat_config.llm_suggestions_enabled
        ):
            chat_message = outcome.chat_message
        self.latest_chat_suggestion = chat_message
        await self.client.send_decision(outcome.response)
        self._publish_event(
            "decision.committed",
            {
                "request_id": request.request_id,
                "source": outcome.source,
                "choice_id": outcome.choice_id,
                "reason": outcome.reason,
                "core_confidence": outcome.core_confidence,
                "has_intervention_update": bool(outcome.intervention_update),
            },
        )
        await self._apply_decision_controls(request, outcome)
        if chat_message:
            self._publish_event(
                "chat.suggested",
                {
                    "request_id": request.request_id,
                    "message": chat_message,
                    "source": outcome.source,
                },
            )
            if game_chat_config.auto_send_llm_chat:
                try:
                    await self.runtime.send_llm_game_chat(
                        chat_message,
                        request_id=request.request_id,
                    )
                except Exception as error:
                    _console_print(f"⚠️ LLM 游戏聊天自动发送失败: {error}")
                    self._publish_event(
                        "game_chat.auto_send_failed",
                        {
                            "request_id": request.request_id,
                            "error": str(error),
                        },
                    )

    # 推进旧自主覆盖并安全应用本次 LLM 的新宏观调整
    async def _apply_decision_controls(
        self,
        request: DecisionRequest,
        outcome: DecisionOutcome,
    ) -> None:
        runtime = getattr(self, "runtime", None)
        if runtime is None:
            return
        try:
            await runtime.on_decision_committed(request.request_id)
            if outcome.intervention_update is not None:
                await runtime.apply_autonomous_intervention(
                    outcome.intervention_update,
                    request_id=request.request_id,
                )
        except Exception as error:
            _console_print(f"⚠️ 宏观控制更新失败并保持当前策略: {error}")
            self._publish_event(
                "runtime.controls.failed",
                {"request_id": request.request_id, "error": str(error)},
            )

    # 记录未被策略内部兜底处理的决策异常
    async def _handle_decision_error(self, request: DecisionRequest, error: Exception):
        _console_print(f"❌ 决策请求 #{request.request_id} 执行失败: {error}")
        self._publish_event(
            "decision.failed",
            {"request_id": request.request_id, "error": str(error)},
        )
        traceback.print_exception(type(error), error, error.__traceback__)

# 根据应用配置创建对局连接实例
def create_link_from_config(config: AppConfig) -> GalateaLink:
    return GalateaLink(
        config.server.host,
        config.server.port,
        config.server.password,
        config.agent.deck,
        protocol_version=config.server.protocol_version,
        auto_negotiate_version=config.server.auto_negotiate_version,
        max_version_retries=config.server.max_version_retries,
        game_id=config.server.game_id,
        connect_timeout=config.server.connect_timeout,
        trace_packets=config.server.trace_packets,
        agent_name=config.agent.name,
        prefer_second=config.agent.prefer_second,
        model_device=config.model.device,
        weights_path=config.model.weights_path,
        expected_model_id=config.model.expected_model_id,
        model_protocol=config.model.protocol,
        model_inference_backend=config.model.inference_backend,
        model_onnx_providers=config.model.onnx_providers,
        model_onnx_intra_op_threads=config.model.onnx_intra_op_threads,
        model_assets_path=config.model.assets_path,
        strict_model_asset_hashes=config.model.strict_asset_hashes,
        net_config=config.model.config or None,
        llm_config=config.llm,
        decision_config=config.decision,
        game_chat_config=config.game_chat,
    )


if __name__ == "__main__":
    link = create_link_from_config(load_app_config())
    asyncio.run(link.start())
