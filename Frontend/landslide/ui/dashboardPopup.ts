/**
 * Write the owner-verified dashboard HTML into a same-origin about:blank popup.
 * This keeps the authenticated result viewer in the site's origin rather than
 * navigating to a blob URL; the viewer's MapLibre basemap does not require a Google Maps key.
 */
export function writeDashboardHtml(popup: Pick<Window, 'document'>, html: string): void {
  popup.document.open();
  popup.document.write(html);
  popup.document.close();
}
