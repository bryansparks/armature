const token = location.hash.slice(1);
const base = (document.querySelector('meta[name="armature-base"]')?.getAttribute('content') || '').replace(/\/+$/, '');
const intro = document.getElementById('intro');
const errorMessage = document.getElementById('err');
const okMessage = document.getElementById('ok');
const sections = ['database', 'key', 'admin', 'done'].map((id) => document.getElementById(id));

async function call(path, body) {
  const options = { headers: { Accept: 'application/json', 'X-Setup-Token': token }, credentials: 'same-origin' };
  if (body !== undefined) Object.assign(options, { method: 'POST', body: JSON.stringify(body), headers: { ...options.headers, 'Content-Type': 'application/json' } });
  let response;
  try {
    response = await fetch(`${base}/setup/api/${path}`, options);
  } catch {
    throw new Error('Could not reach the server. Is `armature server run` still running?');
  }
  const payload = await response.json().catch(() => null);
  if (!response.ok) throw new Error(payload?.error || `Request failed (HTTP ${response.status}).`);
  return payload;
}

function show(id) {
  for (const section of sections) section.hidden = section.id !== id;
  const stepId = id === 'admin' || id === 'done' ? 'step-admin' : 'step-database';
  for (const step of document.querySelectorAll('.setup-steps li')) {
    if (step.id === stepId) step.setAttribute('aria-current', 'step');
    else step.removeAttribute('aria-current');
  }
  intro.hidden = true;
  document.getElementById(`${id}-title`).focus();
}

async function run(button, change) {
  errorMessage.textContent = '';
  okMessage.textContent = '';
  if (button) button.disabled = true;
  try {
    return await change();
  } catch (err) {
    errorMessage.textContent = err.message;
    return null;
  } finally {
    if (button) button.disabled = false;
  }
}

async function useDatabase(button, url) {
  const saved = await run(button, () => call('database', { url }));
  if (!saved) return;
  if (saved.new_key && saved.key_file) {
    document.getElementById('key-file').textContent = saved.key_file;
    show('key');
  } else {
    show('admin');
  }
}

document.getElementById('use-sqlite').addEventListener('click', (clickEvent) => useDatabase(clickEvent.currentTarget, null));
document.getElementById('database-continue').addEventListener('click', (clickEvent) => useDatabase(clickEvent.currentTarget, null));
document.getElementById('key-continue').addEventListener('click', () => show('admin'));

const databaseForm = document.getElementById('database-form');
document.getElementById('database-test').addEventListener('click', async (clickEvent) => {
  if (await run(clickEvent.currentTarget, () => call('database/test', { url: databaseForm.url.value }))) okMessage.textContent = 'Connected.';
});
databaseForm.addEventListener('submit', (submitEvent) => {
  submitEvent.preventDefault();
  useDatabase(databaseForm.querySelector('button[type="submit"]'), databaseForm.url.value);
});

const adminForm = document.getElementById('admin-form');
adminForm.addEventListener('submit', async (submitEvent) => {
  submitEvent.preventDefault();
  if (adminForm.password.value !== adminForm['password-again'].value) {
    errorMessage.textContent = 'The passwords differ.';
    return;
  }
  const created = await run(adminForm.querySelector('button'), () => call('admin', { username: adminForm.username.value.trim(), password: adminForm.password.value }));
  adminForm.password.value = '';
  adminForm['password-again'].value = '';
  if (!created) return;
  if (created.next) {
    location.replace(created.next);
    return;
  }
  document.getElementById('done-text').textContent = `Restarting on your configured address. Sign in at ${created.sign_in}.`;
  show('done');
});

async function start() {
  if (!token) {
    intro.textContent = 'Open the setup link printed in the terminal where you ran `armature server run`.';
    return;
  }
  const state = await run(null, () => call('state'));
  if (!state) {
    intro.hidden = true;
    return;
  }
  document.getElementById('password-help').textContent = `At least ${state.password_min} characters.`;
  if (state.step === 'admin') {
    show('admin');
    return;
  }
  const configured = state.config_exists || state.database_from_env;
  document.getElementById('database-configured').hidden = !configured;
  document.getElementById('database-choices').hidden = configured;
  document.getElementById('database-configured-text').textContent = state.config_exists
    ? `Armature uses the database named in ${state.config_file}.`
    : 'Armature uses the database named in ARMATURE_SERVER_DATABASE_URL.';
  show('database');
}

start();
