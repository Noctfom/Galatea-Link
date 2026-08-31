import asyncio
import copy
import functools
import struct
import traceback
from concurrent.futures import ThreadPoolExecutor
from app_config import AppConfig, DecisionConfig, LlmConfig, load_app_config
from agents.ai_bot import AiBot
from agents.decision_policy import DecisionOutcome, InterventionPolicy
from agents.llm_client import create_llm_client
from core.decision_runtime import DecisionCoordinator, DecisionRequest
from core.gamestate import DuelState
from core.network import STOC_SELECT_HAND, YgoNetClient, is_select_tp_request
from core.observation import LlmObservationBuilder
from core.parser import OCGParser
from core.rule_bot import get_rule_decision
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
        game_id=0,
        connect_timeout=10.0,
        trace_packets=False,
        agent_name="Galatea_AI",
        prefer_second=None,
        model_device="cpu",
        weights_path="./models/galatea_iter_110.pth",
        net_config=None,
        llm_config=None,
        decision_config=None,
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

        print("🤖 正在唤醒 Galatea AI...")
        self.ai = AiBot(device=model_device, net_config=net_config)
        self.ai.env = None
        self.ai.load_model(weights_path)
        
        print(f"🃏 正在加载卡组: {deck_name}")
        self.deck = load_deck('./decks', deck_name)
        if self.deck is None:
            raise FileNotFoundError(f"\\n❌ 找不到卡组文件！")
            
        second_hand_keywords = ["后手", "Going Second", "Second Hand", "后攻", "后手位", "后攻位"]
        inferred_preference = any(kw in deck_name for kw in second_hand_keywords)
        self.prefer_second = inferred_preference if prefer_second is None else prefer_second
        print(f"🎲 战术偏好: {'后攻 (Going Second)' if self.prefer_second else '先攻 (Going First)'}")
        
        self.gamestate = DuelState(
            p0_main=self.deck.main, p0_extra=self.deck.extra,
            p1_main=[], p1_extra=[]
        )
        self.ignore_actions_blacklist = [] 
        
        # 🌟 新增：动态座位ID守护者。默认设为 None，进房后由服务器指派
        self.ai_player_id = None
        self.last_decision = None
        self.last_prompt_type = None
        self.last_prompt_msg = None
        self.observation_builder = LlmObservationBuilder()
        self.observation_sequence = 0
        self.latest_observation = None
        self.decision_policy = InterventionPolicy(decision_config or DecisionConfig())
        self.llm_client = create_llm_client(llm_config or LlmConfig())
        self.ignore_choice_ids_blacklist = []
        self.last_decision_choice_id = None
        self.last_decision_source = None
        self.latest_chat_suggestion = None
        self.policy_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="galatea-policy",
        )
        self.decision_coordinator = DecisionCoordinator(
            self._compute_decision,
            self._commit_decision,
            self._handle_decision_error,
        )

    async def start(self):
        if not await self.client.connect():
            await self.close()
            return
        try:
            await self.client.send_player_info(self.agent_name)
            await self.client.send_join_game(self.password)
            while self.client.is_connected:
                await asyncio.sleep(1)
        finally:
            await self.close()

    # 关闭决策、网络和策略线程资源
    async def close(self):
        await self.decision_coordinator.close()
        await self.client.close()
        if self.llm_client is not None:
            await self.llm_client.close()
        await asyncio.to_thread(
            self.policy_executor.shutdown,
            wait=True,
            cancel_futures=True,
        )

    async def handle_server_msg(self, msg_type, msg_data):
        try:
            if msg_type == 0x12: # STOC_JOIN_GAME
                print("✅ 成功加入房间！正在上传卡组...")
                await self.client.send_deck(self.deck.main, self.deck.extra)
                # 注意：这里先不要发 send_ready()，等服务器下发 0x13 之后再发
                
            elif msg_type == 0x13: # STOC_TYPE_CHANGE
                if len(msg_data) >= 1:
                    self._assign_ai_player(msg_data[0] & 0x0F)
                    print(f"🪑 服务器分配座位: Player {self.ai_player_id}")
                
                # 拿到座位号后，立刻发送准备信号
                await self.client.send_ready()
                
            elif msg_type == 0x18: # STOC_TIME_LIMIT
                # 👑 完美的排毒心跳包：MDPro 协议的心跳回复是 0x15 (CTOS_TIME_CONFIRM)
                await self.client.send_packet(0x15) 
                
            elif msg_type == 0x02: # STOC_ERROR_MSG
                if not msg_data:
                    print("🚨 [服务器错误] 收到空错误包")
                    return
                err_type = msg_data[0]
                if err_type == 2 and len(msg_data) >= 8:
                    err_code = struct.unpack('<I', msg_data[4:8])[0]
                    print(f"\n🚨 [卡组被拒] 违规卡片 Code: {err_code}\n")
                elif err_type == 2 and len(msg_data) >= 5:
                    err_code = struct.unpack('<I', msg_data[1:5])[0]
                    print(f"\n🚨 [卡组被拒] 违规卡片 Code: {err_code}\n")
                    
            # 决斗开始 (0x15)
            elif msg_type == 0x15 and len(msg_data) == 0:
                print("⚔️ 决斗房间已锁定！进入战前准备阶段。")
                await self._reset_duel_state()

            # 大厅退人 (0x14) 或决斗结束 (0x16)
            elif msg_type in [0x14, 0x16] and len(msg_data) == 0:
                print("🏁 决斗会话已经结束")
                await self._reset_duel_state()

            elif msg_type == STOC_SELECT_HAND and len(msg_data) == 0:
                import random
                await self.client.send_packet(0x03, bytes([random.choice([1, 2, 3])]))

            elif is_select_tp_request(msg_type, msg_data):
                print(f"👑 猜拳获胜！AI 按偏好选择: {'后攻' if self.prefer_second else '先攻'}...")
                # 0 代表自己先攻，1 代表对方先攻
                tp_choice = 1 if self.prefer_second else 0 
                await self.client.send_packet(0x04, bytes([tp_choice]))
            
            elif msg_type == 0x0C: # STOC_TYPE_CHANGE
                # 服务器下发座位号 (0 是 P1, 1 是 P2, 7 是观战)
                self._assign_ai_player(msg_data[0] & 0x0F)
                print(f"🪑 服务器分配座位: Player {self.ai_player_id}")

            elif msg_type == 0x01:
                msgs = OCGParser.robust_parse(msg_data)

                for ocg_type, ocg_payload in msgs:
                    raw_msg = bytes([ocg_type]) + ocg_payload
                    
                    # === 修复 1：完美处理 RETRY 死锁 ===
                    if ocg_type == 1:
                        print(f"⚠️ [引擎警告] 操作不合法，触发重试 (MSG_RETRY)")
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
                            print("🔄 正在触发重试决策...")
                            await self._schedule_decision(
                                self.last_prompt_type,
                                self.last_prompt_msg,
                            )
                        continue 
                    
                    # 只有非重试指令才重置黑名单（你原来的代码已有，保持即可）
                    if ocg_type in [10, 11, 12, 13, 14, 15, 16, 18, 19, 20, 22, 23, 24, 25, 26, 140, 141, 142, 143]:
                        self.ignore_actions_blacklist = []
                        self.ignore_choice_ids_blacklist = []

                    self.gamestate.update(ocg_type, ocg_payload)
                    snapshot = self.gamestate.get_snapshot()
                    self._capture_llm_observation(ocg_type, snapshot)

                    # === 修复 2：增加座位隔离 (防抢答) ===
                    if self.gamestate.current_valid_actions:
                        # 只有引擎呼叫的 active_player 是 AI 自己时，才响应！
                        if self.ai_player_id is not None and self.gamestate.active_player == self.ai_player_id:
                            self.last_prompt_type = ocg_type # 记录当前提示类型，供 Retry 使用
                            self.last_prompt_msg = raw_msg
                            await self._schedule_decision(ocg_type, raw_msg, snapshot)
                        else:
                            # 🎯 新增：明确打印出 AI 正在等待谁的操作
                            print(f"⏳ [时点移交] 引擎正在等待 Player {self.gamestate.active_player} (对方) 操作，AI 待机中...")
                            self.gamestate.current_valid_actions = []

        except Exception as e:
            print(f"❌ 消息处理崩溃: {e}")
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

        print(f"\\n--- 🎯 引擎提示 Type {msg_type}，当前可用选项 [{len(snap.valid_actions)} 个] ---")
        for idx, act in enumerate(snap.valid_actions):
            code_str = f" (Code: {act.code})" if hasattr(act, 'code') and act.code else ""
            print(f"  [{idx}] 动作类型: {act.action_type}, YGO索引: {act.index}{code_str}, 描述: {act.desc_str}")
        print("---------------------------------------------------------")

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
        print(f"🧭 已提交异步决策请求 #{request.request_id}")

    # 保存每条游戏消息处理后的 LLM 可见观察
    def _capture_llm_observation(self, event_type, snapshot):
        self.observation_sequence += 1
        observation = self.observation_builder.build(
            snapshot,
            player_id=self._perspective_player_id(),
            event_type=event_type,
        )
        observation["observation_id"] = self.observation_sequence
        self.latest_observation = observation

    # 返回最新观察副本供后续 LLM 和 AstrBot 接口读取
    def get_latest_llm_observation(self):
        return copy.deepcopy(self.latest_observation)

    # 返回玩家视角并让观战或未分配状态安全回退
    def _perspective_player_id(self):
        return self.ai_player_id if self.ai_player_id in (0, 1) else 0

    # 同步服务器座位并把己方卡组映射到正确玩家
    def _assign_ai_player(self, player_id):
        self.ai_player_id = player_id
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

    # 重置单局状态并让未完成的旧决策失效
    async def _reset_duel_state(self):
        await self.decision_coordinator.cancel_active()
        self.gamestate.reset()
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
        core_decision = None
        if self.decision_policy.should_run_core() and not request.ignore_actions:
            core_decision = await self._compute_core_decision(request)

        should_use_llm = self.decision_policy.should_use_llm(
            request.msg_type,
            core_decision,
        )
        if should_use_llm and self.llm_client is not None and request.observation:
            try:
                llm_observation = self.decision_policy.attach_core_suggestion(
                    copy.deepcopy(request.observation),
                    core_decision,
                )
                llm_decision = await self.llm_client.decide(llm_observation)
                response = self.ai.pack_choice_from_snapshot(
                    request.snapshot,
                    llm_decision.choice_id,
                    request.msg_type,
                )
                print(
                    f"💬 LLM 选择动作 [{llm_decision.choice_id}] "
                    f"理由: {llm_decision.reason or '未提供'}"
                )
                return DecisionOutcome(
                    response=response,
                    source="llm",
                    choice_id=llm_decision.choice_id,
                    reason=llm_decision.reason,
                    chat_message=llm_decision.chat_message,
                    core_confidence=(
                        core_decision.confidence if core_decision is not None else None
                    ),
                )
            except Exception as error:
                print(f"💬 LLM 决策失败并进入安全回退: {error}")

        if core_decision is not None:
            print(
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
        if not hasattr(self.ai.net, "parameters"):
            return None
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                self.policy_executor,
                functools.partial(
                    self.ai.get_scored_decision_from_snapshot,
                    request.snapshot,
                    request.msg_type,
                ),
            )
        except Exception as error:
            print(f"🧠 Core 决策异常: {error}")
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
        print(f"⚙️ RuleBot 接管运算 Cosmic Result -> 原始返回: {decision}")
        return decision

    # 提交仍然有效的决策结果并清理当前动作池
    async def _commit_decision(self, request: DecisionRequest, decision):
        outcome = decision
        if not isinstance(outcome, DecisionOutcome):
            outcome = DecisionOutcome(response=decision, source="unknown")
        self.last_decision = outcome.response
        self.last_decision_choice_id = outcome.choice_id
        self.last_decision_source = outcome.source
        self.latest_chat_suggestion = outcome.chat_message
        await self.client.send_decision(outcome.response)

    # 记录未被策略内部兜底处理的决策异常
    async def _handle_decision_error(self, request: DecisionRequest, error: Exception):
        print(f"❌ 决策请求 #{request.request_id} 执行失败: {error}")
        traceback.print_exception(type(error), error, error.__traceback__)

# 根据应用配置创建对局连接实例
def create_link_from_config(config: AppConfig) -> GalateaLink:
    return GalateaLink(
        config.server.host,
        config.server.port,
        config.server.password,
        config.agent.deck,
        protocol_version=config.server.protocol_version,
        game_id=config.server.game_id,
        connect_timeout=config.server.connect_timeout,
        trace_packets=config.server.trace_packets,
        agent_name=config.agent.name,
        prefer_second=config.agent.prefer_second,
        model_device=config.model.device,
        weights_path=config.model.weights_path,
        net_config=config.model.config or None,
        llm_config=config.llm,
        decision_config=config.decision,
    )


if __name__ == "__main__":
    link = create_link_from_config(load_app_config())
    asyncio.run(link.start())
