/**
 * Derive a display name from a user's email local part, e.g.
 * "srivani@dwp.com" -> "Srivani". Returns null when no usable local part
 * exists. Shared by the TopNav profile tooltip and the Executive Report PDF
 * header so both render the same "<Name>'s Profile" label.
 */
export function profileNameFromEmail(email: string | null | undefined): string | null {
  const localPart = email?.trim().split("@", 1)[0]?.trim();
  if (!localPart) return null;
  return `${localPart.charAt(0).toUpperCase()}${localPart.slice(1)}`;
}
