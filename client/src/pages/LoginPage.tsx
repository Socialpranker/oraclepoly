import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '../hooks/useAuth';

interface TelegramUser {
  id: number;
  first_name: string;
  username?: string;
  photo_url?: string;
  auth_date: number;
  hash: string;
}

declare global {
  interface Window {
    onTelegramAuth?: (user: TelegramUser) => void;
  }
}

export function LoginPage() {
  const navigate = useNavigate();
  const { refetch } = useAuth();
  const containerRef = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    const botUsername = import.meta.env.VITE_TELEGRAM_BOT_USERNAME;
    if (!botUsername) {
      setError('VITE_TELEGRAM_BOT_USERNAME не задан');
      return;
    }

    window.onTelegramAuth = async (user: TelegramUser) => {
      setLoading(true);
      setError(null);
      try {
        const res = await fetch('/api/auth/callback', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            id: String(user.id),
            first_name: user.first_name,
            username: user.username ?? '',
            photo_url: user.photo_url ?? '',
            auth_date: String(user.auth_date),
            hash: user.hash,
          }),
          credentials: 'include',
        });

        if (!res.ok) {
          const err = await res.json().catch(() => ({ detail: 'Ошибка сервера' }));
          setError(err.detail ?? 'Ошибка авторизации');
          return;
        }

        await refetch();
        navigate('/bot', { replace: true });
      } catch {
        setError('Сетевая ошибка');
      } finally {
        setLoading(false);
      }
    };

    const script = document.createElement('script');
    script.src = 'https://telegram.org/js/telegram-widget.js?22';
    script.setAttribute('data-telegram-login', botUsername);
    script.setAttribute('data-size', 'large');
    script.setAttribute('data-onauth', 'onTelegramAuth(user)');
    script.setAttribute('data-request-access', 'write');
    script.async = true;

    const container = containerRef.current;
    container?.appendChild(script);

    return () => {
      delete window.onTelegramAuth;
      if (container && script.parentNode === container) {
        container.removeChild(script);
      }
    };
  }, [navigate, refetch]);

  return (
    <div className="min-h-screen bg-[#0c0c0c] flex flex-col items-center justify-center gap-8 p-6">
      <div className="text-center">
        <h1 className="text-2xl font-bold text-white mb-2">OraclePoly</h1>
        <p className="text-[#888] text-sm">Войдите через Telegram для доступа</p>
      </div>

      <div className="bg-[#1a1a1a] rounded-2xl p-8 flex flex-col items-center gap-4 min-w-[280px]">
        {loading ? (
          <div className="text-[#00FF88] text-sm">Авторизация...</div>
        ) : (
          <div ref={containerRef} />
        )}

        {error && (
          <p className="text-red-400 text-sm text-center">{error}</p>
        )}
      </div>
    </div>
  );
}
