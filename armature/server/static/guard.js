// One page at a time can hold unsaved edits, so one check is enough.
let check = null;

export function protect(isDirty) {
  check = isDirty;
}

export function release() {
  check = null;
}

export const hasUnsaved = () => Boolean(check?.());

export async function mayLeave(ask) {
  if (!hasUnsaved()) return true;
  const leave = Boolean(await ask());
  if (leave) release();
  return leave;
}

// Going to the hash already shown renders the view again, through the same guard.
export function navigate(hash) {
  if (location.hash === hash) window.dispatchEvent(new HashChangeEvent('hashchange'));
  else location.hash = hash;
}
