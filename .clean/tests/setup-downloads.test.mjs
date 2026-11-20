import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';
import { normalizeDownloadBaseUrl, setupMode, verifyDownloadChannel } from '../scripts/setup-updates.mjs';
import { validateKnownHosts, validateSettings } from '../scripts/setup-downloads.mjs';

const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; });
const material = Buffer.concat([Buffer.from('Ed'), Buffer.alloc(40, 3)]);
const publicKey = Buffer.from(`untrusted comment: public test fixture\n${material.toString('base64')}\n`).toString('base64');
const base = 'https://dl.example.com/cc-switch';
const sshPublic = Buffer.alloc(64, 7).toString('base64');
const settings = {
  base_url: base, ssh_host: 'hz.example.com', ssh_user: 'cc-switch-upload', ssh_port: 22,
  ssh_key_file: '/tmp/upload-key', known_hosts_file: '/tmp/known_hosts', public_key_file: '/tmp/tauri.pub',
};
const channel = {
  schema: 1, application: 'cc-switch', repository: 'zuijiaosy/cc-switch', base_url: base,
  updater_public_key: publicKey, max_batch_bytes: 2 ** 31, min_free_bytes: 2 ** 32,
};

test('download origin is canonical HTTPS with the fixed application path', () => {
  assert.equal(normalizeDownloadBaseUrl(base + '/'), base);
  assert.equal(normalizeDownloadBaseUrl('https://DL.EXAMPLE.COM:443/cc-switch'), base);
  for (const bad of ['http://dl.example.com/cc-switch', 'https://localhost/cc-switch',
    'https://127.0.0.1/cc-switch', 'https://[::1]/cc-switch', 'https://user@dl.example.com/cc-switch',
    base + '?q=x', base + '#fragment', 'https://dl.example.com:8443/cc-switch',
    'https://dl.example.com/elsewhere', 'https://dl.example.com/cc-switch/../other']) {
    assert.throws(() => normalizeDownloadBaseUrl(bad));
  }
});

test('preparation is explicit and cannot silently accept a misspelled activation flag', () => {
  assert.equal(setupMode([]), 'activate');
  assert.equal(setupMode(['--prepare-only']), 'prepare');
  assert.throws(() => setupMode(['--prepare']));
  assert.throws(() => setupMode(['--prepare-only', '--activate']));
});

test('SSH settings reject option injection, invalid users and ports', () => {
  assert.equal(validateSettings(settings).ssh_port, 22);
  for (const patch of [{ ssh_host: '-oProxyCommand=x' }, { ssh_host: 'a@b' }, { ssh_host: 'a\nb' },
    { ssh_host: 'bad-.example.com' }, { ssh_host: 'hz.example.com.' }, { ssh_host: 'x'.repeat(64) + '.com' },
    { ssh_user: 'root;id' }, { ssh_port: 0 }, { ssh_port: 65536 }, { ssh_port: '22' }, { ssh_key_file: '' }]) {
    assert.throws(() => validateSettings({ ...settings, ...patch }));
  }
});

test('known_hosts pins only the verified destination and exact port', () => {
  assert.equal(validateKnownHosts(`hz.example.com ssh-ed25519 ${sshPublic}\n`, 'hz.example.com', 22), `hz.example.com ssh-ed25519 ${sshPublic}\n`);
  assert.equal(validateKnownHosts(`[hz.example.com]:2222 ssh-ed25519 ${sshPublic}\n`, 'hz.example.com', 2222), `[hz.example.com]:2222 ssh-ed25519 ${sshPublic}\n`);
  for (const text of ['* ssh-ed25519 YWJj', 'wrong.example.com ssh-ed25519 YWJj',
    '@cert-authority hz.example.com ssh-ed25519 YWJj', '|1|hash|host ssh-ed25519 YWJj']) {
    assert.throws(() => validateKnownHosts(text, 'hz.example.com', 22));
  }
});

test('server readiness verifies repository, origin and signing identity without following redirects', async () => {
  globalThis.fetch = async (url, options) => {
    assert.equal(url, base + '/channel.json');
    assert.equal(options.redirect, 'error');
    return { ok: true, text: async () => JSON.stringify(channel) };
  };
  assert.deepEqual(await verifyDownloadChannel(base, publicKey), channel);
});

test('server readiness rejects a different signing key or repository', async () => {
  const wrong = Buffer.from(`untrusted comment: other\n${Buffer.concat([Buffer.from('Ed'), Buffer.alloc(40, 9)]).toString('base64')}\n`).toString('base64');
  for (const patch of [{ updater_public_key: wrong }, { repository: 'other/project' },
    { base_url: 'https://wrong.example.com/cc-switch' }, { max_batch_bytes: 0 }, { min_free_bytes: -1 }]) {
    globalThis.fetch = async () => ({ ok: true, text: async () => JSON.stringify({ ...channel, ...patch }) });
    await assert.rejects(verifyDownloadChannel(base, publicKey));
  }
});

test('unavailable or oversized readiness documents cannot enable deployment', async () => {
  globalThis.fetch = async () => ({ ok: false });
  await assert.rejects(verifyDownloadChannel(base, publicKey));
  globalThis.fetch = async () => ({ ok: true, text: async () => 'x'.repeat(65537) });
  await assert.rejects(verifyDownloadChannel(base, publicKey));
});
