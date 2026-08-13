#!/usr/bin/env node
/**
 * gen-archive-ledger-bitable.js — 磐系列凭证归档【累计索引台账 · 飞书多维表版】
 *
 * 与 gen-archive-ledger.js（xlsx 版）互补：
 *   - xlsx 版 = 每批一张的明细核对归档报告（附件名 / 是否匹配）
 *   - 多维表版 = 长期累计的索引台账（凭证号 → 账期 → 附件数 → 文件夹链接）
 *
 * 节点：每月批次归档报告定稿后跑一次，把该批追加进对应公司 tab，台账自然成型。
 *
 * 列（用户定稿 4 列）：凭证号 / 账期 / 附件数 / 文件夹链接
 * 表：磐沄 / 磐曜 / 磐晓 / 磐旭 各一个数据表（无数据的出空表头）。
 * 放置位置：用户指定飞书夹 Y4iNfewqrloUAXdgCZycvGzWnZe（95_归档管理）。
 *
 * 数据来源：
 *   - /tmp/*-arch-report.json （gen-archive-report.py 产出，= 归档真相：凭证号 / 附件数）
 *   - 飞书云盘 live（解析各凭证号夹 token，构造文件夹链接）
 *
 * 幂等 / 累计：
 *   - 首次跑：建 app + 4 表，录磐沄数据，把 app_token / table_ids / 已录批次写进
 *     scripts/archive-ledger-bitable.json。
 *   - 再跑：复用同一 app，按 (entity, period) 跳过已录批次，只追加新批次（不重复录）。
 *
 * 用法：
 *   node gen-archive-ledger-bitable.js [--dir /tmp]
 *     --dir  报告 JSON 所在目录，默认 /tmp
 *
 * 授权：FINHKG-154 dev-ticket grant（wps-to-feishu-migration/scripts/**）。
 */
try { require('dotenv').config({ path: '/home/fiona/2_FIN_CHN/claude-slack-agent/.env' }); }
catch (e) { /* dotenv 可缺省，靠进程环境变量 */ }

const https = require('https');
const fs = require('fs');
const path = require('path');

const FEISHU_APP_ID = process.env.FEISHU_APP_ID;
const FEISHU_APP_SECRET = process.env.FEISHU_APP_SECRET;
const TARGET_FOLDER = 'Y4iNfewqrloUAXdgCZycvGzWnZe'; // 95_归档管理（用户指定）
const DOMAIN = 'lcnzfxq3rlhh.feishu.cn';
const CONFIG_PATH = path.join(__dirname, 'archive-ledger-bitable.json');

const TABS = [
  { key: '磐沄', entity: '磐沄' },
  { key: '磐曜', entity: '磐曜' },
  { key: '磐晓', entity: '磐晓' },
  { key: '磐旭', entity: '磐旭' },
];

// ── HTTP / auth ──
let tenantToken = '';
let tenantTokenExpiresAt = 0;

function httpsReq(options, body) {
  return new Promise((resolve, reject) => {
    const req = https.request(options, (res) => {
      let data = '';
      res.on('data', (c) => (data += c));
      res.on('end', () => {
        try { resolve(JSON.parse(data)); }
        catch (e) { reject(new Error(`Invalid JSON: ${data.slice(0, 500)}`)); }
      });
    });
    req.on('error', reject);
    if (body) req.write(body);
    req.end();
  });
}

async function ensureTenantToken() {
  const now = Math.floor(Date.now() / 1000);
  if (tenantToken && tenantTokenExpiresAt > now + 300) return tenantToken;
  const body = JSON.stringify({ app_id: FEISHU_APP_ID, app_secret: FEISHU_APP_SECRET });
  const res = await httpsReq({
    hostname: 'open.feishu.cn',
    path: '/open-apis/auth/v3/tenant_access_token/internal',
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) },
  }, body);
  if (!res.tenant_access_token) throw new Error('Failed to get tenant_access_token: ' + JSON.stringify(res));
  tenantToken = res.tenant_access_token;
  tenantTokenExpiresAt = now + (res.expire || 7200);
  return tenantToken;
}

