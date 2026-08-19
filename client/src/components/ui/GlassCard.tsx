import React from 'react';
import type { ReactNode, KeyboardEvent } from 'react';

/**
 * Depth levels for visual hierarchy:
 * - hero: Highest prominence (Oracle Score, main metrics)
 * - primary: Standard cards (Whale cards, Risk cards)
 * - nested: Inside other cards (Details, sub-sections)
 */
type GlassDepth = 'hero' | 'primary' | 'nested';

interface GlassCardProps {
    children: ReactNode;
    className?: string;
    style?: React.CSSProperties;
    onClick?: () => void;
    hover?: boolean;
    /** Visual depth level for hierarchy. Default: 'primary' */
    depth?: GlassDepth;
    /** Enable glow effect on hover */
    glow?: 'safe' | 'danger' | 'warning' | 'accent' | 'info' | false;
}

const depthStyles: Record<GlassDepth, { bg: string; hoverBg: string }> = {
    hero: {
        bg: 'bg-[rgba(21,26,33,0.8)]',
        hoverBg: 'hover:bg-[rgba(21,26,33,0.9)]',
    },
    primary: {
        bg: 'bg-[rgba(21,26,33,0.6)]',
        hoverBg: 'hover:bg-[rgba(21,26,33,0.7)]',
    },
    nested: {
        bg: 'bg-[rgba(21,26,33,0.3)]',
        hoverBg: 'hover:bg-[rgba(21,26,33,0.4)]',
    },
};

const glowStyles: Record<string, string> = {
    safe: 'hover:shadow-[0_0_20px_rgba(0,255,133,0.15)]',
    danger: 'hover:shadow-[0_0_20px_rgba(255,59,48,0.15)]',
    warning: 'hover:shadow-[0_0_20px_rgba(255,106,61,0.15)]',
    accent: 'hover:shadow-[0_0_20px_rgba(158,122,255,0.15)]',
    info: 'hover:shadow-[0_0_20px_rgba(64,224,255,0.15)]',
};

export function GlassCard({
    children,
    className = '',
    style,
    onClick,
    hover = true,
    depth = 'primary',
    glow = false,
}: GlassCardProps) {
    const { bg, hoverBg } = depthStyles[depth];

    const baseClasses = `
        ${bg}
        backdrop-blur-xl
        border border-[var(--color-border-default)]
        rounded-2xl
        p-4
        transition-all duration-200
    `;

    const hoverClasses = hover
        ? `${hoverBg} hover:border-[var(--color-border-default)] cursor-pointer`
        : '';

    const glowClass = glow && hover ? glowStyles[glow] : '';

    return (
        <div
            className={`${baseClasses} ${hoverClasses} ${glowClass} ${className}`}
            style={style}
            onClick={onClick}
            {...(onClick ? {
                role: 'button',
                tabIndex: 0,
                onKeyDown: (e: KeyboardEvent) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onClick(); } },
            } : {})}
        >
            {children}
        </div>
    );
}
