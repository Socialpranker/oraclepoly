import { cn } from "@/lib/utils"

/**
 * SemanticSkeleton — умный скелетон с контекстным неоновым пульсом.
 *
 * В отличие от обычного скелетона, показывает:
 * - Trend-зависимый неоновый контур (up/down/neutral)
 * - Микро-текст о текущем действии (напр. "Syncing Polymarket liquidity...")
 * - CSS shimmer-анимацию поверх glass-основы
 *
 * Варианты: card, text, chart, avatar.
 */

type SkeletonVariant = "card" | "text" | "chart" | "avatar"
type SkeletonTrend = "up" | "down" | "neutral"

interface SemanticSkeletonProps {
  /** Тип скелетона — определяет размеры */
  variant: SkeletonVariant
  /** Направление тренда — определяет цвет неонового контура */
  trend: SkeletonTrend
  /** Микро-текст внутри (описание текущей операции) */
  message?: string
  /** Дополнительные CSS классы */
  className?: string
}

/** Маппинг тренда на неоновый цвет */
const TREND_COLORS: Record<SkeletonTrend, string> = {
  up: "#00FF85",
  down: "#FF3B30",
  neutral: "#273140",
}

/** Маппинг варианта на CSS классы размеров */
const VARIANT_CLASSES: Record<SkeletonVariant, string> = {
  card: "h-24 w-full rounded-2xl",
  text: "h-4 w-full rounded",
  chart: "h-48 w-full rounded-2xl",
  avatar: "h-10 w-10 rounded-full",
}

export function SemanticSkeleton({
  variant,
  trend,
  message,
  className,
}: SemanticSkeletonProps) {
  const trendColor = TREND_COLORS[trend]
  const variantClass = VARIANT_CLASSES[variant]

  return (
    <div
      className={cn(
        "relative overflow-hidden",
        "bg-[rgba(21,26,33,0.6)]",
        variantClass,
        className
      )}
      style={{
        border: `1px solid ${trendColor}`,
        ["--skeleton-color" as string]: trendColor,
        animation: "skeleton-semantic-pulse 2s ease-in-out infinite",
      }}
    >
      {/* Shimmer-слой */}
      <div className="skeleton-shimmer absolute inset-0" />

      {/* Микро-текст (для card и chart вариантов) */}
      {message && variant !== "text" && variant !== "avatar" && (
        <div className="absolute inset-0 flex items-center justify-center">
          <span
            className="font-mono text-[10px] tracking-wider"
            style={{ color: `${trendColor}80` }}
          >
            {message}
          </span>
        </div>
      )}
    </div>
  )
}
