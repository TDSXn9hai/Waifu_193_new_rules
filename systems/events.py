import json
import os
import random
import re
import time
import typing

from pkg.core import app

from plugins.Waifu.cells.cards import Cards
from plugins.Waifu.cells.generator import Generator
from plugins.Waifu.organs.memories import Memory


class Events:
    """根据状态与行为触发事件，将事件引入上下文，并支持主动发起消息。"""

    ap: app.Application

    def __init__(self, ap: app.Application, launcher_id: str, launcher_type: str):
        self.ap = ap
        self._generator = Generator(ap)
        self._launcher_id = launcher_id
        self._launcher_type = launcher_type
        self._enabled = False
        self._idle_interval = 0
        self._idle_rate = 0.0
        self._idle_min_messages = 2
        self._max_words = 60
        self._rules: typing.List[dict] = []
        self._state_file = f"data/plugins/Waifu/data/events_{launcher_id}.json"
        self._state = {"rules": {}, "once": [], "last_idle_event": 0}
        self._load_state()

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def idle_interval(self) -> int:
        return self._idle_interval

    def load_config(self, waifu_config: dict):
        self._enabled = bool(waifu_config.get("events_enabled", False))
        self._idle_interval = max(0, self._to_int(waifu_config.get("event_idle_interval"), 0))
        self._idle_rate = min(1.0, max(0.0, self._to_float(waifu_config.get("event_idle_rate"), 0.5)))
        self._idle_min_messages = max(1, self._to_int(waifu_config.get("event_idle_min_messages"), 2))
        self._max_words = max(10, self._to_int(waifu_config.get("event_max_words"), 60))
        self._rules = self._normalize_rules(waifu_config.get("event_rules", []))

    def _normalize_rules(self, raw_rules) -> typing.List[dict]:
        if not raw_rules:
            return []
        if isinstance(raw_rules, dict):
            raw_rules = [raw_rules]
        if not isinstance(raw_rules, list):
            self.ap.logger.error("event_rules 配置格式错误，应为列表。")
            return []

        rules = []
        for index, raw in enumerate(raw_rules):
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or f"event_{index + 1}")
            regexp = None
            if raw.get("regexp"):
                try:
                    regexp = re.compile(str(raw["regexp"]))
                except re.error as e:
                    self.ap.logger.error(f"事件规则 {name} 的正则表达式无效，已跳过：{raw['regexp']!r}（{e}）")
                    continue

            rules.append({
                "name": name,
                "regexp": regexp,
                "min_value": self._to_int(raw.get("min_value"), 0),
                "max_value": self._to_int(raw.get("max_value"), 10000),
                "cooldown": max(0, self._to_int(raw.get("cooldown"), 3600)),
                "probability": min(1.0, max(0.0, self._to_float(raw.get("probability"), 1.0))),
                "once": bool(raw.get("once", False)),
                "prompt": str(raw.get("prompt", "") or ""),
            })
        return rules

    def _load_state(self):
        try:
            with open(self._state_file, "r", encoding="utf-8") as file:
                data = json.load(file)
            if isinstance(data, dict):
                if isinstance(data.get("rules"), dict):
                    self._state["rules"] = data["rules"]
                if isinstance(data.get("once"), list):
                    self._state["once"] = data["once"]
                self._state["last_idle_event"] = self._to_float(data.get("last_idle_event"), 0)
        except FileNotFoundError:
            pass
        except Exception as e:
            self.ap.logger.warning(f"读取事件状态文件失败：{e}")

    def _save_state(self):
        try:
            directory = os.path.dirname(self._state_file)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(self._state_file, "w", encoding="utf-8") as file:
                json.dump(self._state, file, ensure_ascii=False, indent=2)
        except Exception as e:
            self.ap.logger.error(f"保存事件状态文件失败：{e}")

    def _get_last_user_message(self, memory: Memory) -> str:
        for message in reversed(memory.short_term_memory):
            if message.role in ("assistant", "narrator", "system"):
                continue
            return self._generator.get_content_str_without_timestamp(message)
        return ""

    def _rule_matches(self, rule: dict, message_text: str, value: int) -> bool:
        if rule["regexp"] and not rule["regexp"].search(message_text):
            return False
        if not (rule["min_value"] <= value <= rule["max_value"]):
            return False
        if rule["once"] and rule["name"] in self._state["once"]:
            return False
        last_fired = self._to_float(self._state["rules"].get(rule["name"]), 0)
        if last_fired and time.time() - last_fired < rule["cooldown"]:
            return False
        return True

    def _mark_rule_fired(self, rule: dict):
        self._state["rules"][rule["name"]] = time.time()
        if rule["once"] and rule["name"] not in self._state["once"]:
            self._state["once"].append(rule["name"])
        self._save_state()

    def _conversations_str(self, memory: Memory, max_conversations: int = 8) -> str:
        conversations = memory.short_term_memory[-max_conversations:]
        if self._launcher_type == "group":
            return memory.get_conversations_str_for_group(conversations)
        _, conversations_str = memory.get_conversations_str_for_person(conversations)
        return conversations_str

    async def check_message_event(self, memory: Memory, card: Cards, value: int, manner: str) -> str:
        """检查消息触发规则；命中时返回事件描述（旁白事实），否则返回空字符串。"""
        if not self._enabled or not self._rules:
            return ""
        message_text = self._get_last_user_message(memory)
        if not message_text:
            return ""

        for rule in self._rules:
            if not self._rule_matches(rule, message_text, value):
                continue
            if random.random() >= rule["probability"]:
                continue
            try:
                event = await self._generate_event(memory, card, value, manner, rule, message_text)
            except Exception as e:
                self.ap.logger.error(f"生成事件失败（{rule['name']}）：{e}")
                continue
            if event:
                self._mark_rule_fired(rule)
                self.ap.logger.info(f"触发事件：{rule['name']}")
                return event
        return ""

    async def _generate_event(
        self,
        memory: Memory,
        card: Cards,
        value: int,
        manner: str,
        rule: dict,
        message_text: str,
    ) -> str:
        profile = memory.to_custom_names(card.get_profile() + card.get_background() + card.get_manner())
        conversations_str = self._conversations_str(memory)
        time_str = self._generator.get_chinese_current_time()

        user_prompt = ""
        if profile:
            user_prompt += f"这是{memory.assistant_name}的角色设定“{profile}”。"
        if conversations_str:
            user_prompt += f"这是最近的记录：“{conversations_str}”。"
        user_prompt += f"当前时间是{time_str}，当前状态值为{value}。"
        if manner:
            user_prompt += f"当前行为准则：“{manner}”。"
        user_prompt += f"用户最后说：“{message_text}”。"
        if rule["prompt"]:
            user_prompt += f"事件要求：{rule['prompt']}。"
        else:
            user_prompt += "请围绕当前状态、用户发言和角色设定，设计一个此刻发生的合理生活事件或小插曲。"
        user_prompt += f"事件必须是客观发生的旁白事实，只提供事件描述，不要包含任何角色的发言，不超过{self._max_words}字。"

        self._generator.set_speakers([memory.assistant_name])
        return (await self._generator.return_string(user_prompt)).strip()

    def should_fire_idle(self, memory: Memory) -> bool:
        if not self._enabled or self._idle_interval <= 0:
            return False
        if len(memory.short_term_memory) < self._idle_min_messages:
            return False
        return random.random() < self._idle_rate

    async def generate_idle_event(self, memory: Memory, card: Cards, value: int, manner: str) -> typing.Tuple[str, str]:
        """生成空闲主动事件，返回（事件描述，主动消息）。"""
        profile = memory.to_custom_names(card.get_profile() + card.get_background() + card.get_manner())
        conversations_str = self._conversations_str(memory)
        time_str = self._generator.get_chinese_current_time()

        user_prompt = ""
        if profile:
            user_prompt += f"这是{memory.assistant_name}的角色设定“{profile}”。"
        if conversations_str:
            user_prompt += f"这是最近的记录：“{conversations_str}”。"
        user_prompt += f"当前时间是{time_str}，当前状态值为{value}。"
        if manner:
            user_prompt += f"当前行为准则：“{manner}”。"
        user_prompt += "用户已经有一段时间没有发言了。请设计一个此刻发生在角色身边的小事件，并以角色的口吻主动发一条消息给用户，自然地引出这个事件。"
        user_prompt += "事件应贴近角色设定和当前状态，不要提及等待、计时或系统。"
        user_prompt += "严格按照以下格式输出，不要输出其他内容：\n[EVENT]事件描述（旁白事实，不含角色发言）\n[MESSAGE]主动发给用户的消息"

        self._generator.set_speakers([memory.assistant_name])
        response = await self._generator.return_string(user_prompt)
        event, message = self._parse_idle_response(response)
        if event or message:
            self._state["last_idle_event"] = time.time()
            self._save_state()
        return event, message

    def _parse_idle_response(self, response: str) -> typing.Tuple[str, str]:
        response = re.sub(r"【\s*(EVENT|MESSAGE)\s*】", r"[\1]", response, flags=re.I)
        event = ""
        message = ""
        event_match = re.search(r"\[EVENT\]\s*(.*?)(?=\[MESSAGE\]|$)", response, re.S)
        if event_match:
            event = event_match.group(1).strip()
        message_match = re.search(r"\[MESSAGE\]\s*(.*)$", response, re.S)
        if message_match:
            message = message_match.group(1).strip()
        if not event and not message:
            message = response.strip()
        return event, message

    def _to_int(self, value, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _to_float(self, value, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def set_jail_break(self, type: str, user_name: str):
        self._generator.set_jail_break(type, user_name)
