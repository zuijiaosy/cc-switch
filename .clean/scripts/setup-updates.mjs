#!/usr/bin/env node
/** One-time setup on the repository owner's already-authenticated computer. */
import { spawnSync } from 'node:child_process';
import { chmodSync, existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { setTimeout as delay } from 'node:timers/promises';

const REPOSITORY = 'zuijiaosy/cc-switch';
const ENDPOINT = `https://github.com/${REPOSITORY}/releases/latest/download/latest.json`;
const CLI_VERSION = '2.12.4';

export function normalizeDownloadBaseUrl(value) {
  let url;
  try { url = new URL(value); } catch { throw new Error('下载地址必须为 https://下载域名/cc-switch。'); }
  const labels = url.hostname.split('.');
  if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash || url.port ||
      url.pathname.replace(/\/$/, '') !== '/cc-switch' || labels.length < 2 ||
      labels.some(label => !/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/i.test(label)) ||
      /^\d+(?:\.\d+){3}$/.test(url.hostname) || /(?:^|\.)(?:localhost|local)$/i.test(url.hostname)) {
    throw new Error('下载地址必须为 https://下载域名/cc-switch，不允许 IP、账号、查询参数或自定义端口。');
  }
  return `${url.origin}/cc-switch`;
}

export function setupMode(args) {
  if (!args.length) return 'activate';
  if (args.length === 1 && args[0] === '--prepare-only') return 'prepare';
  throw new Error('用法：node .clean/scripts/setup-updates.mjs [--prepare-only]');
}

export async function verifyDownloadChannel(base, publicKey) {
  const response = await fetch(`${base}/channel.json`, {
    redirect: 'error', headers: { 'Cache-Control': 'no-cache' }, signal: AbortSignal.timeout(15000),
  });
  if (!response.ok) throw new Error('下载站 channel.json 尚不可公开读取，请先完成服务器部署。');
  const raw = await response.text();
  if (Buffer.byteLength(raw) > 64 * 1024) throw new Error('下载站 channel.json 超过大小限制。');
  const channel = JSON.parse(raw);
  if (channel.schema !== 1 || channel.application !== 'cc-switch' || channel.repository !== REPOSITORY ||
      channel.base_url !== base || keyMaterial(channel.updater_public_key || '') !== keyMaterial(publicKey) ||
      !Number.isSafeInteger(channel.max_batch_bytes) || channel.max_batch_bytes <= 0 ||
      !Number.isSafeInteger(channel.min_free_bytes) || channel.min_free_bytes <= 0) {
    throw new Error('下载站身份、公钥或容量配置与此仓库不一致，不能启用。');
  }
  return channel;
}

export function keyMaterial(value) {
  const trimmed = value.trim();
  const text = trimmed.startsWith('untrusted comment:')
    ? trimmed
    : Buffer.from(trimmed, 'base64').toString('utf8').trim();
  const lines = text.split(/\r?\n/);
  if (lines.length !== 2 || !lines[0].startsWith('untrusted comment:')) {
    throw new Error('Tauri 公钥格式不正确，请保留原有密钥并检查配置。');
  }
  const key = Buffer.from(lines[1], 'base64');
  if (key.length !== 42 || !['Ed', 'ED'].includes(key.subarray(0, 2).toString())) {
    throw new Error('Tauri 公钥不是有效的 minisign Ed25519 公钥。');
  }
  return key.toString('base64');
}

export function keySetupPlan({ remotePublic, remotePrivate, remotePassword, localPublic, localPrivate }) {
  if (remotePublic) keyMaterial(remotePublic);
  // An existing configured pair is never replaced or rotated by this script.
  if (remotePublic && remotePrivate) return 'keep-remote';
  if (remotePrivate && !remotePublic) {
    throw new Error('仓库仅有私钥 Secret，无法核实本机备份是否与它匹配。请先恢复原公钥；脚本不会覆盖现存私钥。');
  }
  if (Boolean(localPublic) !== Boolean(localPrivate)) {
    throw new Error('本机签名备份不完整：需要同时恢复 signing.key 和 signing.key.pub，不能生成另一把密钥。');
  }
  if (remotePassword) {
    throw new Error('已存在签名密码 Secret，但签名配置不完整。请恢复原有密钥，不要用无密码新密钥覆盖。');
  }
  if ((remotePublic || remotePrivate) && !localPublic) {
    throw new Error('仓库已有部分签名配置。请先恢复原密钥备份；脚本不会生成新密钥覆盖它。');
  }
  if (localPublic) {
    const local = keyMaterial(localPublic);
    if (remotePublic && local !== keyMaterial(remotePublic)) {
      throw new Error('本机公钥与仓库已配置公钥不同。保留原密钥，不能静默轮换。');
    }
    return 'use-backup';
  }
  return 'generate';
}

