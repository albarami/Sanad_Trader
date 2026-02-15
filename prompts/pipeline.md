# Trading Pipeline — Signal Processing Orchestration

When a trading signal arrives, process it through these stages IN ORDER. Do not skip any stage.

## Stage 1: Signal Intake

Log the raw signal with timestamp, source, token, and claim. Save to trading/signals/YYYY-MM-DD.md.

## Stage 2: Sanad Verification

Read trading/prompts/sanad-verifier.md. Apply that role. Analyze the signal using the Sanad Trust Framework.

If sanad_trust_score < 70: STOP. Log rejection to trading/sanad-rejected/ with reason. Pipeline ends.

## Stage 3: Strategy Match

Check trading/strategies/ for a matching strategy. If no strategy matches, log as NO_STRATEGY_MATCH and stop.

If match found, calculate position size using Kelly Criterion (or default 2% if <30 trades). Read thresholds from trading/config/thresholds.yaml.

## Stage 4: Bull/Bear Debate

Read trading/prompts/bull-albaqarah.md. Argue FOR the trade with full conviction.

Then read trading/prompts/bear-aldahhak.md. Argue AGAINST the trade with full conviction.

Present both arguments in structured format.

## Stage 5: Al-Muhasbi Judge

Switch to GPT model. Read trading/prompts/judge-almuhasbi.md.

Present the complete DecisionPacket: signal, Sanad verification, strategy match, Kelly sizing, Bull argument, Bear argument.

Al-Muhasbi issues APPROVE, REJECT, or REVISE.

## Stage 6: Policy Engine Check

Verify against ALL gates in thresholds.yaml: position size, exposure, daily loss, drawdown. Check circuit breaker states.

This is a deterministic check — if ANY gate fails, BLOCK.

## Stage 7: Execute or Log

If APPROVED by Al-Muhasbi AND Policy Engine PASS: execute the trade.

If REJECTED: log the full DecisionPacket to trading/execution-logs/ with rejection reason.

Always sync the event to Supabase.

## CRITICAL RULES

- NEVER skip the Bear argument to trade faster
- NEVER override Al-Muhasbi's REJECT
- If in doubt at ANY stage, STOP and log
- Every signal gets a correlation_id (UUID) that follows it through every stage
