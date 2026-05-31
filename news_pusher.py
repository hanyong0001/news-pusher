#!/usr/bin/env python3
"""
新闻热点推送系统
自动抓取 RSS 源 → 分类聚合 → 推送到微信 (WxPusher)
"""

import feedparser
import requests
import json
import time
import os
import re
import html
import logging
import hashlib
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Set, Tuple
from dataclasses import dataclass, field, asdict

# A股分析引擎
from stock_analyzer import AStockConceptEngine, NewsAnalysis

# ============================================================
# 配置
# ============================================================

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
SEEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".seen_ids.json")
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "news_pusher.log")
CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".news_cache.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


# ============================================================
# 数据结构
# ============================================================

@dataclass
class NewsItem:
    title: str
    url: str
    summary: str
    source: str
    category: str
    published: str
    id_hash: str = ""
    keywords_matched: List[str] = field(default_factory=list)

    def __post_init__(self):
        if not self.id_hash:
            raw = f"{self.title}|{self.url}|{self.source}"
            self.id_hash = hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]


# ============================================================
# 配置管理
# ============================================================

class Config:
    def __init__(self, path: str = CONFIG_PATH):
        self.path = path
        self.data = self._load()

    def _load(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            logger.error(f"读取配置文件失败: {e}")
            return {}

    def reload(self):
        self.data = self._load()

    def get(self, *keys, default=None):
        val = self.data
        for k in keys:
            if isinstance(val, dict):
                val = val.get(k)
            else:
                return default
        return val if val is not None else default

    @property
    def app_token(self) -> str:
        # 优先使用环境变量（支持 GitHub Actions 注入密钥）
        env_token = os.environ.get("WXPUSHER_APP_TOKEN")
        if env_token:
            return env_token
        return self.get("wxpusher", "appToken", default="")

    @property
    def uids(self) -> List[str]:
        # 优先使用环境变量
        env_uids = os.environ.get("WXPUSHER_UIDS")
        if env_uids:
            return [uid.strip() for uid in env_uids.split(",") if uid.strip()]
        return self.get("wxpusher", "uid", default=[])

    @property
    def enabled_categories(self) -> List[str]:
        cats = self.get("categories", default={})
        return [k for k, v in cats.items() if v.get("enabled", False)]

    def get_sources(self, category: str) -> List[dict]:
        return self.get("rss_sources", category, default=[])

    def get_category_config(self, category: str) -> dict:
        return self.get("categories", category, default={})

    @property
    def stock_analysis_enabled(self) -> bool:
        return self.get("stock_analysis", "enabled", default=True)

    @property
    def stock_analysis_max_stocks(self) -> int:
        return self.get("stock_analysis", "max_stocks_per_concept", default=5)

    @property
    def stock_analysis_max_concepts(self) -> int:
        return self.get("stock_analysis", "max_concepts_per_news", default=3)


# ============================================================
# 已读去重 (持久化)
# ============================================================

class SeenManager:
    def __init__(self, path: str = SEEN_FILE):
        self.path = path
        self.seen: Set[str] = set()
        self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.seen = set(data.get("ids", []))
        except (FileNotFoundError, json.JSONDecodeError):
            self.seen = set()

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"ids": list(self.seen)[-2000:]}, f, ensure_ascii=False, indent=2)

    def is_new(self, item: NewsItem) -> bool:
        return item.id_hash not in self.seen

    def mark_seen(self, item: NewsItem):
        self.seen.add(item.id_hash)

    def mark_seen_batch(self, items: List[NewsItem]):
        for item in items:
            self.seen.add(item.id_hash)

    def cleanup(self):
        """Keep only last 2000 entries"""
        if len(self.seen) > 2000:
            self.seen = set(list(self.seen)[-2000:])


# ============================================================
# RSS 抓取
# ============================================================

