import { api, ApiError, setCsrf, url } from './api.js';
import { returnHash, signInIntro, signInNotice, ssoLoginPath } from './session.js';

const form = document.getElementById('login');
const intro = document.getElementById('intro');
const errorMessage = document.getElementById('err');
const sso = document.getElementById('sso');
const totpField = document.getElementById('totp-field');
const back = returnHash(location.search, location.hash);

document.getElementById('sso-button').addEventListener('click', () => location.assign(url(ssoLoginPath(back))));

form.addEventListener('submit', async (submitEvent) => {
  submitEvent.preventDefault();
  const submitButton = form.querySelector('button');
  submitButton.disabled = true;
  errorMessage.textContent = '';
  try {
    const body = { username: form.username.value, password: form.password.value };
    if (!totpField.hidden) body.totp = form.totp.value.replace(/\s+/g, '');
    await api('/api/login', { method: 'POST', body });
    location.replace(url('/') + back);
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
    location.replace(url('/') + back);
    return;
  }
  setCsrf(sessionPayload?.csrf);
  const methods = sessionPayload?.auth || {};
  form.hidden = !methods.password;
  sso.hidden = !methods.oidc;
  document.getElementById('sso-or').hidden = !(methods.password && methods.oidc);
  totpField.hidden = !methods.totp;
  intro.textContent = signInIntro(methods);
  if (methods.password) form.username.focus();
}

errorMessage.textContent = signInNotice(location.search);

start();
