"""网页搜索与抓取。

搜索源可插拔：searxng（自建/公共实例 JSON API，推荐）/ duckduckgo（免密钥 HTML）。
抓取：粗暴提取正文（去脚本/样式/标签），截断后交给模型。
"""
from __future__ import annotations

import html as html_mod
import re
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from app.config.store import ConfigStore
from app.logbus import get_logger

log = get_logger("app.search")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


class WebSearch:
    def __init__(self, store: ConfigStore) -> None:
        self.store = store

    def _cfg(self):
        return self.store.get().search

    async def search(self, query: str) -> dict:
        cfg = self._cfg()
        if not cfg.enabled or cfg.web_provider == "off":
            return {"ok": False, "message": "网页搜索没开（配置页・联网搜集里选个搜索源）"}
        try:
            if cfg.web_provider == "searxng":
                return await self._searxng(query, cfg)
            return await self._duckduckgo(query, cfg)
        except httpx.ConnectError:
            return {"ok": False, "message": f"连不上搜索源（{cfg.searxng_url or cfg.web_provider}）"}
        except httpx.TimeoutException:
            return {"ok": False, "message": "搜索超时"}
        except Exception as e:
            log.warning("搜索出错：%s", e)
            return {"ok": False, "message": f"搜索出错：{e}"}

    async def _searxng(self, query: str, cfg) -> dict:
        base = (cfg.searxng_url or "").rstrip("/")
        if not base:
            return {"ok": False, "message": "还没填 SearXNG 地址"}
        async with httpx.AsyncClient(timeout=httpx.Timeout(cfg.timeout), headers={"User-Agent": UA}) as cx:
            r = await cx.get(f"{base}/search", params={"q": query, "format": "json"})
            r.raise_for_status()
            data = r.json()
        results = []
        for it in (data.get("results") or [])[: int(cfg.max_results)]:
            results.append({
                "title": str(it.get("title", "")).strip(),
                "url": str(it.get("url", "")).strip(),
                "snippet": str(it.get("content", "")).strip()[:300],
            })
        return {"ok": True, "keyword": query, "count": len(results), "results": results}

    async def _duckduckgo(self, query: str, cfg) -> dict:
        async with httpx.AsyncClient(timeout=httpx.Timeout(cfg.timeout), headers={"User-Agent": UA}) as cx:
            r = await cx.get("https://html.duckduckgo.com/html/", params={"q": query})
            r.raise_for_status()
            page = r.text
        results = []
        # 结果块：result__a（标题链接）与 result__snippet（摘要）
        for m in re.finditer(
            r'<a[^>]+class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            page, flags=re.S,
        ):
            url = html_mod.unescape(m.group(1))
            title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
            if "uddg=" in url:
                q = parse_qs(urlparse(url).query).get("uddg")
                if q:
                    url = unquote(q[0])
            if url.startswith("//"):
                url = "https:" + url
            results.append({"title": html_mod.unescape(title), "url": url, "snippet": ""})
            if len(results) >= int(cfg.max_results):
                break
        snippets = [re.sub(r"<[^>]+>", "", s).strip() for s in re.findall(
            r'<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', page, flags=re.S)]
        for i, s in enumerate(snippets):
            if i < len(results):
                results[i]["snippet"] = html_mod.unescape(s)[:300]
        return {"ok": True, "keyword": query, "count": len(results), "results": results}

    # ---- 抓取 ----
    async def fetch(self, url: str) -> dict:
        cfg = self._cfg()
        if not cfg.enabled or not cfg.fetch_enabled:
            return {"ok": False, "message": "网页抓取没开（配置页・联网搜集）"}
        if not url.lower().startswith(("http://", "https://")):
            return {"ok": False, "message": "只抓 http/https 链接"}
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(cfg.timeout), headers={"User-Agent": UA},
                follow_redirects=True,
            ) as cx:
                r = await cx.get(url)
                r.raise_for_status()
                ctype = (r.headers.get("content-type") or "").lower()
                if "text" not in ctype and "json" not in ctype and "xml" not in ctype:
                    return {"ok": False, "message": f"这个链接不是文本页面（{ctype or '未知类型'}）"}
                text = extract_text(r.text)
            text = text[: int(cfg.fetch_max_chars)]
            return {"ok": True, "url": str(r.url), "length": len(text), "text": text}
        except httpx.ConnectError:
            return {"ok": False, "message": "连不上这个网址"}
        except httpx.TimeoutException:
            return {"ok": False, "message": "抓取超时"}
        except Exception as e:
            return {"ok": False, "message": f"抓取出错：{e}"}


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head", "template"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "article", "section"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self.SKIP:
            self._skip_depth += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data.strip():
            self.parts.append(data.strip() + " ")

    def text(self) -> str:
        raw = "".join(self.parts)
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r"\n\s*\n+", "\n\n", raw)
        return raw.strip()


def extract_text(html: str) -> str:
    p = _TextExtractor()
    try:
        p.feed(html)
    except Exception:
        pass
    return p.text()