"""海龟汤主持人：会话级状态机 + 判定白名单 + 泄底兜底。

设计要点（都是踩过的坑换来的）：
- 主持人只需要四句话：是 / 不是 / 无关 / 是也不是。判定结果由模型给，但输出必须过白名单，
  超范围的一律降级成「无关」——否则模型迟早把汤底说出来。
- 汤底只在判定 prompt 里出现，任何对外文本都不带汤底原文；揭底是单独动作。
- 一局绑一个会话，别人插话不打断；同会话已有进行中的局时不再开新局。
- 题库本地加载，玩过的记进 puzzle_played，抽样时避开，玩完一轮才会重复。
"""
from __future__ import annotations

import json
import random
import re
from pathlib import Path
from typing import Any

# 判定白名单：模型只允许输出这四类，其他一律归一成「无关」
_VERDICTS = {
    "yes": "是",
    "no": "不是",
    "irrelevant": "无关",
    "partly": "是也不是",
    "是": "是",
    "不是": "不是",
    "无关": "无关",
    "是也不是": "是也不是",
}

_START_WORDS = ("来一局", "开一局", "开始游戏", "出个题", "出一道", "来把海龟汤", "玩海龟汤", "海龟汤来")
_HINT_WORDS = ("提示", "给点提示", "来点提示", "给提示", "再给个提示")
_REVEAL_WORDS = ("揭底", "公布答案", "直接说吧", "不玩了", "放弃", "公布汤底", "告诉我答案")
_GUESS_WORDS = ("汤底是", "答案是", "我知道了", "我猜是", "是不是因为", "真相是")

_JUDGE_SYSTEM = (
    "你是海龟汤主持人。玩家问一个关于汤面的问题，你只能依据汤底判断，"
    "并且只能回答四类之一：yes（是）、no（不是）、irrelevant（与本汤无关）、partly（部分对/是也不是）。"
    "严禁解释、严禁复述汤底、严禁给建议。只输出 JSON。"
)


