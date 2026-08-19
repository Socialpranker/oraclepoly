if (window.Telegram && window.Telegram.WebApp) {
  const tg = window.Telegram.WebApp;
  tg.ready();
  tg.expand();
  if (tg.disableVerticalSwipes) tg.disableVerticalSwipes();
  if (tg.enableClosingConfirmation) tg.enableClosingConfirmation();
  document.documentElement.classList.add('tma');

  document.documentElement.style.setProperty('--tg-viewport-height', tg.viewportHeight + 'px');

  tg.onEvent('viewportChanged', () => {
    document.documentElement.style.setProperty('--tg-viewport-height', tg.viewportHeight + 'px');
  });

  document.documentElement.style.setProperty('--tg-theme-bg-color', tg.themeParams.bg_color || '#0A0A0B');

  document.addEventListener('touchstart', function (e) {
    var scrollable = document.scrollingElement || document.body;
    if (scrollable.scrollTop <= 0) {
      scrollable.scrollTop = 1;
    }
  }, { passive: true });
}
