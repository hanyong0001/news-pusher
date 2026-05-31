#!/usr/bin/env python3
"""
新闻→A股投资机会分析引擎
将新闻事件映射到对应的A股概念板块和个股
"""

import json
import os
import re
import logging
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ============================================================
# 数据结构
# ============================================================

@dataclass
class Stock:
    code: str
    name: str
    desc: str

@dataclass
class ConceptMatch:
    concept_name: str
    category: str
    confidence: float  # 0-1
    stocks: List[Stock]
    logic: str
    matched_keywords: List[str]

@dataclass
class NewsAnalysis:
    """一条新闻的投资机会分析结果"""
    news_title: str
    news_url: str
    matched_concepts: List[ConceptMatch]
    overall_sentiment: str  # positive/negative/neutral
    urgency: str  # high/medium/low

    @property
    def has_matches(self) -> bool:
        return len(self.matched_concepts) > 0

    def best_match(self) -> Optional[ConceptMatch]:
        return self.matched_concepts[0] if self.matched_concepts else None


# ============================================================
# 概念引擎
# ============================================================

class AStockConceptEngine:
    """A股概念映射引擎 - 新闻→概念→个股"""

    def __init__(self, db_path: str = None):
        if db_path is None:
            db_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "stock_concepts.json"
            )
        self.db_path = db_path
        self.data = self._load_db()
        # Build keyword index for fast matching
        self._keyword_index = self._build_index()

    def _load_db(self) -> dict:
        try:
            with open(self.db_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            logger.error(f"加载概念数据库失败: {e}")
            return {"categories": {}}

    def _build_index(self) -> Dict[str, List[Tuple[str, str, int]]]:
        """
        Build inverted index: keyword -> [(category, concept_name, stock_count)]
        """
        index = {}
        cats = self.data.get("categories", {})
        for cat_name, cat_data in cats.items():
            concepts = cat_data.get("concepts", [])
            for ci, concept in enumerate(concepts):
                keywords = concept.get("keywords", [])
                stock_count = len(concept.get("stocks", []))
                for kw in keywords:
                    kw_lower = kw.lower()
                    if kw_lower not in index:
                        index[kw_lower] = []
                    index[kw_lower].append((cat_name, concept["name"], stock_count))
        return index

    def analyze(self, title: str, summary: str = "", source: str = "") -> NewsAnalysis:
        """
        Analyze a news item against the concept database
        Returns matched concepts sorted by confidence
        """
        combined = f"{title} {summary} {source}".lower()
        title_lower = title.lower()

        cats = self.data.get("categories", {})
        matches = []

        for cat_name, cat_data in cats.items():
            concepts = cat_data.get("concepts", [])
            for concept in concepts:
                matched_keywords = []
                for kw in concept.get("keywords", []):
                    kw_lower = kw.lower()
                    if kw_lower in combined:
                        matched_keywords.append(kw)

                if not matched_keywords:
                    continue

                # Calculate confidence score
                title_matches = sum(1 for kw in matched_keywords if kw.lower() in title_lower)
                coverage = len(matched_keywords) / max(len(concept["keywords"]), 1)

                # Weight: title match > summary match, keyword density matters
                base_score = 0.3
                title_bonus = 0.4 * min(title_matches / max(len(matched_keywords), 1), 1.0)
                coverage_bonus = 0.2 * min(coverage, 0.5)
                # More matched keywords = higher confidence
                count_bonus = 0.1 * min(len(matched_keywords) / 3, 1.0)

                confidence = min(base_score + title_bonus + coverage_bonus + count_bonus, 1.0)

                # Build stock list
                stocks = [
                    Stock(
                        code=s.get("code", ""),
                        name=s.get("name", ""),
                        desc=s.get("desc", "")
                    )
                    for s in concept.get("stocks", [])
                ]

                match = ConceptMatch(
                    concept_name=concept["name"],
                    category=cat_name,
                    confidence=round(confidence, 2),
                    stocks=stocks,
                    logic=concept.get("logic", ""),
                    matched_keywords=matched_keywords
                )
                matches.append(match)

        # Sort by confidence descending
        matches.sort(key=lambda x: x.confidence, reverse=True)

        # Determine urgency
        urgency = self._assess_urgency(matches, title_lower)

        # Determine sentiment
        sentiment = self._assess_sentiment(title_lower, summary.lower())

        return NewsAnalysis(
            news_title=title,
            news_url="",
            matched_concepts=matches[:5],  # Top 5 matches
            overall_sentiment=sentiment,
            urgency=urgency
        )

    def _assess_urgency(self, matches: List[ConceptMatch], title: str) -> str:
        """Assess urgency based on keywords"""
        urgent_keywords = [
            "突发", "紧急", "爆发", "战争", "冲突", "制裁", "崩盘", "暴跌",
            "大涨", "涨停", "重磅", "重大", "首次", "突破", "发射", "演习",
            "加息", "降息", "降准", "危机", "禁令", "出口管制"
        ]
        for kw in urgent_keywords:
            if kw in title:
                return "high"
        # High confidence matches also = high urgency
        if matches and matches[0].confidence >= 0.7:
            return "high"
        if matches and matches[0].confidence >= 0.4:
            return "medium"
        return "low"

    def _assess_sentiment(self, title: str, summary: str) -> str:
        """Determine overall sentiment of the news"""
        text = f"{title} {summary}"
        positive = ["突破", "增长", "利好", "大涨", "创新高", "突破", "繁荣",
                    "反转", "复苏", "刺激", "降息", "放水"]
        negative = ["战争", "冲突", "制裁", "危机", "暴跌", "崩盘", "衰退",
                   "加息", "紧缩", "下滑", "下降", "萎缩", "抗议", "动荡"]

        pos_score = sum(1 for w in positive if w in text)
        neg_score = sum(1 for w in negative if w in text)

        if pos_score > neg_score:
            return "positive"
        elif neg_score > pos_score:
            return "negative"
        return "neutral"

    def analyze_batch(self, news_dict: Dict[str, List]) -> Dict[str, List[NewsAnalysis]]:
        """Analyze a dictionary of categorized news items"""
        result = {}
        for cat, items in news_dict.items():
            result[cat] = []
            for item in items:
                analysis = self.analyze(item.title, item.summary, item.source)
                analysis.news_url = item.url
                result[cat].append(analysis)
        return result

    @staticmethod
    def format_investment_advice(analysis: NewsAnalysis, max_stocks: int = 5) -> str:
        """Format analysis into a readable investment advice string"""
        if not analysis.has_matches:
            return ""

        parts = [f"📊 **A股映射分析**"]

        sentiment_emoji = {
            "positive": "📈",
            "negative": "📉",
            "neutral": "➡️"
        }
        urgency_label = {
            "high": "🔴 高紧迫",
            "medium": "🟡 中等",
            "low": "🟢 关注"
        }

        parts.append(f"影响方向: {sentiment_emoji.get(analysis.overall_sentiment, '➡️')} {analysis.overall_sentiment} | 紧迫度: {urgency_label.get(analysis.urgency, '关注')}")

        for i, match in enumerate(analysis.matched_concepts[:3], 1):
            # Confidence bar
            bar_len = int(match.confidence * 10)
            confidence_bar = "▓" * bar_len + "░" * (10 - bar_len)

            parts.append(f"\n{i}. **{match.concept_name}** [{confidence_bar} {int(match.confidence*100)}%]")
            parts.append(f"   🔑 关键词匹配: {', '.join(match.matched_keywords[:5])}")

            # Show top stocks
            top_stocks = match.stocks[:max_stocks]
            stock_strs = []
            for s in top_stocks:
                stock_strs.append(f"{s.name}({s.code})")
            parts.append(f"   🎯 关注个股: {' | '.join(stock_strs)}")

            # Brief logic
            logic_short = match.logic[:80] + ("..." if len(match.logic) > 80 else "")
            parts.append(f"   💡 逻辑: {logic_short}")

        return "\n".join(parts)

    @staticmethod
    def format_summary_table(all_analyses: Dict[str, List[NewsAnalysis]]) -> str:
        """Format overview table of all investment opportunities"""
        results = []

        # Count matches
        total_news = sum(len(v) for v in all_analyses.values())
        total_matched = sum(
            sum(1 for a in analyses if a.has_matches)
            for analyses in all_analyses.values()
        )
        high_urgency = sum(
            sum(1 for a in analyses if a.urgency == "high")
            for analyses in all_analyses.values()
        )

        results.append(f"📊 共分析 {total_news} 条新闻 | {total_matched} 条有映射 | {high_urgency} 条高紧迫")

        # Collect all unique concepts with their max confidence
        concept_map = {}
        for cat, analyses in all_analyses.items():
            for analysis in analyses:
                for match in analysis.matched_concepts:
                    key = f"{match.category}::{match.concept_name}"
                    if key not in concept_map or match.confidence > concept_map[key][0]:
                        concept_map[key] = (match.confidence, match)

        # Sort by confidence descending
        sorted_concepts = sorted(
            concept_map.values(),
            key=lambda x: x[0],
            reverse=True
        )

        if sorted_concepts:
            results.append(f"\n🏆 **今日热点概念 TOP10**")
            for i, (_, match) in enumerate(sorted_concepts[:10], 1):
                stocks_sample = ", ".join(
                    [f"{s.name}" for s in match.stocks[:3]]
                )
                bar = "▓" * int(match.confidence * 10) + "░" * (10 - int(match.confidence * 10))
                results.append(
                    f"  {i}. {match.concept_name} "
                    f"[{int(match.confidence*100)}%] "
                    f"→ {stocks_sample}"
                )

        return "\n".join(results)


# ============================================================
# 快速测试
# ============================================================

def test_engine():
    engine = AStockConceptEngine()
    test_cases = [
        "中东局势升级：伊朗向以色列发射导弹，国际油价大涨",
        "AI大模型突破：OpenAI发布GPT-5，算力需求暴增",
        "央行宣布降息降准，释放流动性超预期",
        "台海紧张：解放军举行大规模环岛军演",
        "华为发布麒麟芯片新突破，国产芯片弯道超车",
    ]
    print("=== A股映射测试 ===")
    for news in test_cases:
        print(f"\n{'='*50}")
        print(f"新闻: {news}")
        analysis = engine.analyze(news, "", "测试")
        print(engine.format_investment_advice(analysis))
        print(f"{'='*50}")

if __name__ == "__main__":
    test_engine()
