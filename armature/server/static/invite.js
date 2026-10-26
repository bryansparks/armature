import { api, ApiError, url } from './api.js';
import { secretFromOtpauth, tokenFromHash } from './otpauth.js';

const form = document.getElementById('invite');
const intro = document.getElementById('intro');
const errorMessage = document.getElementById('err');

// The token lives in the fragment so it never reaches a log. Drop it from the address bar and history too.
const token = tokenFromHash(location.hash);
history.replaceState(null, '', location.pathname);

const messageOf = (err) => (err instanceof ApiError && err.status !== 0 ? err.message : 'Could not reach the server.');

async function start() {
  if (!token) {
    intro.textContent = 'This invite link is incomplete. Open the whole link you were sent, or ask for a new one.';
    return;
  }
  let invite;
  try {
    invite = await api('/api/invite/inspect', { method: 'POST', body: { token } });
  } catch (err) {
    intro.textContent = '';
    errorMessage.textContent = messageOf(err);
    return;
  }
  intro.textContent = 'Set a password of at least 12 characters and add an authenticator to finish setting up your account.';
  document.getElementById('who').textContent = `Username: ${invite.username}`;
  document.getElementById('otpauth').textContent = invite.otpauth_uri;
  document.getElementById('secret').textContent = secretFromOtpauth(invite.otpauth_uri);
  form.hidden = false;
  form.password.focus();
}

form.addEventListener('submit', async (submitEvent) => {
  submitEvent.preventDefault();
  errorMessage.textContent = '';
  if (form.password.value !== form['password-again'].value) {
    errorMessage.textContent = 'The two passwords differ.';
    form['password-again'].focus();
    return;
  }
  const submitButton = form.querySelector('button');
  submitButton.disabled = true;
  try {
    await api('/api/invite/accept', { method: 'POST', body: { token, password: form.password.value, code: form.code.value.replace(/\s+/g, '') } });
    location.replace(url('/'));
  } catch (err) {
    form.code.value = '';
    errorMessage.textContent = messageOf(err);
    form.code.focus();
  } finally {
    submitButton.disabled = false;
  }
});

start();
