#!/usr/bin/env node
/** Configure the already-deployed hz download channel using the owner's gh login. */
import { spawnSync } from 'node:child_process';
import { chmodSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { homedir, tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { normalizeDownloadBaseUrl, verifyDownloadChannel } from './setup-updates.mjs';

const REPOSITORY = 'zuijiaosy/cc-switch';

export function validateSettings(input) {
  const base = normalizeDownloadBaseUrl(input.base_url || '');
  const host = input.ssh_host;
  const user = input.ssh_user;
  const port = input.ssh_port ?? 22;
  if (typeof host !== 'string' || host.length > 253 ||
      host.split('.').some(label => !/^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$/.test(label))) {
    throw new Error('ssh_host 必须是单一主机名或 IPv4 地址，不能包含 SSH 参数。');
  }
  if (typeof user !== 'string' || !/^[a-z_][a-z0-9_-]{0,31}$/.test(user)) throw new Error('ssh_user 格式不正确。');
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('ssh_port 必须为 1–65535 的整数。');
  for (const name of ['ssh_key_file', 'known_hosts_file', 'public_key_file']) {
    if (typeof input[name] !== 'string' || !input[name] || input[name].includes('\0')) throw new Error(`缺少本机文件路径：${name}`);
  }
  return { ...input, base_url: base, ssh_host: host, ssh_user: user, ssh_port: port };
}

export function validateKnownHosts(text, host, port) {
  const expected = port === 22 ? host : `[${host}]:${port}`;
  const lines = text.split(/\r?\n/).filter(line => line.trim() && !line.trim().startsWith('#'));
  if (!lines.length || lines.length > 4) throw new Error('known_hosts 必须含有服务器 agent 核实过的主机公钥。');
  for (const line of lines) {
    const fields = line.trim().split(/\s+/);
    if (fields[0] !== expected || !/^(?:ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521))$/.test(fields[1] || '') ||
        !/^[A-Za-z0-9+/]+={0,2}$/.test(fields[2] || '') ||
        Buffer.from(fields[2] || '', 'base64').length < 16 ||
        Buffer.from(fields[2] || '', 'base64').toString('base64') !== fields[2]) {
      throw new Error(`known_hosts 只能固定 ${expected} 的实际主机公钥，不允许通配、扫描即信任或其他主机。`);
    }
  }
  return lines.join('\n') + '\n';
}

function command(program, args, input) {
  const result = spawnSync(program, args, {
    input, encoding: 'utf8', maxBuffer: 8 * 1024 * 1024,
    env: { ...process.env, GH_HOST: 'github.com', GH_PROMPT_DISABLED: 'true' },
    stdio: ['pipe', 'pipe', 'pipe'],
  });
  if (result.error || result.status !== 0) {
    // Do not echo subprocess output or credential input.
    throw new Error(`${program} 配置/连接检查失败。检查本机 gh 登录、服务器受限 SFTP、主机公钥和网络后重试。`);
  }
  return result.stdout;
}

function localPath(value) {
  return value.startsWith('~/') ? join(homedir(), value.slice(2)) : resolve(value);
}

export async function main(args = process.argv.slice(2)) {
  if (args.length !== 2 || args[0] !== '--config') throw new Error('用法：node .clean/scripts/setup-downloads.mjs --config /本机路径/download-setup.json');
  if (Number(process.versions.node.split('.')[0]) < 20) throw new Error('需要 Node.js 20 或更新版本。');
  process.umask(0o077);
  const settings = validateSettings(JSON.parse(readFileSync(args[1], 'utf8')));
  const key = readFileSync(localPath(settings.ssh_key_file), 'utf8');
  if (!key.trim().startsWith('-----BEGIN OPENSSH PRIVATE KEY-----')) throw new Error('需要专用 OpenSSH 私钥文件，不要使用服务器管理员密钥。');
  const known = validateKnownHosts(readFileSync(localPath(settings.known_hosts_file), 'utf8'), settings.ssh_host, settings.ssh_port);
  const publicKey = readFileSync(localPath(settings.public_key_file), 'utf8');
  command('gh', ['auth', 'status', '--hostname', 'github.com']);
  const repo = JSON.parse(command('gh', ['api', `repos/${REPOSITORY}`]));
  if (repo.full_name !== REPOSITORY || !repo.permissions?.admin) throw new Error(`当前 gh 账号没有 ${REPOSITORY} 的管理权限。`);
  await verifyDownloadChannel(settings.base_url, publicKey);
  const temporary = mkdtempSync(join(tmpdir(), 'cc-switch-download-setup-'));
  try {
    const identity = join(temporary, 'identity');
    const hosts = join(temporary, 'known_hosts');
    writeFileSync(identity, key, { mode: 0o600 });
    writeFileSync(hosts, known, { mode: 0o600 });
    chmodSync(identity, 0o600);
    command('sftp', [
      '-F', '/dev/null', '-b', '-', '-P', String(settings.ssh_port), '-i', identity,
      '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=yes',
      '-o', 'IdentityAgent=none',
      '-o', `UserKnownHostsFile=${hosts}`, '-o', 'GlobalKnownHostsFile=/dev/null',
      '-o', 'PasswordAuthentication=no', '-o', 'KbdInteractiveAuthentication=no', '-o', 'ConnectTimeout=15',
      `${settings.ssh_user}@${settings.ssh_host}`,
    ], 'ls /cc-switch\nls /receipts\n');
  } finally {
    rmSync(temporary, { recursive: true, force: true });
  }
  // The enabling base URL is written last, after public identity and SSH are
  // verified and all other fields are available. No private key is put in Git.
  command('gh', ['secret', 'set', 'DOWNLOAD_SSH_PRIVATE_KEY', '--repo', REPOSITORY], key);
  command('gh', ['secret', 'set', 'DOWNLOAD_SSH_KNOWN_HOSTS', '--repo', REPOSITORY], known);
  for (const [name, value] of Object.entries({
    DOWNLOAD_SSH_HOST: settings.ssh_host,
    DOWNLOAD_SSH_PORT: String(settings.ssh_port),
    DOWNLOAD_SSH_USER: settings.ssh_user,
  })) command('gh', ['variable', 'set', name, '--repo', REPOSITORY], value);
  command('gh', ['variable', 'set', 'DOWNLOAD_BASE_URL', '--repo', REPOSITORY], settings.base_url);
  console.log(`下载站身份与受限 SFTP 已验证，GitHub 配置已写入：${settings.base_url}`);
  console.log('现在运行 node .clean/scripts/setup-updates.mjs，完成签名配置并触发首次正式构建。');
  console.log('本命令只验证连接并配置通道；实际上传、服务器发布和全文件 HTTPS 校验以 Actions 结果为准。');
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(error => { console.error(error.message); process.exitCode = 1; });
}
