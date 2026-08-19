/**
 * Утилиты для работы с Telegram WebApp.
 *
 * Централизованное место для получения initData и формирования заголовков.
 * Используется во всех хуках и компонентах, взаимодействующих с API через
 * Telegram Mini App аутентификацию.
 */

/**
 * Возвращает строку initData из Telegram WebApp.
 * Возвращает пустую строку если WebApp недоступен.
 */
export function getTelegramInitData(): string {
  return window.Telegram?.WebApp?.initData ?? '';
}

/**
 * Формирует заголовки аутентификации для обычных API-запросов.
 * Если initData недоступна — возвращает пустой объект.
 */
export function getTelegramHeaders(): HeadersInit {
  const initData = getTelegramInitData();
  return initData ? { 'x-telegram-init-data': initData } : {};
}

/**
 * Формирует заголовки для admin-эндпоинтов.
 *
 * Использует Telegram initData для аутентификации.
 * Admin-ключи НЕ должны храниться на клиенте (security risk — попадают в JS бандл).
 *
 * Dev-only escape hatch: если в localStorage есть `__dev_admin_key`, поднимаем
 * Bearer header. Используется только для локальной отладки (нет TG WebApp);
 * в продакшен-сборку не попадает потому что прод грузится в TMA с initData.
 */
export function getAdminHeaders(): HeadersInit {
  const initData = getTelegramInitData();

  if (initData) {
    return {
      'Content-Type': 'application/json',
      'x-telegram-init-data': initData,
    };
  }

  if (import.meta.env.DEV) {
    try {
      const devKey = window.localStorage?.getItem('__dev_admin_key');
      if (devKey) {
        return {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${devKey}`,
        };
      }
    } catch {
      // ignore
    }
  }

  return { 'Content-Type': 'application/json' };
}

/**
 * Выполняет fetch к admin-эндпоинту с автоматической подстановкой заголовков.
 * Бросает ошибку при не-OK статусе.
 */
export async function adminFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const API_BASE = import.meta.env.VITE_API_URL || '';
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    credentials: 'include',
    headers: {
      ...getAdminHeaders(),
      ...(init?.headers ?? {}),
    },
  });

  if (!res.ok) throw new Error(`adminFetch ${path}: ${res.status} ${res.statusText}`);
  return res.json() as Promise<T>;
}
