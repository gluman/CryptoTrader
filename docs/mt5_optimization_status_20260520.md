================================================================================
MT5 OPTIMIZATION — LAUNCH LOG
Date: 2026-05-20 01:05 MSK
EA: CCFp GA-Expert 2.3.0
Symbol: EURUSD H1
Period: Nov 2025 — May 2026 (6 months)
Mode: Optimization=0 (single backtest first)
Criterion: Balance max (criterion=0)

================================================================================
## INI CONFIG: tester_optimize_6m.ini

[Tester]
Expert=CCFp GA-Expert 2.3.0
Symbol=EURUSD
Period=H1
ExecutionMode=0
Optimization=0
OptimizationCriterion=0
Model=8 (every tick)
FromDate=2025.11.01
ToDate=2026.05.20
ForwardMode=0
Deposit=10000
Currency=USD
ShutdownTerminal=1
Report=optim_6m_report
Replace=Yes
UseLocal=1
UseRemote=0
UseCloud=0
Visual=0
ExpertParameters=CCFp_optim.set
Login=64805429

================================================================================
## EA PARAMETERS (CCFp_optim.set)

NumSignal=1||1||1||3||Y
level_trade=0.2||0.10||0.05||0.40||Y
TrendMAPeriod=50||20||30||200||Y
RiskPercent=2.0||0.5||0.5||3.0||Y
MinStepNL=1000||500||500||2000||Y
TP=1000||500||500||2000||Y
ATRMultiplier=2.0||1.0||0.5||3.0||Y

================================================================================
## KNOWN ISSUES / PITFALLS

1. EA lacks OnTester() — Criterion=7 (Custom max) WILL NOT WORK. Using criterion=0 (Balance max)
2. Demo account 64805429 may be deleted by broker at any time
3. Terminal must connect to account before optimization can start
4. After single backtest (Optimization=0), switch to Optimization=1 for full optimization
5. Agents must be restarted after terminal restart (passwords change)

================================================================================
## CURRENT STATUS (01:05)

Terminal launched with single-test mode first to verify:
- Account connection works
- EA loads and runs
- Historical data is available

After confirmation:
- Kill terminal
- Change Optimization=1
- Restart agents with fresh passwords
- Relaunch terminal for full 6-month optimization (2068 passes)

================================================================================
## AGENT STARTUP COMMAND

for port in 3000 3001 3002 3003 3004 3005 3006 3007; do
  mt5-tester /run /address:127.0.0.1:$port > /home/andy/projects/MT5Robot/data/agent_$port.log 2>&1 &
done

FIND PASSWORDS:
iconv -f UTF-16LE -t UTF-8 Agent-127.0.0.1-3000/logs/*.log | grep password

================================================================================
## NEXT STEPS FOR CLAUDE (at 5:30)

1. Read /home/andy/cryptotrader/docs/mt5_optimization_status_20260520.md
2. Check if single test completed (look for .opt file in cache/)
3. If yes: switch Optimization=0→1, restart terminal
4. If no: diagnose issue (account? data? EA?)
5. After optimization runs: analyze results, find profitable parameter sets
6. For profitable sets: run forward test, verify stability

================================================================================