import type { ReactNode } from 'react';

type BadgeVariant = 'safe' | 'danger' | 'warning' | 'info' | 'accent' | 'alpha' | 'echo';

interface BadgeProps {
    children: ReactNode;
    variant?: BadgeVariant;
    className?: string;
}

const variantStyles: Record<BadgeVariant, string> = {
    safe: 'bg-[var(--color-safe)]/20 text-[var(--color-safe)] border-[var(--color-safe)]/30',
    danger: 'bg-[var(--color-danger)]/20 text-[var(--color-danger)] border-[var(--color-danger)]/30',
    warning: 'bg-[var(--color-warning)]/20 text-[var(--color-warning)] border-[var(--color-warning)]/30',
    info: 'bg-[var(--color-info)]/20 text-[var(--color-info)] border-[var(--color-info)]/30',
    accent: 'bg-[var(--color-accent)]/20 text-[var(--color-accent)] border-[var(--color-accent)]/30',
    // News signal badges
    alpha: 'bg-[var(--color-safe)]/30 text-[var(--color-safe)] border-[var(--color-safe)]/50 font-bold animate-pulse',
    echo: 'bg-[var(--color-text-muted)]/20 text-[var(--color-text-muted)] border-[var(--color-text-muted)]/30 opacity-75',
};

export function Badge({
    children,
    variant = 'info',
    className = ''
}: BadgeProps) {
    return (
        <span
            className={`
        inline-flex items-center
        px-2 py-1
        text-xs font-medium
        rounded-md
        border
        ${variantStyles[variant]}
        ${className}
      `}
        >
            {children}
        </span>
    );
}
