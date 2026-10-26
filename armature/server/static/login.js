import { api, ApiError, setCsrf, url } from './api.js';
import { signInNotice } from './session.js';

const form = document.getElementById('login');
const intro = document.getElementById('intro');
const errorMessage = document.getElementById('err');
const sso = document.getElementById('sso');

document.getElementById('sso-button').addEventListener('click', () => location.assign(url('/auth/oidc/login')));

form.addEventListener('submit', async (submitEvent) => {
  submitEvent.preventDefault();
  const submitButton = form.querySelector('button');
  submitButton.disabled = true;
  errorMessage.textContent = '';
  try {
    await api('/api/login', { method: 'POST', body: { username: form.username.value, password: form.password.value, totp: form.totp.value.replace(/\s+/g, '') } });
    location.replace(url('/'));
  } catch (err) {
    form.password.value = '';
    form.totp.value = '';
    errorMessage.textContent = err instanceof ApiError && err.status !== 0 ? err.message : 'Could not reach the server.';
    form.password.focus();
  } finally {
    submitButton.disabled = false;
  }
});

async function start() {
  let sessionPayload;
  try {
    sessionPayload = await api('/api/session');
  } catch (err) {
    intro.textContent = '';
    errorMessage.textContent = err.message;
    return;
  }
  if (sessionPayload?.user) {
    location.replace(url('/'));
    return;
  }
  setCsrf(sessionPayload?.csrf);
  const methods = sessionPayload?.auth || {};
  form.hidden = !methods.password;
  sso.hidden = !methods.oidc;
  document.getElementById('sso-or').hidden = !(methods.password && methods.oidc);
  if (methods.password) {
    intro.textContent = 'Sign in with your username and password, plus your authenticator code once you have set one up.';
    form.username.focus();
  } else if (methods.oidc) intro.textContent = 'Sign in with your organization account.';
  else intro.textContent = 'No sign-in method is enabled on this server.';
}

errorMessage.textContent = signInNotice(location.search);

start();
