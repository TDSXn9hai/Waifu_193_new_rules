import asyncio
import re
import time
import typing

import requests

from pkg.core import app

from plugins.Waifu.cells.generator import Generator
from plugins.Waifu.organs.memories import Memory


class Searching:
    """根据用户信息自主构造搜索决策，并把搜索结果引入当前回复上下文。"""

    ap: app.Application

    def __init__(self, ap: app.Application, launcher_type: str):
        self.ap = ap
        self._generator = Generator(ap)
        self._launcher_type = launcher_type
        self._enabled = False
        self._mode = "off"
        self._provider = "duckduckgo"
        self._api_url = ""
        self._api_key = ""
        self._max_results = 5
        self._max_queries = 2
        self._timeout = 10
        self._language = "zh-CN"
        self._cache_ttl = 600
        self._trigger_regexp: typing.List[re.Pattern] = []
        self._cache: typing.Dict[str, typing.Tuple[float, typing.List[dict]]] = {}

    @property
    def enabled(self) -> bool:
        return self._enabled

    def load_config(self, waifu_config: dict):
        self._enabled = bool(waifu_config.get("search_enabled", False))
        mode = str(waifu_config.get("search_mode", "auto") or "auto").lower()
        self._mode = mode if mode in ("auto", "always", "off") else "auto"
        if not self._enabled:
            self._mode = "off"
        self._provider = str(waifu_config.get("search_provider", "duckduckgo") or "duckduckgo").lower()
        self._api_url = str(waifu_config.get("search_api_url", "") or "")
        self._api_key = str(waifu_config.get("search_api_key", "") or "")
        self._max_results = max(1, min(10, self._to_int(waifu_config.get("search_max_results"), 5)))
        self._max_queries = max(1, min(5, self._to_int(waifu_config.get("search_max_queries"), 2)))
        self._timeout = max(1, min(60, self._to_int(waifu_config.get("search_timeout"), 10)))
        self._language = str(waifu_config.get("search_language", "zh-CN") or "zh-CN")
        self._cache_ttl = max(0, self._to_int(waifu_config.get("search_cache_ttl"), 600))
        self._trigger_regexp = self._compile_patterns(waifu_config.get("search_trigger_regexp", []))

    def _compile_patterns(self, patterns) -> typing.List[re.Pattern]:
        if isinstance(patterns, str):
            patterns = [patterns]
        elif not isinstance(patterns, (list, tuple)):
            self.ap.logger.error("search_trigger_regexp 配置格式错误，应为字符串列表。")
            return []

        compiled = []
        for pattern in patterns:
            if not isinstance(pattern, str) or not pattern.strip():
                continue
            try:
                compiled.append(re.compile(pattern))
            except re.error as e:
                self.ap.logger.error(f"search_trigger_regexp 配置无效，已跳过：{pattern!r}（{e}）")
        return compiled

    def _get_last_user_message(self, memory: Memory) -> str:
        for message in reversed(memory.short_term_memory):
            if message.role in ("assistant", "narrator", "system"):
                continue
            return self._generator.get_content_str_without_timestamp(message)
        return ""

    def _conversations_str(self, memory: Memory, max_conversations: int = 6) -> str:
        conversations = memory.short_term_memory[-max_conversations:]
        if self._launcher_type == "group":
            return memory.get_conversations_str_for_group(conversations)
        _, conversations_str = memory.get_conversations_str_for_person(conversations)
        return conversations_str

    async def search_for_context(self, memory: Memory) -> str:
        """返回可直接追加到 system prompt 的搜索资料；不需要搜索时返回空字符串。"""
        if not self._enabled or self._mode == "off":
            return ""
        latest_message = self._get_last_user_message(memory)
        if not latest_message.strip():
            return ""

        queries = await self._build_queries(memory, latest_message)
        if not queries:
            return ""

        results: typing.List[dict] = []
        seen = set()
        for query in queries[: self._max_queries]:
            for item in await self._search(query):
                key = item.get("url") or item.get("title")
                if key in seen:
                    continue
                seen.add(key)
                results.append(item)
                if len(results) >= self._max_results:
                    break
            if len(results) >= self._max_results:
                break

        if not results:
            return ""
        self.ap.logger.info(f"联网搜索完成，共 {len(results)} 条结果：{queries}")
        return self._format_context(results)

    async def _build_queries(self, memory: Memory, latest_message: str) -> typing.List[str]:
        if any(pattern.search(latest_message) for pattern in self._trigger_regexp):
            return [latest_message[:200]]
        if self._mode == "always":
            return [latest_message[:200]]
        return await self._decide_queries(memory, latest_message)

    async def _decide_queries(self, memory: Memory, latest_message: str) -> typing.List[str]:
        conversations_str = self._conversations_str(memory)
        user_prompt = ""
        if conversations_str:
            user_prompt += f"这是最近的对话记录：“{conversations_str}”。"
        user_prompt += f"用户最后说：“{latest_message}”。"
        user_prompt += "判断回答这条消息是否需要联网搜索实时信息（例如新闻、天气、价格、赛事、产品、人物近况、政策、时效性事实等）。"
        user_prompt += "闲聊、情感交流、角色扮演、创作、纯常识问题不需要搜索。"
        user_prompt += "若需要搜索，请给出1到2条简洁的搜索关键词；若不需要，不要给出关键词。"
        user_prompt += "严格按照以下格式输出，不要输出其他内容：\n[SEARCH]yes 或 no\n[QUERY]搜索关键词（每行一条）"

        self._generator.set_speakers([])
        response = await self._generator.return_string(user_prompt)
        return self._parse_decision(response, latest_message)

    def _parse_decision(self, response: str, latest_message: str) -> typing.List[str]:
        response = re.sub(r"【\s*(SEARCH|QUERY)\s*】", r"[\1]", response, flags=re.I)
        search_match = re.search(r"\[SEARCH\]\s*(yes|no|true|false|是|否)", response, re.I)
        if search_match and search_match.group(1).lower() in ("no", "false", "否"):
            return []

        queries = [query.strip() for query in re.findall(r"\[QUERY\]\s*(.+)", response) if query.strip()]
        if queries:
            return queries[: self._max_queries]
        if search_match:
            return [latest_message[:80]]
        return []

    async def _search(self, query: str) -> typing.List[dict]:
        cache_key = f"{self._provider}|{self._language}|{query}"
        now = time.time()
        cached = self._cache.get(cache_key)
        if cached and now - cached[0] <= self._cache_ttl:
            return cached[1]

        try:
            if self._provider == "searxng":
                results = await asyncio.to_thread(self._search_searxng, query)
            elif self._provider == "serper":
                results = await asyncio.to_thread(self._search_serper, query)
            elif self._provider == "tavily":
                results = await asyncio.to_thread(self._search_tavily, query)
            else:
                results = await asyncio.to_thread(self._search_duckduckgo, query)
        except Exception as e:
            self.ap.logger.error(f"联网搜索失败（{self._provider}）：{e}")
            results = []

        if self._cache_ttl > 0:
            self._cache[cache_key] = (now, results)
            self._prune_cache(now)
        return results

    def _prune_cache(self, now: float):
        expired = [key for key, (timestamp, _) in self._cache.items() if now - timestamp > self._cache_ttl]
        for key in expired:
            self._cache.pop(key, None)

    def _search_duckduckgo(self, query: str) -> typing.List[dict]:
        url = self._api_url or "https://api.duckduckgo.com/"
        response = requests.get(
            url,
            params={"q": query, "format": "json", "no_html": 1, "skip_disambig": 1},
            timeout=self._timeout,
        )
        response.raise_for_status()
        data = response.json()

        results = []
        if data.get("AbstractText"):
            results.append({
                "title": data.get("Heading", "") or query,
                "snippet": data.get("AbstractText", ""),
                "url": data.get("AbstractURL", ""),
            })

        def walk(topics):
            for topic in topics:
                if not isinstance(topic, dict):
                    continue
                if topic.get("Text"):
                    yield topic
                elif isinstance(topic.get("Topics"), list):
                    yield from walk(topic["Topics"])

        for topic in walk(data.get("RelatedTopics", [])):
            results.append({
                "title": topic.get("Text", "")[:80],
                "snippet": topic.get("Text", ""),
                "url": topic.get("FirstURL", ""),
            })
            if len(results) >= self._max_results:
                break
        return results

    def _search_searxng(self, query: str) -> typing.List[dict]:
        if not self._api_url:
            self.ap.logger.error("使用 searxng 时需要配置 search_api_url。")
            return []
        url = f"{self._api_url.rstrip('/')}/search"
        response = requests.get(
            url,
            params={"q": query, "format": "json", "language": self._language},
            timeout=self._timeout,
        )
        response.raise_for_status()
        data = response.json()
        return [
            {
                "title": item.get("title", ""),
                "snippet": item.get("content", ""),
                "url": item.get("url", ""),
            }
            for item in data.get("results", [])[: self._max_results]
        ]

    def _search_serper(self, query: str) -> typing.List[dict]:
        if not self._api_key:
            self.ap.logger.error("使用 serper 时需要配置 search_api_key。")
            return []
        url = self._api_url or "https://google.serper.dev/search"
        response = requests.post(
            url,
            json={"q": query, "num": self._max_results},
            headers={"X-API-KEY": self._api_key, "Content-Type": "application/json"},
            timeout=self._timeout,
        )
        response.raise_for_status()
        data = response.json()
        return [
            {
                "title": item.get("title", ""),
                "snippet": item.get("snippet", ""),
                "url": item.get("link", ""),
            }
            for item in data.get("organic", [])[: self._max_results]
        ]

    def _search_tavily(self, query: str) -> typing.List[dict]:
        if not self._api_key:
            self.ap.logger.error("使用 tavily 时需要配置 search_api_key。")
            return []
        url = self._api_url or "https://api.tavily.com/search"
        response = requests.post(
            url,
            json={
                "api_key": self._api_key,
                "query": query,
                "max_results": self._max_results,
                "search_depth": "basic",
            },
            timeout=self._timeout,
        )
        response.raise_for_status()
        data = response.json()
        return [
            {
                "title": item.get("title", ""),
                "snippet": item.get("content", ""),
                "url": item.get("url", ""),
            }
            for item in data.get("results", [])[: self._max_results]
        ]

    def _format_context(self, results: typing.List[dict]) -> str:
        lines = ["【联网搜索资料】以下内容来自实时搜索，仅作为事实参考。回复时保持角色口吻，不要提及搜索过程；除非用户询问来源，否则不要输出链接。"]
        total_length = len(lines[0])
        for index, item in enumerate(results, 1):
            title = self._clean_text(item.get("title", ""))[:120]
            snippet = self._clean_text(item.get("snippet", ""))[:400]
            url = self._clean_text(item.get("url", ""))[:300]
            block = f"[{index}] {title}\n摘要：{snippet}"
            if url:
                block += f"\n链接：{url}"
            if total_length + len(block) > 2500:
                break
            lines.append(block)
            total_length += len(block)
        return "\n".join(lines)

    def _clean_text(self, text) -> str:
        return re.sub(r"\s+", " ", str(text or "")).strip()

    def _to_int(self, value, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def set_jail_break(self, type: str, user_name: str):
        self._generator.set_jail_break(type, user_name)
