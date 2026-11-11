"""被动日程：只记录聊天里被派给的活，不描述麦麦自己在过什么生活。

设计取舍（来自实际使用反馈）：
- 生成「我今天几点在做什么」的日程没有意义，问起来要么空洞（"上午做事"），
  要么把用户聊过的事偷过来当自己的经历。
- 真正有用的是「对方让我做的事」：真任务（查资料、写东西）和角色扮演任务
  （"你去环游世界"）都算，可以当话题引子，也能顺着聊进度。
- 抽取只用白名单私聊；抽不到就留空，绝不用模型记忆补。
"""
from __future__ import annotations

import json
import time
from typing import Any

# 派活信号：命中才值得花一次模型调用。宁宽勿窄，抽错一条比漏掉一条代价小。
_SIGNAL_WORDS = (
    "你去", "你给我", "你帮", "帮我", "替我", "记得", "别忘", "回头", "明天", "等你",
    "带我去", "假装", "扮演", "让我", "给我写", "写个", "写一", "查一下", "查查", "研究",
    "准备", "做个", "搞个", "整一个", "汇报", "交作业", "试试", "安排", "负责", "商量",
    "说好", "答应", "约定", "任务", "然后告诉", "发我", "给我看",
)

_EXTRACT_SYSTEM = "你只抽取「对方明确让我去做的事」，不推测、不补充、不评价。"


