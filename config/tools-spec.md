# TOOLS.md — Sprint 10.4.6

## External APIs

| Tool | Purpose | Auth | Rate Limit |
|------|---------|------|------------|
| Binance | CEX execution, prices | API key + secret | 1200/min |
| Helius | Solana RPC, DAS, WebSocket | API key | 50 RPS |
| Jupiter | DEX routing, quotes, swaps | None | 10 RPS |
| CoinGecko | Market data, discovery | API key | 30/min |
| DexScreener | DEX pair data | None | 60/min |
| Perplexity | Real-time research | API key | 50/min |
| Anthropic | Claude Opus/Sonnet | API key | 60/min |
| OpenAI | GPT-5.2 | API key | 60/min |
| Telegram Bot | Notifications | Bot token | 30/sec |

## Internal Tools

| Script | Purpose | Frequency |
|--------|---------|-----------| 
| heartbeat.py | System health check | Every 10 min |
| price_snapshot.py | Price data collection | Every 3 min |
| reconciliation.py | Balance verification | Hourly |
| onchain_analytics.py | On-chain monitoring | Every 15 min |
| social_sentiment.py | Social sentiment scan | Every 15 min |
| daily_report.py | Performance summary | Daily 23:00 QAT |
| weekly_analysis.py | Deep analysis suite | Sunday 06:00 QAT |
| weekly_research.py | Macro crypto research | Sunday 08:00 QAT |
| security_audit.py | Security scan | Friday 22:00 QAT |
| github_backup.py | State backup | Every 6 hours |
| red_team.py | Attack simulation | Saturday 23:00 QAT |
| model_check.py | Model availability | Monday 06:00 QAT |
| dust_sweeper.py | Dust conversion | Sunday 04:00 QAT |

## Circuit Breakers

All external API clients wrapped with circuit breaker:
- CLOSED → OPEN after 3 consecutive failures
- OPEN → HALF_OPEN after 60s cooldown
- HALF_OPEN → CLOSED on success, OPEN on failure
- Tracked in state/{api}_circuit_breaker.json
