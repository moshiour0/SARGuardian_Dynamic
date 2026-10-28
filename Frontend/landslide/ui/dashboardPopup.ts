/**
 * Write the owner-verified dashboard HTML into a same-origin about:blank popup.
 * The popup inherits the AOI workspace's site origin, allowing Google Maps
 * HTTP-referrer restrictions to be evaluated against the same website.
 */
export function writeDashboardHtml(popup: Pick<Window, 'document'>, html: string): void {
  popup.document.open();
  popup.document.write(html);
  popup.document.close();
}