class CommitmentService:
    """被指派事项的抽取、注入与话题供给。"""

    def __init__(self, store: Any, config: Any, llm: Any, logger: Any, bot_name: str = "麦麦") -> None:
        self.store = store
        self.config = config
        self.llm = llm
        self.logger = logger
        self.bot_name = bot_name
        # 抽取额度只在进程内计数：重启重新算，宁可多抽一次也不建冗余表。
        self._extract_day = ""
        self._extract_count = 0

    def update_config(self, config: Any) -> None:
        self.config = config

    def set_bot_name(self, name: str) -> None:
        clean = str(name or "").strip()
        if clean:
            self.bot_name = clean

    # ---------------- 抽取 ----------------

    def _cfg(self) -> Any:
        return self.config.commitment

    def _whitelisted(self, user_id: str) -> bool:
        cfg = self._cfg()
        allowed = {str(item).strip() for item in (cfg.owner_qq_list or []) if str(item).strip()}
        return bool(allowed) and str(user_id or "").strip() in allowed

    @staticmethod
    def _has_signal(text: str) -> bool:
        value = str(text or "")
        return len(value) >= 3 and any(word in value for word in _SIGNAL_WORDS)

    async def observe_message(self, user_id: str, text: str, now: Any,
                              source_message_id: str = "", *, is_private: bool = True) -> None:
        """只处理白名单私聊，命中派活信号才抽一次，抽到的进库并生成主动契机。"""
        cfg = self._cfg()
        if not cfg.enabled or not is_private:
            return
        if not self._whitelisted(user_id) or not self._has_signal(text):
            return
        day = now.strftime("%Y-%m-%d")
        if self._extract_day != day:
            self._extract_day = day
            self._extract_count = 0
        if self._extract_count >= int(cfg.extract_daily_max):
            return
        if not self.llm.task_available("commitment"):
            return
        self._extract_count += 1
        payload = {"message": str(text or "")[:800], "bot_name": self.bot_name}
        try:
            result = await self.llm.generate_json(
                "从下面这条消息里，抽取对方明确让我（" + self.bot_name + "）去做或答应要做的事。"
                "只抽真的被指派/约定的，不要抽对方自己的计划、不要抽反问、不要补细节。"
                "角色扮演类的指派也要抽（例如对方叫我去环游世界、去当某个角色），标 roleplay=1。\n"
                + json.dumps(payload, ensure_ascii=False)
                + '\n只返回JSON：{"commitments":[{"content":"","roleplay":0}]}。'
                "content 用一句中文写清要做什么（保留对方的说法），没有就返回空数组。",
                _EXTRACT_SYSTEM,
                {"commitments": []},
                task_kind="commitment",
                request_type="commitment_extract",
            )
        except Exception as exc:  # 抽取失败不影响正常回复
            self.logger.warning(f"[MaiLife] 被动日程抽取失败: {exc}")
            return
        items = result.get("commitments") if isinstance(result, dict) else None
        if not isinstance(items, list):
            return
        saved = 0
        for item in items[:3]:
            if not isinstance(item, dict):
                continue
            content = " ".join(str(item.get("content") or "").split())[:120]
            if len(content) < 3:
                continue
            commitment_id = await self.store.add_commitment({
                "user_id": str(user_id),
                "scope": "private",
                "day": day,
                "content": content,
                "roleplay": 1 if item.get("roleplay") else 0,
                "status": "pending",
                "source_msg": str(source_message_id or "")[:120],
                "created_at": now.timestamp(),
            })
            if not commitment_id:
                continue
            saved += 1
            await self._raise_opportunity(user_id, commitment_id, content, now)
        if saved:
            self.logger.info(f"[MaiLife] 被动日程 +{saved}：{items[0].get('content') if items else ''}")

    async def _raise_opportunity(self, user_id: str, commitment_id: str, content: str, now: Any) -> None:
        """给主动搭话留一个由头：素材是「你让我做的事」，不是虚构生活。"""
        if not bool(getattr(self._cfg(), "proactive_enabled", True)):
            return
        try:
            await self.store.add_opportunity({
                "id": f"commitment-{commitment_id}",
                "framework_id": f"commitment:{commitment_id}",
                "topic": content[:160],
                "motive": "对方交办的事，可以问进度、报进展或接着聊",
                "weight": 0.72,
                "privacy": "owner_only",
                "target_user_id": str(user_id),
                "expires_at": now.timestamp() + max(1, int(self._cfg().active_days)) * 86400,
            })
        except Exception as exc:
            self.logger.warning(f"[MaiLife] 被动日程契机写入失败: {exc}")

    # ---------------- 使用 ----------------

    async def _pending(self, now: Any, limit: int) -> list[dict[str, Any]]:
        cfg = self._cfg()
        if not cfg.enabled:
            return []
        cap = int(limit or cfg.context_item_limit)
        if cap <= 0:
            return []
        rows = await self.store.list_commitments(status="pending", limit=max(cap * 2, cap))
        horizon = float(getattr(now, "timestamp", None) and now.timestamp() or time.time()) - max(1, int(cfg.active_days)) * 86400
        fresh = [row for row in rows if float(row.get("created_at") or 0) >= horizon]
        return fresh[:cap]

    async def context_for(self, now: Any, *, limit: int = 0) -> dict[str, Any]:
        """注入对话用的进行中事项。"""
        rows = await self._pending(now, limit)
        items = [{
            "content": str(row.get("content") or ""),
            "roleplay": bool(row.get("roleplay")),
            "day": str(row.get("day") or ""),
        } for row in rows]
        return {"items": items}

    async def text_for_prompt(self, now: Any, *, limit: int = 0) -> str:
        """把进行中事项写成一段可直接进 prompt 的中文。"""
        rows = await self._pending(now, limit)
        if not rows:
            return ""
        parts = []
        for row in rows:
            content = str(row.get("content") or "").strip()
            if not content:
                continue
            tag = "（角色扮演）" if row.get("roleplay") else ""
            parts.append(f"{content}{tag}")
        return "；".join(parts)

    async def proactive_topics(self, now: Any, limit: int = 3) -> list[dict[str, Any]]:
        """给主动搭话的候选素材：未完成的交办事项，按新鲜度排序。"""
        rows = await self._pending(now, limit)
        return [{
            "topic": str(row.get("content") or ""),
            "roleplay": bool(row.get("roleplay")),
            "created_at": float(row.get("created_at") or 0),
        } for row in rows]

    async def mark_done(self, commitment_id: str) -> bool:
        return await self.store.update_commitment_status(commitment_id, "done")

    async def mark_done_by_content(self, content: str) -> bool:
        """按内容模糊匹配把最近一条未完成事项标完成（用户说"做完了"时用）。"""
        needle = " ".join(str(content or "").split())
        if not needle:
            return False
        for row in await self.store.list_commitments(status="pending", limit=20):
            if needle in str(row.get("content") or "") or str(row.get("content") or "") in needle:
                return await self.mark_done(str(row.get("id") or ""))
        return False
