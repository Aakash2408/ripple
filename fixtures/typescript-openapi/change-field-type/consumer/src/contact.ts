import { User } from "./types";

/**
 * These two functions are ALREADY CORRECT for the new contract -- a phone number
 * is a string, so trimming it and testing its prefix is exactly right.
 *
 * They do not compile today only because types.ts still declares the field as a
 * number. That is what makes this fixture a real test of change_field_type: the
 * error is in the declaration, and the two errors below are its symptoms. Fix the
 * declaration and both disappear without either function being touched.
 */
export function normalisePhone(user: User): string {
  return user.phoneNumber.trim();
}

export function isInternational(user: User): boolean {
  return user.phoneNumber.startsWith("+");
}
