import test from 'node:test';
import assert from 'node:assert/strict';
import { keyMaterial, keySetupPlan } from '../scripts/setup-updates.mjs';

const publicKey = byte => Buffer.from(`untrusted comment: test key\n${Buffer.concat([Buffer.from('Ed'), Buffer.alloc(40, byte)]).toString('base64')}\n`).toString('base64');
const empty = { remotePublic: '', remotePrivate: false, remotePassword: false, localPublic: '', localPrivate: false };

test('first setup generates a key only when no previous configuration exists', () => {
  assert.equal(keySetupPlan(empty), 'generate');
});
test('configured repository keys remain unchanged without requiring a local copy', () => {
  assert.equal(keySetupPlan({ ...empty, remotePublic: publicKey(1), remotePrivate: true, remotePassword: true }), 'keep-remote');
});
test('retry after partial setup reuses the matching backup', () => {
  assert.equal(keySetupPlan({ ...empty, remotePublic: publicKey(1), localPublic: publicKey(1), localPrivate: true }), 'use-backup');
});
test('partial remote setup without the original backup cannot rotate keys', () => {
  assert.throws(() => keySetupPlan({ ...empty, remotePrivate: true }), /不会覆盖现存私钥/);
  assert.throws(() => keySetupPlan({ ...empty, remotePrivate: true, localPublic: publicKey(2), localPrivate: true }), /不会覆盖现存私钥/);
  assert.throws(() => keySetupPlan({ ...empty, remotePublic: publicKey(1) }), /原密钥备份/);
});
test('a different backup cannot replace the configured public key', () => {
  assert.throws(() => keySetupPlan({ ...empty, remotePublic: publicKey(1), localPublic: publicKey(2), localPrivate: true }), /不能静默轮换/);
});
test('partial local backup and unresolved password configuration are rejected', () => {
  assert.throws(() => keySetupPlan({ ...empty, localPrivate: true }), /备份不完整/);
  assert.throws(() => keySetupPlan({ ...empty, remotePassword: true }), /签名密码/);
});
test('public key identity is independent of comments and outer encoding', () => {
  const raw = Buffer.from(publicKey(1), 'base64').toString().replace('test key', 'another comment');
  assert.equal(keyMaterial(raw), keyMaterial(publicKey(1)));
  assert.throws(() => keyMaterial('not-a-key'), /公钥格式/);
});
