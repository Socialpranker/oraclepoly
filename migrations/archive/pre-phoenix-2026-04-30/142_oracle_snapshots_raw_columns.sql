-- 142: Добавить raw contributions в oracle_snapshots для grid search калибровки
-- whale_contribution_raw и news_contribution_raw — суммы ДО взвешивания,
-- z_posterior — log-odds posterior для temperature scaling

ALTER TABLE public.oracle_snapshots ADD COLUMN IF NOT EXISTS whale_contribution_raw NUMERIC(10,6);
ALTER TABLE public.oracle_snapshots ADD COLUMN IF NOT EXISTS news_contribution_raw NUMERIC(10,6);
ALTER TABLE public.oracle_snapshots ADD COLUMN IF NOT EXISTS z_posterior NUMERIC(10,6);