function command(program, args, { input, allowNotFound = false } = {}) {
  const result = spawnSync(program, args, {
    encoding: 'utf8', input, maxBuffer: 8 * 1024 * 1024,
    env: { ...process.env, GH_HOST: 'github.com', GH_PROMPT_DISABLED: 'true' },
    stdio: ['pipe', 'pipe', 'pipe'],
  });
  if (result.error) throw new Error(`无法运行 ${program}。请先安装 GitHub CLI 与 Node.js/npm。`);
  if (result.status !== 0) {
    if (allowNotFound && /\(HTTP 404\)/.test(result.stderr)) return null;
    // Never echo child output: key generation and stdin may contain secrets.
    throw new Error(`${program} ${args.slice(0, 2).join(' ')} 执行失败（退出码 ${result.status}）。请检查本机登录、权限或网络后重试；已保存的签名密钥会保留。`);
  }
  return result.stdout;
}

function github(path, options = {}) {
  const raw = command('gh', ['api', path], options);
  return raw === null ? null : JSON.parse(raw);
}

async function waitForPublicDownloads(previous) {
  const urls = [`https://api.github.com/repos/${REPOSITORY}`];
  if (previous?.tag_name?.startsWith('clean-')) {
    const expected = `https://github.com/${REPOSITORY}/releases/download/${previous.tag_name}/clean-build-provenance.json`;
    if (!previous.assets?.some(asset => asset.name === 'clean-build-provenance.json' && asset.browser_download_url === expected)) {
      throw new Error('上一个清理版缺少可验证的构建来源文件，请先检查 Releases。');
    }
    urls.push(expected);
  }
  for (let attempt = 0; attempt < 12; attempt++) {
    try {
      for (const url of urls) {
        const response = await fetch(url, {
          headers: { 'User-Agent': 'cc-switch-update-setup', 'Cache-Control': 'no-cache' },
          signal: AbortSignal.timeout(15000),
        });
        if (!response.ok) throw new Error('公开下载正在生效');
        const data = await response.json();
        if (url.startsWith('https://api.github.com/') && (data.private || data.full_name !== REPOSITORY)) {
          throw new Error('仓库仍不可公开读取');
        }
        if (!url.startsWith('https://api.github.com/') && (data.repository !== REPOSITORY || data.release_tag !== previous.tag_name)) {
          throw new Error('旧版本构建来源不匹配');
        }
      }
      return;
    } catch {
      if (attempt === 11) break;
      console.log('等待 GitHub 公开下载生效…');
      await delay(10000);
    }
  }
  throw new Error('仓库设置已提交，但公开下载尚未确认。请稍后重新运行本脚本；它会复用已有密钥。');
}

