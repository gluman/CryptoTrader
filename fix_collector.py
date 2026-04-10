#!/usr/bin/env python3
import re
import sys
sys.path.insert(0, '.')

with open('src/agents/data_collector.py', 'r') as f:
    content = f.read()

old_method = '''    def select_symbols(self) -> List[str]:
        """Select symbols based on volume and change criteria"""
        criteria = self.config.selection_criteria
        min_volume = criteria.get('min_volume_24h', 50_000_000)
        min_change = criteria.get('min_change_1h', 2.0)
        quote = criteria.get('quote_currency', 'USDT')
        
        tickers = self.fetch_binance_tickers()
        selected = []
        
        for t in tickers:
            symbol = t.get('symbol', '')
            if not symbol.endswith(quote):
                continue
            
            try:
                volume = float(t.get('quoteVolume', 0))
                change = abs(float(t.get('priceChangePercent', 0)))
                
                if volume >= min_volume and change >= min_change:
                    selected.append(symbol)
            except (ValueError, TypeError):
                continue
        
        self.log('info', f"Selected {len(selected)} symbols from {len(tickers)} tickers")
        
        # Save to DB
        with self.db.get_session() as session:
            from ..core.database import SelectedSymbol
            # Deactivate old selections
            session.query(SelectedSymbol).update({'is_active': False})
            
            for sym in selected:
                exists = session.query(SelectedSymbol).filter_by(symbol=sym).first()
                if exists:
                    exists.is_active = True
                    exists.selected_at = datetime.utcnow()
                else:
                    session.add(SelectedSymbol(symbol=sym, exchange='binance', is_active=True))
        
        return selected'''

new_method = '''    def select_symbols(self) -> List[str]:
        criteria = self.config.selection_criteria
        min_volume = criteria.get('min_volume_24h', 50_000_000)
        min_change = criteria.get('min_change_1h', 2.0)
        quote = criteria.get('quote_currency', 'USDT')
        
        tickers = self.fetch_binance_tickers()
        selected = []
        
        for t in tickers[:500]:
            symbol = t.get('symbol', '')
            if not symbol.endswith(quote):
                continue
            try:
                volume = float(t.get('quoteVolume', 0))
                change = abs(float(t.get('priceChangePercent', 0)))
                if volume >= min_volume and change >= min_change:
                    selected.append(symbol)
            except (ValueError, TypeError):
                continue
            if len(selected) >= 15:
                break
        
        self.log('info', f"Selected {len(selected)} symbols from {len(tickers)} tickers")
        return selected[:15]'''

if old_method in content:
    content = content.replace(old_method, new_method)
    with open('src/agents/data_collector.py', 'w') as f:
        f.write(content)
    print('Updated successfully')
else:
    print('Old method not found, checking if already updated...')
    if 'tickers[:500]' in content:
        print('Already updated!')
    else:
        print('ERROR: Unknown state')