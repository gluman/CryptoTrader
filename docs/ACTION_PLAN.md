# CryptoTrader — Action Plan for Executor Agent
# Created: 2026-05-12

## IMMEDIATE FIXES (1-2 hours total)

### Fix 1: BUG-01 — Enable LLM sentiment via Ollama
File: cryptotrader/src/agents/sentiment_agent.py
Method: run_once()
Change: replace `score = self.analyze_sentiment(...)` with Ollama LLM call
Details: see IMPROVEMENTS.md BUG-01

### Fix 2: BUG-02 — Scalping SL threshold
File: cryptotrader/src/agents/trading_agent.py  
Method: _validate_sl_tp()
Change: accept market_type param, use 0.3%/0.6% SL/TP for linear market type
Details: see IMPROVEMENTS.md BUG-02

### Fix 3: BUG-04 — Ollama in fallback chain
File: cryptotrader/src/agents/trading_agent.py
Method: call_llm()
Change: add _call_ollama() between DeepSeek and rule_based
Details: see IMPROVEMENTS.md BUG-04

### Fix 4: BUG-03 — Remove dead code
File: cryptotrader/src/agents/trading_agent.py
Lines: 622-627 (duplicate return block after line 619)
Change: delete lines 622-627

### Fix 5: ARCH-06 — Correct model_version in save_decision
File: cryptotrader/src/agents/trading_agent.py
Line: ~651
Change: model_version=decision.get("source", self.model)

### Fix 6: ARCH-04 — Signal deduplication
File: cryptotrader/src/agents/trading_agent.py
Method: save_decision()
Change: skip if existing PENDING signal for same symbol+direction exists

## STATUS
- [ ] Fix 1 applied
- [ ] Fix 2 applied  
- [ ] Fix 3 applied
- [ ] Fix 4 applied
- [ ] Fix 5 applied
- [ ] Fix 6 applied
- [ ] Tests run: python main.py --task decide --symbols BTCUSDT ETHUSDT
