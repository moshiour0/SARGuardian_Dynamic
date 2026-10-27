/**
 * Write verified dashboard HTML into the same-origin about:blank popup.
 * Navigating to a blob: URL may change the Referer Google Maps sees and fail
 * HTTP-referrer-restricted browser keys even when the AOI selection map works.
 */
export function writeDashboardHtml(popup: Pick<Window, 'document'>, html: string): void {
  popup.document.open();
  popup.document.write(html);
  popup.document.close();
}
