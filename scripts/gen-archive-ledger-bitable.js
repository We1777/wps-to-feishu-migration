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
 * 架构（2026-08-17 重构）：
 *   - 一家公司 = 一个独立多维表格
 *   - 每个表格内按财年建 table（FY-2024 / FY-2025 / ...）
 * 放置位置：用户指定飞书夹 Y4iNfewqrloUAXdgCZycvGzWnZe（95_归档管理）。
 *
 * 数据来源：
 *   - /tmp/*-arch-report.json （gen-archive-report.py 产出，= 归档真相：凭证号 / 附件数）
 *   - 飞书云盘 live（解析各凭证号夹 token，构造文件夹链接）
 *
 * 幂等 / 累计：
 *   - 首次跑：为公司创建独立多维表格 + 按财年建 table，把结构写进
 *     scripts/archive-ledger-bitable.json。
 *   - 再跑：复用已有表格，按 (entity, fy, month) 跳过已录批次，只追加新批次。
 *
 * 用法：
 *   node gen-archive-ledger-bitable.js [--dir /tmp] [--company 磐沄] [--verify]
 *     --dir     报告 JSON 所在目录，默认 /tmp
 *     --company 仅处理指定公司，默认全部
 *     --verify  只读校验，列出各公司各财年 table 的行数
 *   node gen-archive-ledger-bitable.js --reset-batch <公司> <财年> <月份>
 *     外科式重录单批次：删该账期（如 06-2024）全部行 + 清该批次已录标记，
 *     再跑正常流程即按最新报告重录该批次（补归档后刷新月批次用）。
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

/** 取某表全部 record id（分页拉取）。 */
async function listAllRecordIds(appToken, tableId) {
  const ids = [];
  let pt = null;
  do {
    const qs = `?page_size=500` + (pt ? `&page_token=${pt}` : '');
    const res = await api('GET', `/bitable/v1/apps/${appToken}/tables/${tableId}/records${qs}`);
    if (res.code !== 0) throw new Error(`List records failed: ${res.msg}`);
    for (const it of (res.data.items || [])) ids.push(it.record_id);
    pt = res.data.has_more ? res.data.page_token : null;
  } while (pt);
  return ids;
}

/** 清空某表全部记录（batch_delete，每批 500）。 */
async function deleteAllRecords(appToken, tableId) {
  const ids = await listAllRecordIds(appToken, tableId);
  for (let i = 0; i < ids.length; i += 500) {
    const slice = ids.slice(i, i + 500);
    const res = await api('POST', `/bitable/v1/apps/${appToken}/tables/${tableId}/records/batch_delete`,
      { records: slice });
    if (res.code !== 0) throw new Error(`Batch delete failed: ${res.msg}`);
  }
  return ids.length;
}

/** 按「账期」筛选记录 id（records/search，分页）。 */
async function findRecordIdsByPeriod(appToken, tableId, periodLabel) {
  const ids = [];
  let pt = null;
  do {
    const body = {
      filter: { conjunction: 'and', conditions: [{ field_name: '账期', operator: 'is', value: [periodLabel] }] },
      page_size: 500,
    };
    if (pt) body.page_token = pt;
    const res = await api('POST', `/bitable/v1/apps/${appToken}/tables/${tableId}/records/search`, body);
    if (res.code !== 0) throw new Error(`Search records failed: ${res.msg} (code ${res.code})`);
    for (const it of (res.data.items || [])) ids.push(it.record_id);
    pt = res.data.has_more ? res.data.page_token : null;
  } while (pt);
  return ids;
}

