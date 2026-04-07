import requests
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime


class RAGFlowAPI:
    """RAGFlow API client for storing and retrieving expert knowledge and news"""
    
    # Single dataset ID for all crypto trading data
    DEFAULT_DATASET_NAME = 'cryptotrader'
    
    def __init__(self, base_url: str, api_key: str, dataset_id: Optional[str] = None, logger: Optional[logging.Logger] = None):
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        self.dataset_id = dataset_id  # Single dataset for all data
        self.logger = logger or logging.getLogger(__name__)
        self.session = requests.Session()
        self.session.headers.update({
            'Authorization': f'Bearer {api_key}',
        })
        self.logger.info(f"RAGFlow API initialized with base_url={self.base_url}, dataset_id={self.dataset_id or 'not set'}")
        # Note: Content-Type is NOT set here - it's handled automatically for multipart uploads
    
    def _request(self, method: str, endpoint: str, **kwargs) -> Dict:
        """Make API request"""
        url = f"{self.base_url}{endpoint}"
        try:
            resp = self.session.request(method, url, timeout=30, **kwargs)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            self.logger.error(f"RAGFlow API error: {e}")
            return {'error': str(e)}
    
    # ==================== Datasets ====================
    
    def list_datasets(self) -> List[Dict]:
        """List all datasets"""
        result = self._request('GET', '/api/v1/datasets')
        return result.get('data', [])
    
    def create_dataset(self, name: str, description: str = '') -> Dict:
        """Create a new dataset"""
        data = {'name': name, 'description': description}
        return self._request('POST', '/api/v1/datasets', json=data)
    
    def get_dataset_by_name(self, name: str) -> Optional[Dict]:
        """Get dataset by name"""
        datasets = self.list_datasets()
        for ds in datasets:
            if ds.get('name') == name:
                return ds
        return None
    
    def ensure_dataset(self, name: str, description: str = '') -> str:
        """Get or create dataset and return its ID"""
        ds = self.get_dataset_by_name(name)
        if ds:
            return ds['id']
        
        result = self.create_dataset(name, description)
        if 'data' in result:
            return result['data']['id']
        return ''
    
    # ==================== Documents ====================
    
    def upload_document(self, dataset_id: str, content: str, 
                        filename: str, metadata: Optional[Dict] = None) -> Dict:
        """Upload a document to a dataset"""
        url = f"{self.base_url}/api/v1/datasets/{dataset_id}/documents"
        
        try:
            # RAGFlow expects 'file' as the key - don't pass 'data' param or it fails
            files = {
                'file': (filename, content.encode('utf-8'), 'text/plain')
            }
            
            resp = self.session.post(url, files=files, timeout=60)
            return resp.json()
        except Exception as e:
            return {'error': str(e)}
    
    def list_documents(self, dataset_id: str) -> List[Dict]:
        """List documents in a dataset"""
        result = self._request('GET', f'/api/v1/datasets/{dataset_id}/documents')
        return result.get('data', [])
    
    def delete_document(self, dataset_id: str, document_id: str) -> Dict:
        """Delete a document"""
        return self._request('DELETE', f'/api/v1/datasets/{dataset_id}/documents/{document_id}')
    
    # ==================== Retrieval (RAG) ====================
    
    def retrieve(self, dataset_ids: List[str], query: str, 
                 top_k: int = 5, similarity_threshold: float = 0.5) -> List[Dict]:
        """Retrieve relevant documents for a query"""
        data = {
            'question': query,
            'dataset_ids': dataset_ids,
            'top_k': top_k,
            'similarity_threshold': similarity_threshold,
        }
        result = self._request('POST', '/api/v1/retrieval', json=data)
        return result.get('data', {}).get('chunks', [])
    
    def _get_dataset_id(self) -> str:
        """Get the dataset ID - from instance or by name lookup"""
        if self.dataset_id:
            return self.dataset_id
        # Fallback: lookup by name
        ds = self.get_dataset_by_name(self.DEFAULT_DATASET_NAME)
        if ds:
            self.dataset_id = ds['id']
            return self.dataset_id
        return ''
    
    # ==================== Convenience Methods for CryptoTrader ====================
    
    def store_news(self, title: str, summary: str, source: str, 
                   url: str, sentiment: Optional[float] = None) -> Dict:
        """Store news article in RAGFlow (single dataset)"""
        ds_id = self._get_dataset_id()
        if not ds_id:
            return {'error': 'Dataset not found'}
        
        content = f"""Title: {title}
Source: {source}
URL: {url}
Sentiment: {sentiment if sentiment is not None else 'N/A'}
Date: {datetime.utcnow().isoformat()}

Summary:
{summary}"""
        
        filename = f"news_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{hash(title) % 10000}.txt"
        return self.upload_document(ds_id, content, filename, {
            'type': 'news',
            'source': source,
            'sentiment': sentiment,
        })
    
    def store_expert_analysis(self, symbol: str, analysis: str, 
                              source: str = 'manual') -> Dict:
        """Store expert analysis for a trading pair (single dataset)"""
        ds_id = self._get_dataset_id()
        if not ds_id:
            return {'error': 'Dataset not found'}
        
        content = f"""Symbol: {symbol}
Source: {source}
Date: {datetime.utcnow().isoformat()}

Analysis:
{analysis}"""
        
        filename = f"expert_{symbol}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.txt"
        return self.upload_document(ds_id, content, filename, {
            'type': 'expert_analysis',
            'symbol': symbol,
        })
    
    def store_strategy_note(self, title: str, content: str) -> Dict:
        """Store strategy notes and rules (single dataset)"""
        ds_id = self._get_dataset_id()
        if not ds_id:
            return {'error': 'Dataset not found'}
        
        doc_content = f"""Strategy: {title}
Date: {datetime.utcnow().isoformat()}

{content}"""
        
        filename = f"strategy_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.txt"
        return self.upload_document(ds_id, content, filename, {
            'type': 'strategy',
        })
    
    def store_trading_journal(self, symbol: str, action: str, price: float,
                              reasoning: str, result: Optional[str] = None,
                              pnl: Optional[float] = None) -> Dict:
        """Store trading journal entry (single dataset)"""
        ds_id = self._get_dataset_id()
        if not ds_id:
            return {'error': 'Dataset not found'}
        
        content = f"""Symbol: {symbol}
Action: {action}
Price: ${price:.4f}
Date: {datetime.utcnow().isoformat()}
Result: {result or 'PENDING'}
PnL: {pnl if pnl is not None else 'N/A'}

Reasoning:
{reasoning}"""
        
        filename = f"journal_{symbol}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.txt"
        return self.upload_document(ds_id, content, filename, {
            'type': 'journal',
            'symbol': symbol,
            'action': action,
        })
    
    def get_trading_context(self, symbol: str, query: str, 
                            top_k: int = 5) -> str:
        """Get relevant context for trading decision from single dataset"""
        ds_id = self._get_dataset_id()
        if not ds_id:
            return ''
        
        # Search in single dataset with symbol-specific query
        full_query = f"{symbol} {query}"
        chunks = self.retrieve([ds_id], full_query, top_k=top_k)
        
        context_parts = []
        for chunk in chunks:
            content = chunk.get('content', '').strip()
            if content:
                doc_name = chunk.get('document_name', 'unknown')
                context_parts.append(f"[{doc_name}] {content[:500]}")
        
        return '\n---\n'.join(context_parts[:top_k * 2])
    
    def get_market_sentiment_context(self, symbol: str) -> str:
        """Get sentiment-related context for a symbol"""
        return self.get_trading_context(
            symbol, 
            f"market sentiment news bullish bearish trend {symbol}"
        )
    
    def get_expert_opinion(self, symbol: str) -> str:
        """Get expert analysis for a symbol"""
        return self.get_trading_context(
            symbol,
            f"expert analysis recommendation buy sell hold {symbol}"
        )
