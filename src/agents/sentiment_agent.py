import requests
import logging
import json
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
from .base import BaseAgent
from ..core.config import Config
from ..core.database import DatabaseManager, NewsRaw

class SentimentAgent(BaseAgent):
    """Analyzes news sentiment using OpenRouter LLM"""
    
    def __init__(self, config: Config, logger: logging.Logger, db: DatabaseManager):
        super().__init__('Sentiment', logger)
        self.config = config
        self.db = db
        self.api_key = config.openrouter['api_key']
        self.api_base = config.openrouter['base_url']
        self.model = config.openrouter['model']
        self.temperature = config.openrouter.get('temperature', 0.3)
        self.max_tokens = config.openrouter.get('max_tokens', 1024)
    
    def call_llm(self, prompt: str) -> str:
        """Call OpenRouter API"""
        headers = {
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json',
            'HTTP-Referer': 'https://cryptotrader.local',
        }
        
        data = {
            'model': self.model,
            'messages': [{'role': 'user', 'content': prompt}],
            'temperature': self.temperature,
            'max_tokens': 100,  # We only need a number
        }
        
        try:
            resp = requests.post(
                f"{self.api_base}/chat/completions",
                headers=headers,
                json=data,
                timeout=30
            )
            result = resp.json()
            return result['choices'][0]['message']['content'].strip()
        except Exception as e:
            self.log('error', f"LLM call failed: {e}")
            return "0.0"
    
    def analyze_sentiment(self, title: str, summary: str = '') -> float:
        """Analyze sentiment of a single news item (-1 to +1)"""
        prompt = f"""Rate the crypto market sentiment of this news on a scale from -1.0 (very bearish) to +1.0 (very bullish). Return ONLY a number.

Title: {title}
Summary: {summary[:200]}

Sentiment score:"""
        
        try:
            result = self.call_llm(prompt)
            # Extract number from response
            score = float(result.replace('"', '').strip())
            return max(-1.0, min(1.0, score))  # Clamp
        except (ValueError, TypeError):
            return 0.0
    
    def get_unanalyzed_news(self, hours: int = 24) -> List[Dict]:
        """Get news items without sentiment score"""
        since = datetime.utcnow() - timedelta(hours=hours)
        
        with self.db.get_session() as session:
            news = session.query(NewsRaw).filter(
                NewsRaw.published_at >= since,
                NewsRaw.sentiment_score.is_(None)
            ).order_by(NewsRaw.published_at.desc()).limit(20).all()
            
            return [
                {
                    'id': n.id,
                    'title': n.title,
                    'summary': n.summary or '',
                    'source': n.source,
                }
                for n in news
            ]
    
    def update_sentiment(self, news_id: int, score: float):
        """Update sentiment score in database"""
        with self.db.get_session() as session:
            news = session.query(NewsRaw).filter_by(id=news_id).first()
            if news:
                news.sentiment_score = score
                news.sentiment_source = 'openrouter'
    
    def get_aggregated_sentiment(self, hours: int = 24) -> Dict[str, Any]:
        """Get aggregated sentiment for recent news"""
        since = datetime.utcnow() - timedelta(hours=hours)
        
        with self.db.get_session() as session:
            news = session.query(NewsRaw).filter(
                NewsRaw.published_at >= since,
                NewsRaw.sentiment_score.isnot(None)
            ).all()
            
            if not news:
                return {
                    'avg_sentiment': 0.0,
                    'bullish_ratio': 0.5,
                    'news_count': 0,
                    'sample_titles': [],
                }
            
            sentiments = [float(n.sentiment_score) for n in news]
            avg = sum(sentiments) / len(sentiments)
            bullish = sum(1 for s in sentiments if s > 0)
            
            return {
                'avg_sentiment': round(avg, 3),
                'bullish_ratio': round(bullish / len(sentiments), 3),
                'news_count': len(news),
                'sample_titles': [n.title for n in news[:5]],
            }
    
    def run_once(self) -> Dict[str, Any]:
        """Analyze sentiment for unanalyzed news"""
        self.log('info', "Starting sentiment analysis...")
        
        news = self.get_unanalyzed_news()
        
        analyzed = 0
        for item in news:
            score = self.analyze_sentiment(item['title'], item['summary'])
            self.update_sentiment(item['id'], score)
            analyzed += 1
            self.log('debug', f"Analyzed: {item['title'][:50]}... = {score}")
        
        # Get aggregated sentiment
        aggregated = self.get_aggregated_sentiment()
        
        self.log('info', f"Analyzed {analyzed} news items, avg sentiment: {aggregated['avg_sentiment']}")
        
        return {
            'analyzed': analyzed,
            'aggregated': aggregated,
            'timestamp': datetime.utcnow().isoformat(),
        }
