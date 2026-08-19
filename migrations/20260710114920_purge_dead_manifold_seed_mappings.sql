-- 20260710114920_purge_dead_manifold_seed_mappings.sql
-- Description: удалить 8 мёртвых manifold seed-записей из venue_market_mappings.
--
-- Мотивация (диагностика research/manifold-mapper-diagnostics-2026-07-09/answer.md, §2, рек. 4):
-- 8 manual-записей от 2026-05-20 15:53:41 содержат вымышленные venue_market_id
-- (fed-march-2026 и т.п.), которых нет и никогда не было в manifold_markets
-- (Manifold использует короткие random id вида yhZZZzZgh2, не slug'и).
-- Для manifold_bets_scrape.py эти строки мертвы (JOIN с manifold_markets даёт
-- 0 строк) и лишь искажают счётчик покрытия D50 (10 → реально живых 3).
--
-- Guard NOT EXISTS — на случай, если рынок с таким id внезапно появится
-- в manifold_markets, запись не трогаем. Живые embedding-записи не задеты:
-- фильтр по matched_by='manual' + явный список из диагностики.

DELETE FROM venue_market_mappings vm
 WHERE vm.venue = 'manifold'
   AND vm.matched_by = 'manual'
   AND vm.venue_market_id IN (
        'fed-march-2026',
        'btc-100k-eoy-2026',
        'eth-5k-eoy-2026',
        'trump-leaves-2026',
        'russia-ukraine-ceasefire-2026',
        'oscars-2026',
        'gpt5-2026',
        'anthropic-ipo-2026'
   )
   AND NOT EXISTS (
        SELECT 1
          FROM manifold_markets mm
         WHERE mm.id = vm.venue_market_id
   );