class RSSFetcher:
    def __init__(self, timeout: int = 15, user_agent: str = None):
        self.timeout = timeout
        self.user_agent = user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        )

    def fetch(self, url: str) -> List[dict]:
        """Fetch and parse RSS feed, return raw entries"""
        try:
            headers = {"User-Agent": self.user_agent}
            resp = requests.get(url, headers=headers, timeout=self.timeout)
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
            return feed.entries
        except Exception as e:
            logger.warning(f"抓取 RSS 失败 [{url[:50]}...]: {e}")
            return []

    @staticmethod
    def extract_entry(entry: dict, source_name: str) -> dict:
        """Normalize a feed entry to standard fields"""
        title = entry.get("title", "")
        link = entry.get("link", entry.get("guid", ""))
        published = entry.get("published", entry.get("updated", ""))

        # Extract summary/description
        summary = entry.get("summary", entry.get("description", ""))
        # Strip HTML tags
        summary = re.sub(r"<[^>]+>", "", summary)
        summary = html.unescape(summary)
        title = html.unescape(title)

        return {
            "title": title.strip(),
            "url": link.strip(),
            "summary": summary.strip(),
            "source": source_name,
            "published": published,
        }


# ============================================================
# 新闻分类 & 标签匹配
# ============================================================

class NewsClassifier:
    def __init__(self, config: Config):
        self.config = config

    def classify(self, items: List[dict]) -> Dict[str, List[NewsItem]]:
        """Classify raw items into categories"""
        result: Dict[str, List[NewsItem]] = {cat: [] for cat in self.config.enabled_categories}

        for item in items:
            title = item.get("title", "")
            summary = item.get("summary", "")
            text = f"{title} {summary}".lower()

            best_cat = None
            best_score = 0

            for cat in self.config.enabled_categories:
                cat_config = self.config.get_category_config(cat)
                keywords = cat_config.get("keywords", [])
                score = 0
                matched = []

                for kw in keywords:
                    if kw.lower() in text:
                        score += 1
                        matched.append(kw)

                # Name-based heuristic: if source or category name appears
                cat_lower = cat.lower()
                cat_parts = re.split(r"[_/\\,， ]+", cat_lower)
                if any(part and part in text for part in cat_parts):
                    score += 0.5

                if score > best_score:
                    best_score = score
                    best_cat = cat

            # If no keyword match, put in the most relevant category by name
            # or assign based on source
            target_cat = best_cat or self._infer_category_from_source(
                item.get("source", ""), text
            )

            if target_cat and target_cat in result:
                news_item = NewsItem(
                    title=item["title"],
                    url=item["url"],
                    summary=item["summary"],
                    source=item["source"],
                    category=target_cat,
                    published=item["published"],
                    keywords_matched=[],  # TODO: fill matched keywords
                )
                result[target_cat].append(news_item)

        return result

    def _infer_category_from_source(self, source: str, text: str) -> Optional[str]:
        """Fallback: infer category from source name"""
        source_lower = source.lower()
        for cat in self.config.enabled_categories:
            cat_lower = cat.lower()
            if cat_lower in source_lower:
                return cat
        return None


# ============================================================
# 消息格式化
# ============================================================

