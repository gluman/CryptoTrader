import requests
import logging
import json
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
from .base import BaseAgent
from ..core.config import Config
from ..core.database import DatabaseManager, NewsRaw
from ..gateways import RAGFlowAPI


class SentimentAgent(BaseAgent):
    """Analyzes news sentiment and stores in RAGFlow"""
    
    # Ollama fallback server
    OLLAMA_BASE = "http://192.168.0.94:11434"
    OLLAMA_MODEL = "gemma4:latest"
    OLLAMA_FALLBACK = "qwen3.5:9b"
    
    def __init__(self, config: Config, logger: logging.Logger, db: DatabaseManager):
        super().__init__('Sentiment', logger)
        self.config = config
        self.db = db
        self.api_key = config.openrouter['api_key']
        self.api_base = config.openrouter['base_url']
        self.model = config.openrouter['model']
        self.temperature = config.openrouter.get('temperature', 0.3)
        self.max_tokens = config.openrouter.get('max_tokens', 1024)
        
        # Initialize RAGFlow
        ragflow_cfg = config.ragflow
        self.ragflow = RAGFlowAPI(
            base_url=ragflow_cfg.get('base_url', ''),
            api_key=ragflow_cfg.get('api_key', ''),
            dataset_id=ragflow_cfg.get('dataset_id'),
            logger=logger
        )
        self.ragflow_enabled = bool(ragflow_cfg.get('api_key'))
    
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
                    timeout=120
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
        return "0.0"
    
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
            'max_tokens': 100,
        }
        
        try:
            resp = requests.post(
                f"{self.api_base}/chat/completions",
                headers=headers,
                json=data,
                timeout=30
            )
            
            if resp.status_code != 200:
                self.log('error', f"LLM API returned status {resp.status_code}: {resp.text[:200]}")
                return "0.0"
                
            result = resp.json()
            
            # Check for API errors in response body
            if 'error' in result:
                self.log('error', f"LLM API error: {result['error']}")
                return "0.0"
            
            if 'choices' not in result or not result['choices']:
                self.log('error', f"LLM API returned no choices: {result}")
                return "0.0"
            
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
        
        # Try OpenRouter first
        try:
            result = self.call_llm(prompt)
            if result != "0.0":
                score = float(result.replace('"', '').strip())
                return max(-1.0, min(1.0, score))
        except (ValueError, TypeError):
            pass
        
        # Fallback to Ollama
        try:
            result = self.call_llm_ollama(prompt)
            score = float(result.replace('"', '').strip())
            return max(-1.0, min(1.0, score))
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
                    'url': n.url,
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
        """Analyze sentiment and store in RAGFlow"""
        self.log('info', "Starting sentiment analysis...")
        
        news_items = self.get_unanalyzed_news()
        
        if not news_items:
            self.log('info', "No unanalyzed news found")
            return {'analyzed': 0}
        
        analyzed = 0
        stored_in_rag = 0
        
        for item in news_items:
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
                    self.log('warning', f"Failed to store news in RAG: {e}")
        
        aggregated = self.get_aggregated_sentiment()
        
        self.log('info', f"Analyzed {analyzed} news, {stored_in_rag} stored in RAG, avg sentiment: {aggregated['avg_sentiment']}")
        
        return {
            'analyzed': analyzed,
            'stored_in_rag': stored_in_rag,
            'aggregated': aggregated,
            'timestamp': datetime.utcnow().isoformat(),
        }
