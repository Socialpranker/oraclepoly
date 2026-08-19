import { QueryClient } from '@tanstack/react-query';
import { createSyncStoragePersister } from '@tanstack/query-sync-storage-persister';
import { persistQueryClient } from '@tanstack/react-query-persist-client';

export const queryClient = new QueryClient({
    defaultOptions: {
        queries: {
            staleTime: 1000 * 60 * 5,      // 5 minutes
            gcTime: 1000 * 60 * 30,         // 30 минут (было 24h — bloat localStorage)
            retry: 1,                        // 1 retry (было 3 — слишком долго на ошибках)
            refetchOnWindowFocus: false,
        },
    },
});

// Persistence to localStorage for offline-first support
const persister = createSyncStoragePersister({
    storage: window.localStorage,
    key: 'oracle-query-cache-v2',
});

// Blacklist чувствительных ключей — НЕ сохраняем в localStorage (XSS exfiltration risk).
// Auth/me, balances, P&L, wallets, ML predictions, admin endpoints.
const SENSITIVE_QUERY_PREFIXES = [
    'auth',
    'me',
    'admin',
    'wallet',
    'balance',
    'pnl',
    'execution',
    'order',
    'private',
];

persistQueryClient({
    queryClient,
    persister,
    maxAge: 1000 * 60 * 30, // 30 минут (было 24h)
    dehydrateOptions: {
        shouldDehydrateQuery: (query) => {
            const head = String(query.queryKey?.[0] ?? '').toLowerCase();
            const isSensitive = SENSITIVE_QUERY_PREFIXES.some((p) => head.includes(p));
            return !isSensitive && query.state.status === 'success';
        },
    },
});
