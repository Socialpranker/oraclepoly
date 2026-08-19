import { useEffect, useState } from 'react';

/**
 * useIsMobile — single-source-of-truth для responsive layout switches
 * в lifecycle компонентах. См. plans/wiggly-nibbling-stardust.md (PR 2).
 *
 * Default breakpoint 700px — между tablet и mobile. Для более узких лейаутов
 * (table → card) используй 600. Не SSR-safe (window required).
 */
export function useIsMobile(breakpoint = 700): boolean {
  const [mobile, setMobile] = useState(() =>
    typeof window !== 'undefined' ? window.innerWidth < breakpoint : false,
  );

  useEffect(() => {
    if (typeof window === 'undefined') return;
    const onResize = () => setMobile(window.innerWidth < breakpoint);
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, [breakpoint]);

  return mobile;
}

/** Возвращает [isMobile<600, isCompact<700] — для двухуровневой адаптации. */
export function useViewportTier(): { isNarrow: boolean; isCompact: boolean } {
  const [tier, setTier] = useState(() => {
    const w = typeof window !== 'undefined' ? window.innerWidth : 1200;
    return { isNarrow: w < 600, isCompact: w < 700 };
  });

  useEffect(() => {
    if (typeof window === 'undefined') return;
    const onResize = () => {
      const w = window.innerWidth;
      setTier({ isNarrow: w < 600, isCompact: w < 700 });
    };
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);

  return tier;
}
