// Whether the welcome screen should treat the owner as returning: a Möbius made from an existing account
// arrives with a handle. A handle claimed in this guide is the owner's first setup, so it does not count,
// and editing a handle hides the note while the form is open.
export function returningHandle({ handle, claimedHere, editing }) {
  return handle && !claimedHere && !editing ? handle : null
}