/** 删某账期全部记录（batch_delete，每批 500），返回删除数。 */
async function deleteRecordsByPeriod(appToken, tableId, periodLabel) {
  const ids = await findRecordIdsByPeriod(appToken, tableId, periodLabel);
  for (let i = 0; i < ids.length; i += 500) {
    const res = await api('POST', `/bitable/v1/apps/${appToken}/tables/${tableId}/records/batch_delete`,
      { records: ids.slice(i, i + 500) });
    if (res.code !== 0) throw new Error(`Batch delete failed: ${res.msg}`);
  }
  return ids.length;
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
    catch (e) { return {}; }
  }
  return {};
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
  // 排序：先按账期，再按凭证号
  records.sort((a, b) => {
    const periodCmp = a['账期'].localeCompare(b['账期']);
    if (periodCmp !== 0) return periodCmp;
    return a['凭证号'].localeCompare(b['凭证号'], 'zh-CN', { numeric: true });
  });
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
  console.log('=== verify ===');
  for (const tab of TABS) {
    const ccfg = cfg[tab.entity];
    if (!ccfg || !ccfg.app_token) {
      console.log(`${tab.entity}: 未创建台账`);
      continue;
    }
    const APP = ccfg.app_token;
    const tables = ccfg.tables || {};
    console.log(`${tab.entity}: ${ccfg.url}`);
    // 遍历每个财年 table
    for (const [fy, tableId] of Object.entries(tables)) {
      let r = await api('GET', `/bitable/v1/apps/${APP}/tables/${tableId}/records?page_size=1`);
      if (!r || r.code !== 0) {
        console.log(`  ${fy}: ERR ${r && r.code}:${r && r.msg}`);
        continue;
      }
      const total = typeof r.data.total === 'number' ? r.data.total : `(分页待算)`;
      console.log(`  ${fy}: ${total} 行`);
    }
    // 取第一个 table 的列定义作为样本
    const firstTableId = Object.values(tables)[0];
    if (firstTableId) {
      const fl = (await api('GET', `/bitable/v1/apps/${APP}/tables/${firstTableId}/fields`)).data.items || [];
      console.log(`  列: ${fl.map(f => `${f.field_name}(#${f.type})`).join(', ')}`);
      // 取 3 条样本记录
      const r = await api('GET', `/bitable/v1/apps/${APP}/tables/${firstTableId}/records?page_size=3`);
      for (const it of r.data.items) {
        const f = it.fields;
        console.log(`    样本: ${f['凭证号']} | 账期 ${f['账期']} | 附件数 ${f['附件数']} | 链接 ${JSON.stringify(f['文件夹链接'])}`);
      }
    }
  }
  console.log('配置文件:', CONFIG_PATH);
}

async function main() {
  if (process.argv.includes('--verify')) return verify();

  // --reset <entity>：清空某公司所有 table 记录 + 清已录批次
  const ri = process.argv.indexOf('--reset');
  if (ri >= 0) {
    const entity = process.argv[ri + 1];
    if (!entity) throw new Error('--reset 需跟公司名，如 --reset 磐沄');
    const cfg = loadConfig();
    const ccfg = cfg[entity];
    if (!ccfg || !ccfg.app_token || !ccfg.tables) {
      throw new Error(`无 ${entity} 台账，先跑一次正常流程`);
    }
    let totalDeleted = 0;
    for (const [fy, tableId] of Object.entries(ccfg.tables)) {
      const n = await deleteAllRecords(ccfg.app_token, tableId);
      console.log(`  ${fy}: 删 ${n} 条`);
      totalDeleted += n;
    }
    // 清已录批次
    const pushed = ccfg.pushed || {};
    const clearedCount = Object.keys(pushed).length;
    ccfg.pushed = {};
    saveConfig(cfg);
    console.log(`[reset] ${entity} 共删 ${totalDeleted} 条记录，清 ${clearedCount} 个已录批次标记`);
    return;
  }

  // --reset-batch <entity> <fy> <month>：外科式重录单批次（删该账期行 + 清已录标记，
  // 复拉验证删净后再动 config；随后正常流程按最新报告重录该批次）
  const rbi = process.argv.indexOf('--reset-batch');
  if (rbi >= 0) {
    const entity = process.argv[rbi + 1], fy = process.argv[rbi + 2], month = process.argv[rbi + 3];
    if (!entity || !fy || !month) throw new Error('--reset-batch 需跟 公司 财年 月份，如 --reset-batch 磐沄 FY-2024 06');
    const cfg = loadConfig();
    const ccfg = cfg[entity];
    if (!ccfg || !ccfg.app_token || !ccfg.tables || !ccfg.tables[fy]) {
      throw new Error(`无 ${entity} ${fy} 台账，先跑一次正常流程`);
    }
    const periodLabel = `${month}-${fy.replace(/^FY-/, '')}`;
    const n = await deleteRecordsByPeriod(ccfg.app_token, ccfg.tables[fy], periodLabel);
    const left = await findRecordIdsByPeriod(ccfg.app_token, ccfg.tables[fy], periodLabel);
    if (left.length) throw new Error(`删后复拉仍剩 ${left.length} 行，未删净`);
    if (ccfg.pushed) delete ccfg.pushed[`${fy}|${month}`];
    saveConfig(cfg);
    console.log(`[reset-batch] ${entity} ${fy}|${month}（账期 ${periodLabel}）删 ${n} 条并复拉验证删净，已清批次标记`);
    return;
  }

  const dir = (process.argv.indexOf('--dir') >= 0 ? process.argv[process.argv.indexOf('--dir') + 1] : '/tmp');
  const companyFilter = process.argv.indexOf('--company') >= 0 ? process.argv[process.argv.indexOf('--company') + 1] : null;
  const reports = loadReports(dir);
  const cfg = loadConfig();

  // 按公司处理
  for (const tab of TABS) {
    if (companyFilter && tab.entity !== companyFilter) {
      console.log(`[skip] ${tab.entity}（--company 过滤）`);
      continue;
    }

    const reps = reports[tab.entity] || [];
    const root = ENTITY_ARCHIVE_ROOT[tab.entity];

    // 1. 确保公司台账存在（独立多维表格）
    let ccfg = cfg[tab.entity] || {};
    if (!ccfg.app_token) {
      console.log(`[create] ${tab.entity} 新建独立多维表格...`);
      const app = await createBitableApp(`${tab.entity} 凭证归档 · 索引台账`, TARGET_FOLDER);
      ccfg.app_token = app.app_token;
      ccfg.url = `https://${DOMAIN}/base/${app.app_token}`;
      ccfg.tables = {};
      ccfg.pushed = {};
      // 删默认表
      const def = await listTables(app.app_token);
      for (const d of def) await deleteTable(app.app_token, d.table_id);
      cfg[tab.entity] = ccfg;
      saveConfig(cfg);
      console.log(`[create] ${tab.entity} app=${app.app_token}`);
      console.log(`[create] URL: ${ccfg.url}`);
    } else {
      console.log(`[reuse] ${tab.entity} app=${ccfg.app_token}`);
    }

    // 2. 按财年处理报告，自动创建对应 table
    for (const rep of reps) {
      const batchKey = `${rep.fy}|${rep.month}`;
      if (ccfg.pushed && ccfg.pushed[batchKey]) {
        console.log(`[skip] ${tab.entity} ${batchKey} 已录`);
        continue;
      }
      if (!root) {
        console.log(`[skip] ${tab.entity} 归档根未配置，跳过`);
        continue;
      }

      // 确保该财年的 table 存在
      if (!ccfg.tables[rep.fy]) {
        console.log(`[create] ${tab.entity} 新建财年表 ${rep.fy}...`);
        ccfg.tables[rep.fy] = await createTable(ccfg.app_token, rep.fy, FIELDS);
        saveConfig(cfg);
        console.log(`[create] ${tab.entity} ${rep.fy} table=${ccfg.tables[rep.fy]}`);
      }

      console.log(`[push] ${tab.entity} ${batchKey} 解析凭证夹并录入...`);
      const records = await buildRecords(rep, root);
      await batchCreateRecords(ccfg.app_token, ccfg.tables[rep.fy], records);
      if (!ccfg.pushed) ccfg.pushed = {};
      ccfg.pushed[batchKey] = { count: records.length, at: new Date().toISOString() };
      saveConfig(cfg);
      console.log(`[push] ${tab.entity} ${batchKey} 录入 ${records.length} 行`);
    }

    if (!reps.length) {
      console.log(`[empty] ${tab.entity} 无报告数据`);
      if (!ccfg.tables || Object.keys(ccfg.tables).length === 0) {
        console.log(`[hint] ${tab.entity} 无财年表，首次归档时将自动创建`);
      }
    }
  }

  console.log('\n=== done ===');
  for (const tab of TABS) {
    const ccfg = cfg[tab.entity];
    if (ccfg && ccfg.url) {
      console.log(`${tab.entity}: ${ccfg.url}`);
    }
  }
  console.log('config:', CONFIG_PATH);
}

if (require.main === module) {
  main().catch(e => { console.error('FATAL:', e); process.exit(1); });
}

module.exports = { buildRecords, FIELDS, TABS };
