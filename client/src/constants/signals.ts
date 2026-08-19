import { TrendingUp, TrendingDown, Minus } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';

export type SignalType = 'underpriced' | 'overpriced' | 'fair';

export interface SignalConfig {
    label: string;
    icon: LucideIcon;
    /** Tailwind color class (CSS-переменные темы) */
    color: string;
}

/**
 * Базовый конфиг сигнала дивергенции.
 * Использует CSS-переменные темы (--color-safe, --color-danger, --color-warning).
 * Применяется в: PredictionCard, DecisionSummary.
 */
export const SIGNAL_CONFIG: Record<SignalType, SignalConfig> = {
    underpriced: {
        label: 'UNDERPRICED',
        icon: TrendingUp,
        color: 'text-[var(--color-safe)]',
    },
    overpriced: {
        label: 'OVERPRICED',
        icon: TrendingDown,
        color: 'text-[var(--color-danger)]',
    },
    fair: {
        label: 'FAIR VALUE',
        icon: Minus,
        color: 'text-[var(--color-warning)]',
    },
};

