import { useContext, createContext } from 'react';

export interface LevelProgress {
    current_level: number;
    xp_into_level: number;
    xp_for_next_level: number;
    cumulative_xp: number;
    cumulative_xp_next: number;
}

export interface LevelPerks {
    title: string;
    max_votes_per_day: number;
    has_profile_frame: boolean;
}

export interface NextPerk {
    level: number;
    title?: string;
    max_votes_per_day?: number;
    bonus_credits?: number;
    has_profile_frame?: boolean;
}

export interface User {
    telegram_id: number;
    username?: string;
    display_name?: string;
    avatar_url?: string;
    level: number;
    xp: number;
    total_xp: number;
    credits: number;
    daily_streak: number;
    is_admin?: boolean;
    last_activity_date?: string;
    created_at?: string;
    level_progress?: LevelProgress;
    level_perks?: LevelPerks;
    next_perk?: NextPerk;
    stats: {
        totalInsights: number;
        accuracyScore: number;
        lockedMarkets: number;
        globalRank: number;
    };
    topic_mastery: {
        category: string;
        level: number;
        xp: number;
        nextLevelXp: number;
        accuracy: number;
        title: string;
    }[];
    achievements: {
        id: string;
        title: string;
        icon: string;
        rarity: string;
    }[];
    recent_activity: {
        id: string;
        marketQuestion: string;
        action: string;
        creditsSpent: number;
        consensus: string;
        date: string;
        category: string;
    }[];
}

export interface AuthState {
    user: User | null;
    loading: boolean;
    error: string | null;
    refetch: () => Promise<void>;
}

export const AuthContext = createContext<AuthState | null>(null);

export function useAuth(): AuthState {
    const ctx = useContext(AuthContext);
    if (!ctx) {
        throw new Error('useAuth must be used within an AuthProvider');
    }
    return ctx;
}
