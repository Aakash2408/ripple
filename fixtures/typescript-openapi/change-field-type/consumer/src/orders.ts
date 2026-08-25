import { User } from "./types";

/**
 * Unrelated to the breaking change, and deliberately number-free: if it declared
 * a `: number` of its own it would sit outside the fix target anyway, but keeping
 * it clean makes the minimal-diff assertion unambiguous. If Ripple touches this
 * file, the fix is not minimal and the PR is not reviewable.
 */
export function orderLabel(user: User, orderId: string): string {
  return `${orderId} for ${user.email}`;
}
