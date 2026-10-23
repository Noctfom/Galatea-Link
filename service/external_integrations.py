# 外部智能体连接登记模块，维护 dsh 等接入端的有界心跳租约

import time
from typing import Any, Mapping


class ExternalIntegrationRegistry:
    # 初始化指定接入端的连接登记与事件通道
    def __init__(self, provider: str, events, lease_seconds: int = 60):
        self.provider = provider
        self.events = events
        self.lease_seconds = lease_seconds
        self.clients = {}

    # 校验心跳元数据并更新连接租约
    def record(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        client_id = str(payload.get("client_id", "")).strip()
        instance = str(payload.get("instance_name", self.provider)).strip()
        version = str(payload.get("plugin_version", "unknown")).strip()
        action = payload.get("action", "heartbeat")
        for value, limit in ((client_id, 128), (instance, 100), (version, 64)):
            if not value or len(value) > limit or "\x00" in value:
                raise ValueError("外部智能体心跳标识无效")
        if not isinstance(action, str) or action not in {"connect", "heartbeat", "disconnect"}:
            raise ValueError("外部智能体心跳动作无效")
        previous = self.clients.get(client_id)
        record = {
            "client_id": client_id, "instance_name": instance,
            "plugin_version": version, "last_seen": time.time(),
            "connected": action != "disconnect",
        }
        for field, default in (("remote_agent_enabled", True), ("agent_game_chat_enabled", True), ("allow_agent_deck_edit", False)):
            value = payload.get(field, default)
            if not isinstance(value, bool):
                raise ValueError(f"{field} 必须是布尔值")
            record[field] = value
        self.clients[client_id] = record
        if len(self.clients) > 64:
            oldest = min(self.clients, key=lambda key: self.clients[key]["last_seen"])
            del self.clients[oldest]
        if action == "disconnect" or not previous or not previous["connected"]:
            suffix = "disconnected" if action == "disconnect" else "connected"
            self.events.publish(f"integration.{self.provider}.{suffix}", dict(record))
        return {"integration": dict(record), "lease_seconds": self.lease_seconds}

    # 返回脱敏且按最近连接排序的租约状态
    def status(self) -> dict[str, Any]:
        now = time.time()
        clients = []
        for record in self.clients.values():
            age = max(0, now - record["last_seen"])
            clients.append({**record, "age_seconds": round(age, 1), "connected": record["connected"] and age <= self.lease_seconds})
        clients.sort(key=lambda item: item["last_seen"], reverse=True)
        return {"connected": any(item["connected"] for item in clients), "active_count": sum(bool(item["connected"]) for item in clients), "clients": clients[:8]}
