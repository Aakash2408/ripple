/**
 * Generated from ../../spec/user.after.yaml -- do not edit by hand.
 *
 * `phoneNumber` is absent here because the spec removed it. That is what turns
 * every remaining reference into a compile error, which is the point of the
 * fixture: a consumer whose types are stale would prove nothing.
 */
export interface User {
  id: string;
  email: string;
  fullName: string;
}