export async function main(args = process.argv.slice(2)) {
  const mode = setupMode(args);
  if (Number(process.versions.node.split('.')[0]) < 20) throw new Error('需要 Node.js 20 或更新版本。');
  process.umask(0o077);
  command('gh', ['auth', 'status', '--hostname', 'github.com']);
  const repository = github(`repos/${REPOSITORY}`);
  if (repository.full_name !== REPOSITORY || !repository.permissions?.admin) {
    throw new Error(`当前 GitHub 账号没有 ${REPOSITORY} 的管理权限。`);
  }
  const publicVariable = github(`repos/${REPOSITORY}/actions/variables/TAURI_PUBLIC_KEY`, { allowNotFound: true });
  const privateSecret = github(`repos/${REPOSITORY}/actions/secrets/TAURI_SIGNING_PRIVATE_KEY`, { allowNotFound: true });
  const passwordSecret = github(`repos/${REPOSITORY}/actions/secrets/TAURI_SIGNING_PRIVATE_KEY_PASSWORD`, { allowNotFound: true });
  const downloadVariable = github(`repos/${REPOSITORY}/actions/variables/DOWNLOAD_BASE_URL`, { allowNotFound: true });
  const previous = github(`repos/${REPOSITORY}/releases/latest`, { allowNotFound: true });
  const keyDirectory = join(homedir(), '.config', 'cc-switch-clean', 'zuijiaosy-cc-switch');
  const privatePath = join(keyDirectory, 'signing.key');
  const publicPath = privatePath + '.pub';
  const localPublic = existsSync(publicPath) ? readFileSync(publicPath, 'utf8') : '';
  const plan = keySetupPlan({
    remotePublic: publicVariable?.value || '', remotePrivate: Boolean(privateSecret),
    remotePassword: Boolean(passwordSecret), localPublic, localPrivate: existsSync(privatePath),
  });
  mkdirSync(keyDirectory, { recursive: true, mode: 0o700 });
  chmodSync(keyDirectory, 0o700);
  if (plan === 'keep-remote') {
    console.log('保留仓库现有签名配置，不生成或替换密钥。');
  } else {
    if (plan === 'generate') {
      console.log(`使用官方 Tauri CLI ${CLI_VERSION} 生成本机签名备份…`);
      command(process.platform === 'win32' ? 'npx.cmd' : 'npx', [
        '--yes', '--package', `@tauri-apps/cli@${CLI_VERSION}`, 'tauri',
        'signer', 'generate', '--ci', '--password', '', '--write-keys', privatePath,
      ]);
    }
    if (!existsSync(privatePath) || !existsSync(publicPath)) throw new Error('签名文件生成不完整，请检查本机备份目录。');
    chmodSync(privatePath, 0o600);
    chmodSync(publicPath, 0o600);
    console.log(`签名备份保存在 ${keyDirectory}，请保留该目录；私钥未输出到终端或写入 Git。`);
  }
  const publicKey = plan === 'keep-remote' ? publicVariable.value.trim() : readFileSync(publicPath, 'utf8').trim();
  keyMaterial(publicKey);
  const exportedPublic = join(keyDirectory, 'download-channel-public.key');
  writeFileSync(exportedPublic, publicKey + '\n', { mode: 0o600 });
  chmodSync(exportedPublic, 0o600);
  if (mode === 'prepare') {
    console.log(`签名公钥已准备：${exportedPublic}`);
    console.log('本次未写 GitHub 配置、未公开仓库、未触发构建。把这份公钥交给 hz 发布程序，完成下载站配置后再运行不带 --prepare-only 的命令。');
    return;
  }
  const downloadBase = downloadVariable?.value ? normalizeDownloadBaseUrl(downloadVariable.value) : '';
  if (downloadBase) {
    for (const name of ['DOWNLOAD_SSH_HOST', 'DOWNLOAD_SSH_USER']) {
      if (!github(`repos/${REPOSITORY}/actions/variables/${name}`, { allowNotFound: true })?.value) {
        throw new Error(`缺少 ${name}；先运行 setup-downloads.mjs 配置下载通道。`);
      }
    }
    for (const name of ['DOWNLOAD_SSH_PRIVATE_KEY', 'DOWNLOAD_SSH_KNOWN_HOSTS']) {
      if (!github(`repos/${REPOSITORY}/actions/secrets/${name}`, { allowNotFound: true })) {
        throw new Error(`缺少 ${name}；先运行 setup-downloads.mjs 配置下载通道。`);
      }
    }
    await verifyDownloadChannel(downloadBase, publicKey);
  }
  if (repository.private) {
    console.log(`按仓库所有者要求，将 ${REPOSITORY} 的代码、历史和 Releases 设置为公开。`);
    command('gh', ['api', '--method', 'PATCH', `repos/${REPOSITORY}`, '--input', '-'], {
      input: JSON.stringify({ private: false }),
    });
  }
  await waitForPublicDownloads(previous);
  // Public reachability and the optional server channel are verified before
  // completing the key pair that enables scheduled builds.
  if (plan !== 'keep-remote') {
    command('gh', ['variable', 'set', 'TAURI_PUBLIC_KEY', '--repo', REPOSITORY], { input: publicKey });
    command('gh', ['secret', 'set', 'TAURI_SIGNING_PRIVATE_KEY', '--repo', REPOSITORY], {
      input: readFileSync(privatePath),
    });
  }
  command('gh', ['variable', 'set', 'CLEAN_UPDATE_ENDPOINT', '--repo', REPOSITORY], {
    input: downloadBase ? `${downloadBase}/stable/latest.json` : ENDPOINT,
  });
  command('gh', ['workflow', 'enable', 'clean-release.yml', '--repo', REPOSITORY]);
  command('gh', ['workflow', 'run', 'clean-release.yml', '--repo', REPOSITORY, '--ref', 'main']);
  console.log(`设置完成，已触发签名版本构建：https://github.com/${REPOSITORY}/actions/workflows/clean-release.yml`);
  console.log('构建和下载通道验证完成后，首次手动安装新的签名版本；以后的更高官方版本通过客户端自己的更新入口安装。');
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(error => { console.error(error.message); process.exitCode = 1; });
}
