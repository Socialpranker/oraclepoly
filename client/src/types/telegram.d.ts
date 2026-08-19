interface TelegramHapticFeedback {
  impactOccurred(style: 'light' | 'medium' | 'heavy' | 'rigid' | 'soft'): void;
  notificationOccurred(type: 'error' | 'success' | 'warning'): void;
  selectionChanged(): void;
}

interface TelegramWebApp {
  ready(): void;
  expand(): void;
  disableVerticalSwipes?(): void;
  enableVerticalSwipes?(): void;
  isVerticalSwipesEnabled?: boolean;
  enableClosingConfirmation?(): void;
  disableClosingConfirmation?(): void;
  isClosingConfirmationEnabled?: boolean;
  HapticFeedback?: TelegramHapticFeedback;
  initData?: string;
  viewportHeight: number;
  themeParams: Record<string, string>;
  onEvent(event: string, callback: () => void): void;
  openLink(url: string): void;
  openTelegramLink(url: string): void;
  switchInlineQuery?(query: string, chatTypes?: string[]): void;
  initDataUnsafe?: {
    user?: {
      id: number;
      first_name: string;
      last_name?: string;
      username?: string;
      photo_url?: string;
    };
  };
}

declare global {
  interface Window {
    Telegram?: {
      WebApp?: TelegramWebApp;
    };
  }
}

export {};