async function api(method, p, body) {
  const token = await ensureTenantToken();
  const bodyStr = body ? JSON.stringify(body) : null;
  const headers = { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' };
  if (bodyStr) headers['Content-Length'] = String(Buffer.byteLength(bodyStr));
  const res = await httpsReq({
    hostname: 'open.feishu.cn', path: `/open-apis${p}`, method, headers,
  }, bodyStr);
  return res;
}

// ── Bitable ops ──
async function createBitableApp(name, folderToken) {
  const res = await api('POST', '/bitable/v1/apps', { name, folder_token: folderToken });
  if (res.code !== 0) throw new Error(`Create app failed: ${res.msg} (code ${res.code})`);
  return res.data.app;
}

async function listTables(appToken) {
  const res = await api('GET', `/bitable/v1/apps/${appToken}/tables?page_size=100`);
  if (res.code !== 0) throw new Error(`List tables failed: ${res.msg}`);
  return res.data.items || [];
}

async function createTable(appToken, tableName, fields) {
  const res = await api('POST', `/bitable/v1/apps/${appToken}/tables`, {
    table: { name: tableName, default_view_name: 'Grid view', fields },
  });
  if (res.code !== 0) throw new Error(`Create table "${tableName}" failed: ${res.msg}`);
  return res.data.table_id;
}

async function deleteTable(appToken, tableId) {
  const res = await api('DELETE', `/bitable/v1/apps/${appToken}/tables/${tableId}`);
  return res.code === 0;
}

async function batchCreateRecords(appToken, tableId, records) {
  // Feishu 单批最多 1000 条；这里每批 100。
  for (let i = 0; i < records.length; i += 100) {
    const slice = records.slice(i, i + 100);
    const res = await api('POST', `/bitable/v1/apps/${appToken}/tables/${tableId}/records/batch_create`,
      { records: slice.map(fields => ({ fields })) });
    if (res.code !== 0) throw new Error(`Batch create failed: ${res.msg}`);
  }
}

// ── Drive: 列文件夹子项 ──
async function listFolderChildren(folderToken) {
  const out = [];
  let pageToken = null;
  do {
    const qs = `?folder_token=${folderToken}&page_size=200` + (pageToken ? `&page_token=${pageToken}` : '');
    const res = await api('GET', `/drive/v1/files${qs}`);
    if (res.code !== 0) throw new Error(`List folder failed: ${res.msg}`);
    for (const f of (res.data.files || [])) {
      out.push({ name: f.name, token: f.token, type: f.type });
    }
    pageToken = res.data.has_more ? res.data.next_page_token : null;
  } while (pageToken);
  return out;
}

/** 在 root 下按名字找一个子文件夹 token。 */
async function findChildFolder(rootToken, name) {
  const children = await listFolderChildren(rootToken);
  const cands = new Set([name]);
  const hit = children.find(c => c.type === 'folder' && cands.has(c.name));
  return hit ? hit.token : null;
}

// ── 报告加载 ──
function loadReports(dir) {
  const byEntity = {};
  const files = fs.readdirSync(dir).filter(f => f.endsWith('-arch-report.json')).sort();
  for (const f of files) {
    let rep;
    try { rep = JSON.parse(fs.readFileSync(path.join(dir, f), 'utf8')); }
    catch (e) { console.error(`[skip] 解析失败 ${f}: ${e.message}`); continue; }
    if (!rep.entity) continue;
    (byEntity[rep.entity] = byEntity[rep.entity] || []).push(rep);
  }
  return byEntity;
}

// ── config ──
function loadConfig() {
  if (fs.existsSync(CONFIG_PATH)) {
    try { return JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf8')); }
    catch (e) { return { pushed: {} }; }
  }
  return { pushed: {} };
}
function saveConfig(cfg) {
  fs.writeFileSync(CONFIG_PATH, JSON.stringify(cfg, null, 2));
}

const FIELDS = [
  { field_name: '凭证号', type: 1 },
  { field_name: '账期', type: 1 },
  { field_name: '附件数', type: 2 },
  { field_name: '文件夹链接', type: 15 }, // 超链接
];

function folderUrl(token) {
  return `https://${DOMAIN}/drive/folder/${token}`;
}

/**
 * 给某公司某月的报告，产出待写入的记录字段数组。
 * archiveRoot: 该公司该 FY 的归档根 token（用于解析月份夹→凭证号夹 token）。
 */
async function buildRecords(rep, archiveRoot) {
  const monthLabel = rep.month_folder || `${rep.month}-${(rep.fy || 'FY-2024').replace(/^FY-/, '')}`;
  const monthTok = await findChildFolder(archiveRoot, monthLabel) || await findChildFolder(archiveRoot, `${rep.month}-${rep.period.slice(2, 4)}`);
  if (!monthTok) throw new Error(`找不到月份夹 "${monthLabel}" under ${archiveRoot}`);
  // 凭证号夹 token 映射
  const vFolders = await listFolderChildren(monthTok);
  const vnoToTok = {};
  for (const c of vFolders) if (c.type === 'folder') vnoToTok[c.name] = c.token;

  const records = [];
  for (const v of rep.vouchers) {
    const tok = vnoToTok[v.vno];
    records.push({
      '凭证号': v.vno,
      '账期': monthLabel,                       // e.g. 06-2024
      '附件数': Number(v.archived_count) || 0,
      '文件夹链接': tok ? { link: folderUrl(tok), text: v.vno } : '',
    });
  }
  return records;
}

// 各公司归档根（会计凭证 FY 根 token）——首版只录磐沄，其余按需补充。
// 磐沄 FY-2024 根 = CR2ifqHVUldrbxdg7OKckhKBnvh（gen-archive-report.py ARCHIVE_ROOT）。
const ENTITY_ARCHIVE_ROOT = {
  '磐沄': 'CR2ifqHVUldrbxdg7OKckhKBnvh',
  // 磐曜 / 磐晓 / 磐旭 待归档启动时补 token
};

// ── verify（只读）：校验台账结构、行数、样本、所在夹 ──
async function verify() {
  const cfg = loadConfig();
  if (!cfg.app_token) throw new Error('无 app_token，先跑一次正常流程');
  const APP = cfg.app_token, T = cfg.table_ids;
  const listFields = async (k) => (await api('GET', `/bitable/v1/apps/${APP}/tables/${T[k]}/fields`)).data.items || [];
  const count = async (k) => {
    let r = await api('GET', `/bitable/v1/apps/${APP}/tables/${T[k]}/records?page_size=100`);
    let n = (r.data.items || []).length, pt = r.data.next_page_token, more = r.data.has_more;
    while (more) { r = await api('GET', `/bitable/v1/apps/${APP}/tables/${T[k]}/records?page_size=100&page_token=${pt}`); n += (r.data.items || []).length; more = r.data.has_more; pt = r.data.next_page_token; }
    return n;
  };
  console.log('=== verify ===');
  for (const k of TABS.map(t => t.key)) console.log(`${k} 行数: ${await count(k)}`);
  const fl = await listFields('磐沄');
  console.log('磐沄列:', fl.map(f => `${f.field_name}(#${f.type})`).join(', '));
  const r = await api('GET', `/bitable/v1/apps/${APP}/tables/${T['磐沄']}/records?page_size=3`);
  for (const it of r.data.items) { const f = it.fields; console.log(`  样本: ${f['凭证号']} |账期 ${f['账期']} |附件数 ${f['附件数']} |链接 ${JSON.stringify(f['文件夹链接'])}`); }
  const am = (await api('GET', `/bitable/v1/apps/${APP}`)).data.app;
  // 多维表 app 元数据不回传 folder_token；实拉目标夹确认 app 在其中
  const kids = await listFolderChildren(TARGET_FOLDER);
  const inFolder = kids.find(k => k.token === APP);
  console.log('目标夹内含本 app:', inFolder ? `✓ (${inFolder.name})` : '✗');
  console.log('URL:', cfg.url);
}

async function main() {
  if (process.argv.includes('--verify')) return verify();
  const dir = (process.argv.indexOf('--dir') >= 0 ? process.argv[process.argv.indexOf('--dir') + 1] : '/tmp');
  const reports = loadReports(dir);
  const cfg = loadConfig();
  cfg.pushed = cfg.pushed || {};

  // 1. 确保 app 存在
  let appToken = cfg.app_token;
  let tableIds = cfg.table_ids || {};
  if (!appToken) {
    console.log('[create] 新建多维表 app ...');
    const app = await createBitableApp('磐系列凭证归档 · 索引台账', TARGET_FOLDER);
    appToken = app.app_token;
    // 删默认表
    const def = await listTables(appToken);
    // 建 4 表
    for (const t of TABS) {
      tableIds[t.key] = await createTable(appToken, t.key, FIELDS);
    }
    for (const d of def) await deleteTable(appToken, d.table_id);
    cfg.app_token = appToken;
    cfg.table_ids = tableIds;
    cfg.url = `https://${DOMAIN}/base/${appToken}`;
    saveConfig(cfg);
    console.log(`[create] app=${appToken} tables=${JSON.stringify(tableIds)}`);
    console.log(`[create] URL: ${cfg.url}`);
  } else {
    console.log(`[reuse] app=${appToken}`);
  }

  // 2. 按公司录数据（跳过已录批次）
  for (const tab of TABS) {
    const reps = reports[tab.entity] || [];
    const root = ENTITY_ARCHIVE_ROOT[tab.entity];
    for (const rep of reps) {
      const batchKey = `${tab.entity}|${rep.fy}|${rep.month}`;
      if (cfg.pushed[batchKey]) {
        console.log(`[skip] ${batchKey} 已录`);
        continue;
      }
      if (!root) {
        console.log(`[skip] ${tab.entity} 归档根未配置，跳过`);
        continue;
      }
      console.log(`[push] ${batchKey} 解析凭证夹并录入 ...`);
      const records = await buildRecords(rep, root);
      await batchCreateRecords(appToken, tableIds[tab.key], records);
      cfg.pushed[batchKey] = { count: records.length, at: new Date().toISOString() };
      saveConfig(cfg);
      console.log(`[push] ${batchKey} 录入 ${records.length} 行`);
    }
    if (!reps.length) console.log(`[empty] ${tab.entity} 无报告，保持空表头`);
  }

  console.log('\n=== done ===');
  console.log('URL:', cfg.url);
  console.log('config:', CONFIG_PATH);
}

if (require.main === module) {
  main().catch(e => { console.error('FATAL:', e); process.exit(1); });
}

module.exports = { buildRecords, FIELDS, TABS };
