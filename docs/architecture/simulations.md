# Симуляции — архитектура

## Ключевые файлы

| Компонент | Файл |
|-----------|------|
| Simulation service | `app/services/simulation_service.py` |
| Whale copy service | `app/services/whale_copy_service.py` |
| Risk engine | `app/services/risk_engine.py` |
| Fee calculator | `app/services/fee_calculator.py` |
| Trade simulator (handler) | `app/handlers/trade_simulator.py` |
| Daily snapshots (job) | `app/jobs/daily_simulation_snapshot.py` |

## Режимы симуляции

- **Mode C** (Conservative): Kelly fraction × 0.25
- **Mode D** (Default): Kelly fraction × 0.5
- **Mode E** (Enhanced): Kelly fraction × 1.0
- **Mode L** (Longshot): Фокус на событиях с низкой вероятностью, повышенный risk/reward

## Risk Engine

- Position limits — максимальный размер позиции
- Drawdown stops — остановка при превышении просадки
- Exposure limits — контроль общей экспозиции

## Fee Profiles

- **Direct**: прямые комиссии
- **Global**: глобальные комиссии
- **Free**: без комиссий (для бэктестов)

## Frontend

- API: `GET /api/analytics/simulation`
- Hook: `client/src/hooks/useTradeSimulator.ts`
- UI: `client/src/components/market/TradeSimulator.tsx`
- Admin tab: `client/src/components/analytics/OracleSimulationTab.tsx`
