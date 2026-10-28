/**
 * Wait until a popup has loaded a real page on the AOI workspace origin.
 * about:blank is not a reliable HTTP referrer for Google Maps website-key restrictions.
 */
export function waitForDashboardPopupLoad(
  popup: Pick<Window, 'document' | 'addEventListener' | 'removeEventListener'>,
  origin: string,
): Promise<void> {
  const expectedOrigin = new URL(origin).origin;
  return new Promise((resolve) => {
    const checkLoaded = () => {
      try {
        const documentUrl = new URL(popup.document.URL);
        if (documentUrl.origin !== expectedOrigin || popup.document.readyState !== 'complete') return;
        popup.removeEventListener('load', checkLoaded);
        resolve();
      } catch {
        // The browser may still be replacing the initial about:blank document.
      }
    };

    popup.addEventListener('load', checkLoaded);
    checkLoaded();
  });
}

/**
 * Write the owner-verified dashboard HTML into the loaded same-origin popup.
 * This leaves the active document at its real HTTPS URL for Maps referrer checks.
 */
export function writeDashboardHtml(popup: Pick<Window, 'document'>, html: string): void {
  popup.document.open();
  popup.document.write(html);
  popup.document.close();
}
