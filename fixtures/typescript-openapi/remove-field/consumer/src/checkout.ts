import { User } from "./types";

/**
 * A display string. The placeholder can be dropped without changing behaviour,
 * so this reference is MECHANICALLY removable.
 */
export function formatContact(user: User): string {
  return `${user.fullName} <${user.email}> ${user.phoneNumber}`;
}

/**
 * An outbound payload. The key can be dropped -- the field no longer exists
 * upstream, so sending it is meaningless. Also mechanically removable.
 */
export function toCrmPayload(user: User): Record<string, string> {
  return {
    id: user.id,
    email: user.email,
    phone: user.phoneNumber,
  };
}
