import { User } from "./types";

/**
 * Unrelated to the breaking change. If Ripple touches this file, the fix is not
 * minimal and the PR is not reviewable -- so the fixture asserts it is untouched.
 */
export function orderLabel(user: User, orderId: string): string {
  return `${orderId} for ${user.email}`;
}