class MessageFormatter:
    @staticmethod
    def format_category(items: List[NewsItem], category: str, max_items: int = 5) -> str:
        """Format a category's news items into a readable string"""
        if not items:
            return ""

        # Emoji mapping
        emoji_map = {
            "军事_战争": "🔞",
            "科技": "💻",
            "财经": "💰",
        }
        emoji = emoji_map.get(category, "📰")

        lines = [f"\n{'='*35}", f"{emoji} 【{category}】热点 {emoji}", f"{'='*35}"]

        for i, item in enumerate(items[:max_items], 1):
            title = item.title[:60] + ("..." if len(item.title) > 60 else "")
            # Truncate summary
            summary = item.summary[:120] + ("..." if len(item.summary) > 120 else "")
            # Clean summary for WeChat display
            summary = summary.replace("\n", " ").replace("\r", "")

            lines.append(f"\n{i}. {title}")
            if summary:
                lines.append(f"   {summary}")
            lines.append(f"   🔗 {item.url}")
            lines.append(f"   📡 {item.source}")

        lines.append("")
        return "\n".join(lines)

    @staticmethod
    def format_breaking_alert(item: NewsItem) -> str:
        """Format a single breaking news alert"""
        title = item.title[:60] + ("..." if len(item.title) > 60 else "")
        summary = item.summary[:150] + ("..." if len(item.summary) > 150 else "")
        summary = summary.replace("\n", " ").replace("\r", "")

        return (
            f"🚨 **突发新闻** 🚨\n\n"
            f"📌 {title}\n"
            f"📝 {summary}\n"
            f"🔗 {item.url}\n"
            f"📡 {item.source} | {item.category}"
        )

    @staticmethod
    def format_digest(all_news: Dict[str, List[NewsItem]], config: Config,
                      all_analyses: Optional[Dict[str, List[NewsAnalysis]]] = None) -> str:
        """Format a full digest message, optionally with A-share analysis"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        parts = [
            f"📰 **新闻晚报 | {now}** 📰\n",
            f"共 {sum(len(v) for v in all_news.values())} 条热点"
        ]

        # Add A-share analysis summary if available
        if all_analyses and config.stock_analysis_enabled:
            from stock_analyzer import AStockConceptEngine as _
            summary = _.format_summary_table(all_analyses)
            if summary:
                parts.append(f"\n{summary}\n")

        for cat in config.enabled_categories:
            items = all_news.get(cat, [])
            max_items = config.get_category_config(cat).get("max_items", 5)
            formatted = MessageFormatter.format_category(items, cat, max_items)
            if formatted:
                parts.append(formatted)

            # Add A-share analysis for this category if available
            if all_analyses and config.stock_analysis_enabled:
                cat_analyses = all_analyses.get(cat, [])
                # Only show analyses for news that had matches
                matched = [a for a in cat_analyses if a.has_matches]
                if matched:
                    parts.append("\n📊 **A股映射详情**")
                    for i, analysis in enumerate(matched[:3], 1):
                        advice = AStockConceptEngine.format_investment_advice(
                            analysis,
                            max_stocks=config.stock_analysis_max_stocks
                        )
                        if advice:
                            parts.append(f"\n  ═══ 新闻 {i} ═══")
                            parts.append(f"  {analysis.news_title[:50]}")
                            parts.append(advice)

        # Footer
        parts.append(f"\n---\n🤖 自动推送 | 含A股投资机会分析")

        return "\n".join(parts)


# ============================================================
# WxPusher 推送
# ============================================================

class WxPusher:
    """微信推送服务 (https://wxpusher.zjiecode.com)"""
    API_URL = "https://wxpusher.zjiecode.com/api/send/message"
    QUERY_URL = "https://wxpusher.zjiecode.com/api/fun/create/qrcode"

    def __init__(self, app_token: str, uids: List[str]):
        self.app_token = app_token
        self.uids = uids
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json"
        })

    def send(self, content: str, title: str = "新闻推送", content_type: int = 1) -> bool:
        """
        发送消息到微信
        content_type: 1=纯文字, 2=html, 3=markdown
        """
        if not self.app_token:
            logger.error("WxPusher AppToken 未配置！请先配置 config.json")
            return False
        if not self.uids:
            logger.error("WxPusher UID 未配置！请先关注并获取 UID")
            return False

        payload = {
            "appToken": self.app_token,
            "content": content,
            "contentType": content_type,
            "uids": self.uids,
            "url": "",
        }

        try:
            resp = self.session.post(self.API_URL, json=payload, timeout=10)
            result = resp.json()
            if result.get("code") == 1000:
                logger.info(f"✅ 推送成功: {title} ({len(content)} chars)")
                return True
            else:
                logger.error(f"❌ 推送失败: {result.get('msg', '未知错误')}")
                return False
        except Exception as e:
            logger.error(f"❌ 推送异常: {e}")
            return False

    @staticmethod
    def get_qrcode_url(app_token: str) -> Optional[str]:
        """获取关注二维码 URL（用于首次配置）"""
        try:
            resp = requests.post(
                "https://wxpusher.zjiecode.com/api/fun/create/qrcode",
                json={"appToken": app_token},
                timeout=10
            )
            data = resp.json()
            if data.get("code") == 1000:
                return data.get("data")
        except Exception as e:
            logger.error(f"获取二维码失败: {e}")
        return None


# ============================================================
# 缓存（避免频繁 RSS 请求导致被封）
# ============================================================

class NewsCache:
    def __init__(self, path: str = CACHE_FILE, ttl_minutes: int = 15):
        self.path = path
        self.ttl = timedelta(minutes=ttl_minutes)

    def get(self) -> Optional[List[dict]]:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            cached_time = datetime.fromisoformat(data.get("timestamp", ""))
            if datetime.now() - cached_time < self.ttl:
                return data.get("items", [])
        except (FileNotFoundError, json.JSONDecodeError, ValueError):
            pass
        return None

    def set(self, items: List[dict]):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({
                "timestamp": datetime.now().isoformat(),
                "items": items,
            }, f, ensure_ascii=False, indent=2)


