import requests
import logging
import json
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
from .base import BaseAgent
from ..core.config import Config
from ..core.database import DatabaseManager, NewsRaw


class SentimentAgent(BaseAgent):
    """Analyzes news sentiment and stores in RAGFlow"""
    
    # Ollama fallback server
    OLLAMA_BASE = "http://192.168.0.94:11434"
    OLLAMA_MODEL = "gemma4:e2b"
    OLLAMA_FALLBACK = "gemma4:e2b"   # qwen3.5:9b not present on host; 26b too slow
    
    def __init__(self, config: Config, logger: logging.Logger, db: DatabaseManager):
        super().__init__('Sentiment', logger)
        self.config = config
        self.db = db
        self.api_key = config.openrouter['api_key']
        self.api_base = config.openrouter['base_url']
        self.model = config.openrouter['model']
        self.temperature = config.openrouter.get('temperature', 0.3)
        self.max_tokens = config.openrouter.get('max_tokens', 1024)
        
        self.ragflow_enabled = False
        self.ragflow = None
    
    def call_llm_ollama(self, prompt: str) -> str:
        """Call Ollama API with fallback to alternate model"""
        models = [self.OLLAMA_MODEL, self.OLLAMA_FALLBACK]
        
        for model in models:
            try:
                data = {
                    'model': model,
                    'prompt': prompt,
                    'temperature': self.temperature,
                    'max_tokens': 100,
                    'stream': False,
                }
                resp = requests.post(
                    f"{self.OLLAMA_BASE}/api/generate",
                    json=data,
                    timeout=(3, 30)  # (connect, read): fail fast if host unreachable
                )
                if resp.status_code != 200:
                    self.log('warning', f"Ollama model {model} returned status {resp.status_code}, trying fallback...")
                    continue
                result = resp.json()
                response = result.get('response', '').strip()
                if response:
                    # Extract numeric value from response
                    import re
                    match = re.search(r'[-+]?\d*\.?\d+', response)
                    if match:
                        score = match.group()
                        self.log('info', f"Ollama succeeded with model: {model}, extracted: {score}")
                        return score
                    else:
                        self.log('warning', f"No numeric value found in Ollama response: {response[:100]}")
                else:
                    self.log('warning', f"Empty response from Ollama model {model}")
            except Exception as e:
                self.log('warning', f"Ollama model {model} failed: {e}, trying fallback...")
                continue
        
        self.log('error', "All Ollama models failed")
        raise RuntimeError("All Ollama models failed")
    
    OPENCODE_BASE = "https://opencode.ai/zen/v1"
    OPENCODE_MODEL = "deepseek-v4-flash-free"

    def _call_opencode(self, prompt: str) -> str:
        """Call opencode-zen (deepseek-v4-flash-free) for sentiment scoring."""
        import os
        api_key = os.getenv('OPENCODE_API_KEY', 'public')
        headers = {
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        }
        data = {
            'model': self.OPENCODE_MODEL,
            'messages': [
                {'role': 'system', 'content': 'You are a crypto sentiment scorer. Output a single decimal number between -1.0 (very bearish) and +1.0 (very bullish). No other text.'},
                {'role': 'user', 'content': prompt},
            ],
            'temperature': 0.1,
            'max_tokens': 50,
        }
        resp = requests.post(
            f"{self.OPENCODE_BASE}/chat/completions",
            headers=headers, json=data, timeout=30,
        )
        if resp.status_code >= 400:
            raise ValueError(f"opencode HTTP {resp.status_code}: {resp.text[:200]}")
        result = resp.json()
        content = result['choices'][0]['message'].get('content', '').strip()
        import re
        m = re.search(r'[-+]?\d*\.?\d+', content)
        if not m:
            raise ValueError(f"no numeric value in opencode response: {content[:100]}")
        return m.group()

    def _call_ruapi_sentiment(self, prompt: str) -> str:
        """RuAPI proxy (api.stepanovikov.uno) for sentiment scoring. Returns numeric string.
        Needs JSON/number-forcing system prompt + browser User-Agent (Cloudflare 1010)."""
        import os, re
        api_key = os.environ.get('RUAPI_API_KEY', '')
        if not api_key:
            raise ValueError("RUAPI_API_KEY not set")
        headers = {
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) cryptotrader/1.0',
        }
        # NB: request JSON, not a bare number — the proxy routes bare "give me a number"
        # prompts to a coding assistant ("I'm Kiro...") that refuses; JSON framing avoids it.
        data = {
            'model': 'gpt-5.4',  # haiku on this proxy routes to Amazon Q ('Kiro') and refuses
            'messages': [
                {'role': 'system', 'content': 'You are a crypto news sentiment scoring API. Output ONLY valid JSON: {"sentiment": <number from -1.0 very bearish to 1.0 very bullish>}. No prose, no refusal.'},
                {'role': 'user', 'content': prompt},
            ],
            'temperature': 0.1,
            'max_tokens': 40,
        }
        resp = requests.post('https://api.stepanovikov.uno/v1/chat/completions',
                             headers=headers, json=data, timeout=30)
        if resp.status_code != 200:
            raise ValueError(f"RuAPI HTTP {resp.status_code}: {resp.text[:150]}")
        content = resp.json()['choices'][0]['message'].get('content', '').strip()
        m = re.search(r'[-+]?\d*\.?\d+', content)
        if not m:
            raise ValueError(f"no numeric value in RuAPI response: {content[:100]}")
        return m.group()

    _BULL_WORDS = ('surge', 'rally', 'soar', 'gain', 'jump', 'bullish', 'breakout',
                   'adopt', 'approval', 'upgrade', 'partnership', 'record high', 'inflow', 'buy')
    _BEAR_WORDS = ('crash', 'plunge', 'drop', 'fall', 'bearish', 'hack', 'exploit', 'ban',
                   'lawsuit', 'sell-off', 'liquidation', 'outflow', 'fraud', 'collapse', 'dump')

    def _keyword_sentiment(self, title: str, summary: str = '') -> float:
        """Free offline fallback when every LLM provider is unavailable."""
        text = f"{title} {summary}".lower()
        score = (sum(0.2 for w in self._BULL_WORDS if w in text)
                 - sum(0.2 for w in self._BEAR_WORDS if w in text))
        return max(-1.0, min(1.0, score))

    def analyze_sentiment(self, title: str, summary: str = '') -> float:
        """Analyze sentiment of a single news item (-1 to +1).
        Chain: RuAPI haiku -> Ollama -> keyword fallback."""
        prompt = (
            f"Rate the crypto market sentiment of this news from -1.0 (very bearish) to +1.0 (very bullish).\n"
            f"Title: {title}\nSummary: {summary[:200]}\nSentiment score:"
        )
        try:
            return max(-1.0, min(1.0, float(self._call_ruapi_sentiment(prompt))))
        except Exception as e:
            self.log('warning', f"RuAPI sentiment failed ({e}), trying Ollama")
        try:
            return max(-1.0, min(1.0, float(self.call_llm_ollama(prompt))))
        except Exception as e:
            self.log('warning', f"Ollama sentiment failed ({e}), keyword fallback")
        return self._keyword_sentiment(title, summary)
    
    def get_unanalyzed_news(self, hours: int = 24, symbol: str = None) -> List[Dict]:
        """Get news items without sentiment score, optionally filtered by symbol."""
        since = datetime.utcnow() - timedelta(hours=hours)
        
        with self.db.get_session() as session:
            query = session.query(NewsRaw).filter(
                NewsRaw.published_at >= since,
                NewsRaw.sentiment_score.is_(None)
            )
            news = query.order_by(NewsRaw.published_at.desc()).limit(20).all()
            
            # Filter by symbol if provided
            if symbol:
                news = [n for n in news if n.symbols and symbol.upper() in n.symbols]
            
            return [
                {
                    'id': n.id,
                    'title': n.title,
                    'summary': n.summary or '',
                    'source': n.source,
                    'url': n.url,
                    'symbols': n.symbols or [],
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
    
    def get_aggregated_sentiment(self, hours: int = 24, symbol: str = None) -> Dict[str, Any]:
        """Get aggregated sentiment for recent news, optionally filtered by symbol."""
        since = datetime.utcnow() - timedelta(hours=hours)
        
        with self.db.get_session() as session:
            news = session.query(NewsRaw).filter(
                NewsRaw.published_at >= since,
                NewsRaw.sentiment_score.isnot(None)
            ).all()
            
            # Filter by symbol if provided (Python-side filtering)
            if symbol:
                news = [n for n in news if n.symbols and symbol.upper() in n.symbols]
            
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
        """Analyze sentiment and store in RAGFlow"""
        self.log('info', "Starting sentiment analysis...")
        
        news = self.get_unanalyzed_news()
        
        analyzed = 0
        stored_in_rag = 0
        
        for item in news:
            score = self.analyze_sentiment(item['title'], item['summary'])
            self.update_sentiment(item['id'], score)
            analyzed += 1
            self.log('debug', f"Analyzed: {item['title'][:50]}... = {score}")
            
            # Store in RAGFlow
            if self.ragflow_enabled:
                try:
                    self.ragflow.store_news(
                        title=item['title'],
                        summary=item['summary'],
                        source=item['source'],
                        url=item['url'],
                        sentiment=score,
                    )
                    stored_in_rag += 1
                except Exception as e:
                    self.log('warning', f"Failed to store news in RAGFlow: {e}")
        
        aggregated = self.get_aggregated_sentiment()
        
        self.log('info', f"Analyzed {analyzed} news, {stored_in_rag} stored in RAG, avg sentiment: {aggregated['avg_sentiment']}")
        
        return {
            'analyzed': analyzed,
            'stored_in_rag': stored_in_rag,
            'aggregated': aggregated,
            'timestamp': datetime.utcnow().isoformat(),
        }