def _norm(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", str(text or ""))


class PuzzleLibrary:
    """本地题库：加载、过滤、抽样。"""

    def __init__(self, path: str | Path, logger: Any = None) -> None:
        self.path = Path(path)
        self.logger = logger
        self.items: list[dict[str, Any]] = []
        self.by_id: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            if self.logger:
                self.logger.warning(f"[MaiLife] 海龟汤题库读取失败 {self.path}: {exc}")
            data = []
        if isinstance(data, dict):
            data = data.get("items") or []
        self.items = [item for item in data if isinstance(item, dict) and item.get("story") and item.get("answer")]
        self.by_id = {str(item.get("id") or ""): item for item in self.items}
        if self.logger:
            self.logger.info(f"[MaiLife] 海龟汤题库载入 {len(self.items)} 题（{self.path.name}）")

    def get(self, puzzle_id: str) -> dict[str, Any]:
        return self.by_id.get(str(puzzle_id or "")) or {}

    def pick(self, played: set[str], *, allow_ratings: list[str] | None = None,
             difficulty: str = "", rng: random.Random | None = None) -> dict[str, Any]:
        """抽一道没玩过的；按分级和难度过滤，池子空了逐级放宽。"""
        pool = [item for item in self.items if str(item.get("id")) not in played]
        if not pool:
            return {}
        ratings = [str(x) for x in (allow_ratings or [])]
        narrowed = [item for item in pool if not ratings or str(item.get("rating") or "light") in ratings]
        if difficulty:
            same = [item for item in narrowed if str(item.get("difficulty")) == difficulty]
            narrowed = same or narrowed
        if not narrowed:
            narrowed = pool
        rng = rng or random.Random()
        return rng.choice(narrowed)


class GameService:
    """一局海龟汤的完整生命周期。"""

    def __init__(self, store: Any, config: Any, llm: Any, logger: Any, *, assets_dir: str | Path,
                 bot_name: str = "麦麦") -> None:
        self.store = store
        self.config = config
        self.llm = llm
        self.logger = logger
        self.bot_name = bot_name
        self._lib: PuzzleLibrary | None = None
        self._assets_dir = Path(assets_dir)

    # ---------------- 基础 ----------------

    def _cfg(self) -> Any:
        return self.config.game

    def library(self) -> PuzzleLibrary:
        if self._lib is None:
            path = Path(getattr(self._cfg(), "library_file", "assets/puzzles_zh.json") or "assets/puzzles_zh.json")
            if not path.is_absolute():
                # assets_dir 可能是插件根目录（配 assets/xxx.json），也可能就是 assets 目录（配 xxx.json）
                candidate = self._assets_dir / path
                path = candidate if candidate.exists() else self._assets_dir / path.name
            self._lib = PuzzleLibrary(path, self.logger)
        return self._lib

    def update_config(self, config: Any) -> None:
        self.config = config

    def set_bot_name(self, name: str) -> None:
        clean = str(name or "").strip()
        if clean:
            self.bot_name = clean

    @staticmethod
    def _trigger(text: str) -> str:
        value = str(text or "")
        if any(word in value for word in _REVEAL_WORDS):
            return "reveal"
        if any(word in value for word in _HINT_WORDS):
            return "hint"
        if any(word in value for word in _START_WORDS) or ("海龟汤" in value and len(value) <= 16):
            return "start"
        return ""

    @staticmethod
    def looks_like_question(text: str) -> bool:
        value = str(text or "").strip()
        if len(value) < 2:
            return False
        if value.endswith(("?", "？")):
            return True
        return any(word in value for word in ("吗", "是不是", "是否", "有没有", "为什么", "为何", "怎么"))

    # ---------------- 开局 ----------------

    async def try_start(self, session_id: str, text: str, *, scope: str = "group",
                        group_id: str = "", user_id: str = "", now: Any = None) -> str:
        """识别开局意图并出汤面；不需要处理时返回空串。"""
        cfg = self._cfg()
        if not cfg.enabled:
            return ""
        if str(scope) == "group" and not cfg.group_enabled:
            return ""
        if str(scope) != "group" and not cfg.private_enabled:
            return ""
        if self._trigger(text) != "start":
            return ""
        if not re.search(r"海龟汤|来一局|开一局|出个题|出一道|开始游戏", str(text or "")):
            return ""
        stamp = getattr(now, "timestamp", None)
        ts = float(stamp() if callable(stamp) else (now or 0)) or __import__("time").time()
        existing = await self.store.active_game_round(session_id)
        if existing:
            puzzle = self.library().get(str(existing.get("puzzle_id") or ""))
            story = str(puzzle.get("story") or "")
            return f"这一局还没结束呢，汤面还是刚才那个：\n{story}\n想问就问，要提示就说「提示」。"
        played = await self.store.played_puzzle_ids()
        puzzle = self.library().pick(
            played,
            allow_ratings=list(getattr(cfg, "allow_ratings", []) or []),
            difficulty=str(getattr(cfg, "difficulty", "") or ""),
        )
        if not puzzle:
            return "题都玩完了，题库见底了。等我想想再出新的。"
        ok = await self.store.start_game_round(
            session_id, str(puzzle.get("id") or ""), scope=scope,
            group_id=group_id, user_id=user_id, now=ts,
        )
        if not ok:
            return ""
        await self.store.mark_puzzle_played(str(puzzle.get("id") or ""), session_id, ts)
        await self.store.add_game_turn(session_id, "start", str(puzzle.get("story") or ""),
                                       user_id=user_id, now=ts)
        hints = len(puzzle.get("hints") or [])
        tail = f"想问就一句一句问，我只会答「是 / 不是 / 无关」。卡住了说「提示」（这道一共 {hints} 条）。" if hints \
            else "想问就一句一句问，我只会答「是 / 不是 / 无关」。"
        title = str(puzzle.get("title") or "")
        head = f"来一局。汤面：\n" if not title else f"【{title}】\n汤面：\n"
        return f"{head}{puzzle.get('story')}\n\n{tail}"

    # ---------------- 判定 ----------------

    async def _judge(self, puzzle: dict[str, Any], question: str, turns: list[dict[str, Any]]) -> str:
        """拿到汤底判断四选一；输出过白名单，异常一律「无关」。"""
        if not self.llm.task_available("puzzle_judge"):
            return "无关"
        history = "\n".join(
            f"Q：{row.get('text')} → {_VERDICTS.get(str(row.get('verdict') or ''), '无关')}"
            for row in turns[-8:] if str(row.get("kind")) == "question"
        )
        payload = {
            "汤面": puzzle.get("story"),
            "汤底": puzzle.get("answer"),
            "已问过": history or "无",
            "玩家这一问": question,
        }
        try:
            result = await self.llm.generate_json(
                "依据汤底判断玩家这一问的答案。只能返回 "
                '{"verdict":"yes|no|irrelevant|partly"}，不解释、不复述汤底。\n'
                + json.dumps(payload, ensure_ascii=False),
                _JUDGE_SYSTEM,
                {"verdict": "irrelevant"},
                task_kind="puzzle_judge",
                request_type="turtle_soup_judge",
            )
        except Exception as exc:
            self.logger.warning(f"[MaiLife] 海龟汤判定失败: {exc}")
            return "无关"
        raw = str((result or {}).get("verdict") or "").strip().lower()
        return _VERDICTS.get(raw, "无关")

    def _contains_answer(self, text: str, answer: str) -> bool:
        """出口兜底：文本里出现汤底的长片段就认为泄底。"""
        needle = _norm(answer)
        hay = _norm(text)
        if len(needle) < 12:
            return False
        for size in (24, 16, 12):
            if len(needle) >= size:
                for start in range(0, len(needle) - size + 1, size):
                    if needle[start:start + size] in hay:
                        return True
        return False

    async def handle(self, session_id: str, text: str, *, user_id: str = "", now: Any = None) -> str:
        """局内消息处理：判定 / 提示 / 揭底 / 猜答案。不需要回应时返回空串。"""
        cfg = self._cfg()
        if not cfg.enabled:
            return ""
        round_ = await self.store.active_game_round(session_id)
        if not round_:
            return ""
        stamp = getattr(now, "timestamp", None)
        ts = float(stamp() if callable(stamp) else (now or 0)) or __import__("time").time()
        # 超时自动收摊
        timeout = max(5, int(getattr(cfg, "round_timeout_minutes", 90) or 90)) * 60
        if ts - float(round_.get("updated_at") or 0) > timeout:
            await self.store.touch_game_round(session_id, phase="timeout", now=ts)
            return "这局放太久了，先收摊。想玩再喊我一声。"
        puzzle = self.library().get(str(round_.get("puzzle_id") or ""))
        if not puzzle:
            await self.store.touch_game_round(session_id, phase="broken", now=ts)
            return "题目找不到了，这局作废。"
        action = self._trigger(text)

        if action == "reveal":
            await self.store.touch_game_round(session_id, phase="revealed", now=ts)
            await self.store.add_game_turn(session_id, "reveal", str(text), user_id=user_id, now=ts)
            return f"揭底：\n{puzzle.get('answer')}"

        if action == "hint":
            hints = list(puzzle.get("hints") or [])
            used = int(round_.get("hints_used") or 0)
            if not hints or used >= len(hints):
                return "这道没别的提示了，或者本来就没写提示。要直接看汤底就说「揭底」。"
            await self.store.touch_game_round(session_id, hints_used=used + 1, now=ts)
            await self.store.add_game_turn(session_id, "hint", str(hints[used]), user_id=user_id, now=ts)
            return f"提示 {used + 1}：{hints[used]}"

        max_q = max(1, int(getattr(cfg, "max_questions", 60) or 60))
        if int(round_.get("question_count") or 0) >= max_q:
            await self.store.touch_game_round(session_id, phase="expired", now=ts)
            return f"问了 {max_q} 个问题了，这局到量。揭底：\n{puzzle.get('answer')}"

        # 猜答案：单独判定，命中就收官
        if any(word in str(text) for word in _GUESS_WORDS):
            await self.store.add_game_turn(session_id, "guess", str(text), user_id=user_id, now=ts)
            verdict = await self._judge(puzzle, f"玩家的猜测是：{text}（判断他是否已猜出汤底核心）", [])
            if verdict == "是":
                await self.store.touch_game_round(session_id, phase="solved", now=ts)
                return f"就是它了，你猜对了。汤底：\n{puzzle.get('answer')}"
            return "还差一点，接着问。"

        if not self.looks_like_question(text):
            return ""

        verdict = await self._judge(puzzle, str(text), await self.store.list_game_turns(session_id, 20))
        await self.store.add_game_turn(session_id, "question", str(text), user_id=user_id,
                                       verdict=verdict, now=ts)
        await self.store.touch_game_round(session_id, question_delta=1, now=ts)
        if self._contains_answer(str(verdict), str(puzzle.get("answer") or "")):
            return "无关"
        return verdict

    # ---------------- 运维 ----------------

    async def cleanup(self, now: Any = None) -> None:
        """把超时的局标掉，避免残局卡住会话。"""
        stamp = getattr(now, "timestamp", None)
        ts = float(stamp() if callable(stamp) else (now or 0)) or __import__("time").time()
        timeout = max(5, int(getattr(self._cfg(), "round_timeout_minutes", 90) or 90)) * 60
        for row in await self.store.stale_game_rounds(ts - timeout):
            await self.store.touch_game_round(str(row.get("session_id") or ""), phase="timeout", now=ts)
