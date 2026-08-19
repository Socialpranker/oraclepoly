/**
 * Telegram share utilities for Oracle.
 * Uses Telegram WebApp's switchInlineQuery or openTelegramLink
 * to share content natively within Telegram.
 */

const BOT_USERNAME = import.meta.env.VITE_BOT_USERNAME || 'YourBotUsername';

/**
 * Share text via Telegram's native share dialog.
 * Falls back to copying to clipboard if not in Telegram context.
 */
export function shareViaTelegram(text: string): boolean {
    const tg = window.Telegram?.WebApp;

    // Try Telegram's switchInlineQuery first (shares to chats)
    if (tg?.switchInlineQuery) {
        try {
            tg.switchInlineQuery(text, ['users', 'groups', 'channels']);
            return true;
        } catch {
            // fallback below
        }
    }

    // Fallback: open Telegram share URL
    const shareUrl = `https://t.me/share/url?url=${encodeURIComponent(`https://t.me/${BOT_USERNAME}`)}&text=${encodeURIComponent(text)}`;

    if (tg?.openTelegramLink) {
        tg.openTelegramLink(shareUrl);
        return true;
    }

    // Last resort: open in new tab
    window.open(shareUrl, '_blank');
    return true;
}

/**
 * Generate a share message for a correct prediction.
 */
export function sharePrediction(marketQuestion: string, prediction: 'YES' | 'NO'): boolean {
    const text = `🎯 I correctly predicted "${marketQuestion}" → ${prediction}!\n\nJoin Oracle and test your market prediction skills 🔮`;
    return shareViaTelegram(text);
}

/**
 * Generate a share message for leaderboard rank.
 */
export function shareLeaderboardRank(rank: number, period: string): boolean {
    const periodLabel = period === 'weekly' ? 'this week' : period === 'monthly' ? 'this month' : 'overall';
    const text = `🏆 I'm ranked #${rank} ${periodLabel} on Oracle!\n\nCan you beat my score? Join the prediction challenge 🔮`;
    return shareViaTelegram(text);
}

/**
 * Generate a share message for a user profile.
 */
export function shareProfile(displayName: string, level: number, accuracy: number): boolean {
    const text = `🔮 ${displayName} — Level ${level} Oracle Analyst with ${accuracy}% accuracy!\n\nJoin Oracle and test your prediction skills 🔮`;
    return shareViaTelegram(text);
}

/**
 * Generate a share message for an achievement unlock.
 */
export function shareAchievement(title: string, icon: string, rarity: string): boolean {
    const rarityEmoji = rarity === 'legendary' ? '✨' : rarity === 'epic' ? '💎' : rarity === 'rare' ? '⭐' : '🏅';
    const text = `${icon} I just unlocked "${title}" ${rarityEmoji} (${rarity}) on Oracle!\n\nJoin and start earning achievements 🔮`;
    return shareViaTelegram(text);
}
