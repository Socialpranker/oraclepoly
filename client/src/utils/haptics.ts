/**
 * Telegram Mini App haptic feedback wrapper.
 * No-ops silently when outside TMA context.
 */

type ImpactStyle = 'light' | 'medium' | 'heavy' | 'rigid' | 'soft';
type NotificationType = 'error' | 'success' | 'warning';

const hf = () => window.Telegram?.WebApp?.HapticFeedback;

export const haptic = {
  /** Light tap — tab switches, selections */
  light: () => hf()?.impactOccurred('light'),
  /** Medium tap — confirmations, unlocks */
  medium: () => hf()?.impactOccurred('medium'),
  /** Heavy tap — important actions */
  heavy: () => hf()?.impactOccurred('heavy'),
  /** Impact with custom style */
  impact: (style: ImpactStyle) => hf()?.impactOccurred(style),
  /** Success notification — vote confirmed, unlock success */
  success: () => hf()?.notificationOccurred('success'),
  /** Error notification — failed action */
  error: () => hf()?.notificationOccurred('error'),
  /** Warning notification */
  warning: () => hf()?.notificationOccurred('warning'),
  /** Notification with custom type */
  notify: (type: NotificationType) => hf()?.notificationOccurred(type),
  /** Selection changed — slider, picker */
  selection: () => hf()?.selectionChanged(),
};