# ============================================================
# 主调度器
# ============================================================

class NewsPusher:
    def __init__(self, config_path: str = CONFIG_PATH):
        self.config = Config(config_path)
        self.seen = SeenManager()
        self.fetcher = RSSFetcher()
        self.classifier = NewsClassifier(self.config)
        self.formatter = MessageFormatter()
        self.cache = NewsCache(
            ttl_minutes=self.config.get("general", "fetch_interval_minutes", default=15)
        )

        # Init WxPusher
        self.pusher = WxPusher(
            self.config.app_token,
            self.config.uids
        )

        # Init A-share analysis engine
        self.stock_engine = AStockConceptEngine()
        logger.info(f"A股分析引擎: {'已启用' if self.config.stock_analysis_enabled else '已禁用'}")

    def fetch_all(self, use_cache: bool = True) -> List[dict]:
        """Fetch all RSS sources"""
        # Try cache first
        if use_cache:
            cached = self.cache.get()
            if cached:
                logger.info(f"使用缓存 ({len(cached)} 条)")
                return cached

        all_items = []
        for cat in self.config.enabled_categories:
            sources = self.config.get_sources(cat)
            for source in sources:
                name = source.get("name", "未知源")
                url = source.get("url", "")
                logger.info(f"抓取: {name}")
                entries = self.fetcher.fetch(url)
                for entry in entries:
                    item = RSSFetcher.extract_entry(entry, name)
                    item["_category"] = cat
                    all_items.append(item)
                time.sleep(1)  # Polite delay between requests
                logger.info(f"  → 获取 {len(entries)} 条")

        # Update cache
        if all_items:
            self.cache.set(all_items)

        logger.info(f"总共获取 {len(all_items)} 条原始数据")
        return all_items

    def process(self, raw_items: List[dict]) -> Dict[str, List[NewsItem]]:
        """Classify and deduplicate"""
        classified = self.classifier.classify(raw_items)

        # Deduplicate and filter
        result = {}
        for cat, items in classified.items():
            new_items = [item for item in items if self.seen.is_new(item)]
            self.seen.mark_seen_batch(new_items)

            # Sort by published time (newest first)
            new_items.sort(
                key=lambda x: x.published or "",
                reverse=True
            )
            result[cat] = new_items
            logger.info(f"  {cat}: {len(new_items)} 条新内容")

        self.seen.save()
        return result

    def analyze_news(self, news: Dict[str, List[NewsItem]]) -> Dict[str, List[NewsAnalysis]]:
        """Run A-share analysis on all news items"""
        if not self.config.stock_analysis_enabled:
            return {}
        logger.info("开始A股概念分析...")
        analyses = {}
        for cat, items in news.items():
            if not items:
                continue
            cat_analyses = []
            for item in items:
                analysis = self.stock_engine.analyze(item.title, item.summary, item.source)
                analysis.news_url = item.url
                cat_analyses.append(analysis)
                if analysis.has_matches:
                    logger.info(f"  [{cat}] {analysis.news_title[:30]}... -> {analysis.best_match().concept_name} ({int(analysis.best_match().confidence*100)}%)")
            analyses[cat] = cat_analyses
        logger.info(f"A股分析完成")
        return analyses

    def send_digest(self, news: Dict[str, List[NewsItem]],
                    analyses: Optional[Dict[str, List[NewsAnalysis]]] = None):
        """Send formatted digest to WeChat"""
        message = self.formatter.format_digest(news, self.config, all_analyses=analyses)
        if len(message.strip()) < 50:
            logger.info("没有新内容，跳过推送")
            return False

        # WxPusher supports markdown
        success = self.pusher.send(message, title="新闻热点推送", content_type=3)
        return success

    def send_breaking(self, item: NewsItem):
        """Send a breaking news alert"""
        message = self.formatter.format_breaking_alert(item)
        self.pusher.send(message, title=f"🚨 {item.category}突发", content_type=3)

    def run_once(self) -> bool:
        """Execute one full cycle: fetch -> classify -> analyze -> push"""
        logger.info("=" * 50)
        logger.info("开始新闻抓取和推送...")
        logger.info("=" * 50)

        try:
            raw_items = self.fetch_all(use_cache=True)
            if not raw_items:
                logger.warning("未获取到任何新闻")
                return False

            news = self.process(raw_items)
            total_new = sum(len(v) for v in news.values())
            logger.info(f"共 {total_new} 条新内容")

            if total_new == 0:
                return False

            # Run A-share analysis
            analyses = self.analyze_news(news)

            # Send with analysis
            success = self.send_digest(news, analyses=analyses)
            return success

        except Exception as e:
            logger.exception(f"运行异常: {e}")
            return False

    def run_loop(self, interval_minutes: int = 30):
        """Run continuously"""
        logger.info(f"启动持续运行模式，每 {interval_minutes} 分钟检查一次")
        while True:
            try:
                self.run_once()
            except Exception as e:
                logger.error(f"循环异常: {e}")

            logger.info(f"等待 {interval_minutes} 分钟后下一次检查...")
            time.sleep(interval_minutes * 60)


