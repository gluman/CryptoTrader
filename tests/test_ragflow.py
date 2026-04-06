import pytest
from unittest.mock import MagicMock, patch
from src.gateways.ragflow_api import RAGFlowAPI


class TestRAGFlowAPI:
    """Tests for RAGFlow API client"""
    
    @pytest.fixture
    def ragflow(self, mock_logger):
        """Create RAGFlowAPI instance with mocked session"""
        api = RAGFlowAPI(
            base_url='http://192.168.0.156:9380',
            api_key='test_key',
            logger=mock_logger
        )
        return api
    
    def test_init(self, ragflow):
        """RAGFlowAPI should initialize correctly"""
        assert ragflow.base_url == 'http://192.168.0.156:9380'
        assert ragflow.api_key == 'test_key'
    
    def test_headers(self, ragflow):
        """Session should have correct headers"""
        headers = ragflow.session.headers
        assert 'Authorization' in headers
        assert headers['Authorization'] == 'Bearer test_key'
    
    @patch('requests.Session.request')
    def test_list_datasets(self, mock_request, ragflow):
        """list_datasets should call correct endpoint"""
        mock_request.return_value.json.return_value = {
            'data': [{'id': '1', 'name': 'test'}]
        }
        mock_request.return_value.raise_for_status = MagicMock()
        
        result = ragflow.list_datasets()
        
        mock_request.assert_called_once()
        assert 'data' in result or isinstance(result, list)
    
    @patch('requests.Session.request')
    def test_create_dataset(self, mock_request, ragflow):
        """create_dataset should POST to datasets endpoint"""
        mock_request.return_value.json.return_value = {
            'data': {'id': 'new_id', 'name': 'test_dataset'}
        }
        mock_request.return_value.raise_for_status = MagicMock()
        
        result = ragflow.create_dataset('test_dataset', 'Test description')
        
        mock_request.assert_called_once()
        args = mock_request.call_args
        assert args[0][0] == 'POST'
        assert '/datasets' in args[0][1]
    
    @patch('requests.Session.request')
    def test_retrieve(self, mock_request, ragflow):
        """retrieve should POST to retrieval endpoint"""
        mock_request.return_value.json.return_value = {
            'data': {'chunks': [{'content': 'test chunk'}]}
        }
        mock_request.return_value.raise_for_status = MagicMock()
        
        result = ragflow.retrieve(['dataset1'], 'test query', top_k=3)
        
        mock_request.assert_called_once()
        args = mock_request.call_args
        assert '/retrieval' in args[0][1]
    
    def test_store_news(self, ragflow):
        """store_news should format content correctly"""
        with patch.object(ragflow, 'ensure_dataset', return_value='ds_id'):
            with patch.object(ragflow, 'upload_document') as mock_upload:
                mock_upload.return_value = {'status': 'ok'}
                
                ragflow.store_news(
                    title='Bitcoin surges',
                    summary='BTC hit new high',
                    source='CoinDesk',
                    url='https://example.com/news',
                    sentiment=0.8,
                )
                
                mock_upload.assert_called_once()
                call_args = mock_upload.call_args[0]
                content = call_args[1]
                
                assert 'Bitcoin surges' in content
                assert 'CoinDesk' in content
                assert '0.8' in content
    
    def test_store_expert_analysis(self, ragflow):
        """store_expert_analysis should format content correctly"""
        with patch.object(ragflow, 'ensure_dataset', return_value='ds_id'):
            with patch.object(ragflow, 'upload_document') as mock_upload:
                mock_upload.return_value = {'status': 'ok'}
                
                ragflow.store_expert_analysis(
                    symbol='BTCUSDT',
                    analysis='Strong buy signal',
                    source='Expert1',
                )
                
                mock_upload.assert_called_once()
                call_args = mock_upload.call_args[0]
                content = call_args[1]
                
                assert 'BTCUSDT' in content
                assert 'Strong buy signal' in content
    
    def test_store_trading_journal(self, ragflow):
        """store_trading_journal should format content correctly"""
        with patch.object(ragflow, 'ensure_dataset', return_value='ds_id'):
            with patch.object(ragflow, 'upload_document') as mock_upload:
                mock_upload.return_value = {'status': 'ok'}
                
                ragflow.store_trading_journal(
                    symbol='ETHUSDT',
                    action='BUY',
                    price=3500.0,
                    reasoning='CSS bullish crossover',
                )
                
                mock_upload.assert_called_once()
                call_args = mock_upload.call_args[0]
                content = call_args[1]
                
                assert 'ETHUSDT' in content
                assert 'BUY' in content
                assert '3500' in content


class TestRAGFlowIntegration:
    """Integration tests for RAGFlow"""
    
    def test_get_dataset_by_name(self, mock_logger):
        """Should find dataset by name"""
        ragflow = RAGFlowAPI('http://test', 'key', mock_logger)
        
        with patch.object(ragflow, 'list_datasets', return_value=[
            {'id': '1', 'name': 'other'},
            {'id': '2', 'name': 'cryptotrader_news'},
        ]):
            ds = ragflow.get_dataset_by_name('cryptotrader_news')
            assert ds is not None
            assert ds['id'] == '2'
    
    def test_ensure_dataset_creates(self, mock_logger):
        """Should create dataset if not exists"""
        ragflow = RAGFlowAPI('http://test', 'key', mock_logger)
        
        with patch.object(ragflow, 'get_dataset_by_name', return_value=None):
            with patch.object(ragflow, 'create_dataset', return_value={
                'data': {'id': 'new_id'}
            }):
                ds_id = ragflow.ensure_dataset('new_dataset')
                assert ds_id == 'new_id'
    
    def test_ensure_dataset_returns_existing(self, mock_logger):
        """Should return existing dataset ID"""
        ragflow = RAGFlowAPI('http://test', 'key', mock_logger)
        
        with patch.object(ragflow, 'get_dataset_by_name', return_value={
            'id': 'existing_id', 'name': 'test'
        }):
            ds_id = ragflow.ensure_dataset('test')
            assert ds_id == 'existing_id'
