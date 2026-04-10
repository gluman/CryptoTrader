    def select_symbols(self) -> List[str]:
        """Select symbols based on volume and change criteria"""
        criteria = self.config.selection_criteria
        min_volume = criteria.get('min_volume_24h', 50_000_000)
        min_change = criteria.get('min_change_1h', 2.0)
        quote = criteria.get('quote_currency', 'USDT')
        
        tickers = self.fetch_binance_tickers()
        selected = []
        
        # Limit to first 500 tickers for speed
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
            
            # Limit selection to 15 symbols
            if len(selected) >= 15:
                break
        
        self.log('info', f"Selected {len(selected)} symbols from {len(tickers)} tickers")
        
        return selected[:15]