# ============================================================
# 快速测试：验证 WxPusher 配置
# ============================================================

def test_push():
    """Test WxPusher configuration"""
    config = Config()
    pusher = WxPusher(config.app_token, config.uids)

    if not config.app_token or not config.uids:
        print("[ERROR] WxPusher 未配置！")
        print("\n请按以下步骤配置:")
        print("1. 打开 https://wxpusher.zjiecode.com/")
        print("2. 注册/登录 -> 创建应用 -> 获取 AppToken")
        print("3. 扫描关注二维码 -> 在管理后台查看关注者 UID")
        print("4. 将 AppToken 和 UID 填入 config.json")
        return

    print(f"AppToken: {config.app_token[:8]}...{config.app_token[-4:]}")
    print(f"UIDs: {config.uids}")

    # Show QR code URL
    qr_url = WxPusher.get_qrcode_url(config.app_token)
    if qr_url:
        print(f"\n>>> 关注二维码: {qr_url}")

    msg = (
        "🚀 **新闻推送系统测试** 🚀\n\n"
        "✅ 配置正确，推送正常！\n\n"
        f"📡 已开启分类:\n"
        + "\n".join(f"  - {c}" for c in config.enabled_categories) +
        f"\n\n推送时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )

    success = pusher.send(msg, title="新闻推送 - 系统测试", content_type=3)
    if success:
        print("\n[OK] 测试推送成功！请查看你的微信。")
    else:
        print("\n[ERROR] 推送失败，请检查配置。")


# ============================================================
# 入口
# ============================================================

if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "test":
            test_push()
        elif cmd == "once":
            pusher = NewsPusher()
            pusher.run_once()
        elif cmd == "loop":
            interval = int(sys.argv[2]) if len(sys.argv) > 2 else 30
            pusher = NewsPusher()
            pusher.run_loop(interval)
        elif cmd == "qr":
            config = Config()
            url = WxPusher.get_qrcode_url(config.app_token)
            if url:
                print(f">>> 关注二维码 URL: {url}")
            else:
                print(">>> 获取二维码失败，请检查 AppToken")
        elif cmd == "analyze":
            from stock_analyzer import test_engine
            test_engine()
        else:
            print(f"未知命令: {cmd}")
    else:
        msg = """
[新闻推送系统] - 使用说明

用法:
  python news_pusher.py test     - 测试微信推送配置
  python news_pusher.py once     - 立即抓取并推送一次
  python news_pusher.py loop N   - 持续运行，每 N 分钟推送一次
  python news_pusher.py qr       - 获取微信关注二维码
  python news_pusher.py analyze  - 测试A股分析引擎（无需配置）

功能特色:
  - 战争/军事新闻 -> 军工/石油/黄金概念映射+个股推荐
  - 科技新闻 -> AI/芯片/机器人/新能源概念映射+个股推荐
  - 财经新闻 -> 金融/地产/周期股概念映射+个股推荐

首次使用请先:
  1. 编辑 config.json 填入 WxPusher 的 AppToken 和 UID
  2. 运行 python news_pusher.py test 验证配置
"""
        print(msg.strip())
