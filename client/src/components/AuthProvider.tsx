import { useCallback, useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { AuthContext } from '../hooks/useAuth';
import type { User } from '../hooks/useAuth';

export function AuthProvider({ children }: { children: ReactNode }) {
    const [user, setUser] = useState<User | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);

    const fetchAuth = useCallback(async () => {
        const tg = window.Telegram?.WebApp;

        // Сценарий 1: Telegram Mini App — initData есть, парсим user из него.
        // Реальная верификация HMAC происходит на бэке при каждом API-запросе
        // (через x-telegram-init-data header). Здесь только локальная инициализация.
        if (tg?.initData && tg.initDataUnsafe?.user) {
            const tgUser = tg.initDataUnsafe.user;
            setUser({
                telegram_id: tgUser.id,
                username: tgUser.username,
                display_name: [tgUser.first_name, tgUser.last_name].filter(Boolean).join(' '),
                avatar_url: tgUser.photo_url,
                level: 0,
                xp: 0,
                total_xp: 0,
                credits: 0,
                daily_streak: 0,
                stats: { totalInsights: 0, accuracyScore: 0, lockedMarkets: 0, globalRank: 0 },
                topic_mastery: [],
                achievements: [],
                recent_activity: [],
            } as User);
            setLoading(false);
            return;
        }

        // Сценарий 2: Web — нет initData, пробуем JWT cookie через /api/auth/me.
        try {
            const response = await fetch('/api/auth/me', { credentials: 'include' });
            if (response.ok) {
                const data = await response.json();
                setUser({
                    telegram_id: data.telegram_id,
                    is_admin: data.is_admin,
                    level: 0,
                    xp: 0,
                    total_xp: 0,
                    credits: 0,
                    daily_streak: 0,
                    stats: { totalInsights: 0, accuracyScore: 0, lockedMarkets: 0, globalRank: 0 },
                    topic_mastery: [],
                    achievements: [],
                    recent_activity: [],
                } as User);
            } else {
                setUser(null);
            }
        } catch (err) {
            console.error('Cookie auth failed', err);
            setError('Ошибка сети');
            setUser(null);
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => {
        fetchAuth();
    }, [fetchAuth]);

    return (
        <AuthContext.Provider value={{ user, loading, error, refetch: fetchAuth }}>
            {children}
        </AuthContext.Provider>
    );
}
