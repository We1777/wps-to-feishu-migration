#!/usr/bin/env node
/**
 * gen-archive-ledger.js — 磐系列凭证归档【累计索引台账】（4 公司 × 各一 tab）
 *
 * 节点：每月批次归档报告定稿后跑一次，把该批追加进对应公司 tab，台账自然成型。
 *
 * 数据来源：
 *   - /tmp/*-arch-report.json  （gen-archive-report.py --month 产出，= 归档真相）
 *   - /tmp/<entity>-<fy>-voucher.json  （凭证列表，补充 日期/摘要/金额；可选）
 *
 * 路由：报告里的 entity 字段 → 对应公司 tab。
 *   磐沄 / 磐曜 / 磐晓 / 磐旭 各一个 tab；无数据的公司出表头待填。
 *
 * 输出：累计索引台账 xlsx（4 公司 tab + 1 说明 tab），无 wrap text，长列 overflow。
 *
 * 用法：
 *   node gen-archive-ledger.js [--out /tmp/xxx.xlsx] [--dir /tmp] [--upload]
 *     --out   输出 xlsx 路径，默认 /tmp/pan-voucher-ledger.xlsx
 *     --dir   报告 JSON 所在目录，默认 /tmp
 *     --upload 生成后上传到飞书 95_归档管理 文件夹（依赖 upload-to-feishu.py）
 *
 * 授权：FINHKG-154 dev-ticket grant（wps-to-feishu-migration/scripts/**）。
 */
const xlsx = require('xlsx');
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');

const HERE = __dirname;
const MIG_ROOT = path.resolve(HERE, '..');

// 公司 tab 顺序（与 95_归档管理 输入区一致）
const TABS = [
  { key: '磐沄', entity: '磐沄', full: 'C3_北京磐沄科技有限公司' },
  { key: '磐曜', entity: '磐曜', full: 'C4_北京磐曜科技有限公司（已清算）' },
  { key: '磐晓', entity: '磐晓', full: 'C2_上海磐晓科技有限公司' },
  { key: '磐旭', entity: '磐旭', full: 'C1_北京磐旭科技有限公司' },
];

// entity → 凭证列表文件名前缀（用于富化；无则跳过富化）
const VOUCHER_PREFIX = {
  '磐沄': 'panyun',
  '磐曜': 'panyao',
  '磐晓': 'panxiao',
  '磐旭': 'panxu',
};

const COLS = [
  '财年', '月份', '凭证号', '日期', '摘要',
  '借方金额', '贷方金额',
  '归档附件数', '凭证注明张数', '是否匹配',
  '附件清单', '原文件夹', '备注',
];

function parseArgs() {
  const a = process.argv.slice(2);
  const out = { out: '/tmp/pan-voucher-ledger.xlsx', dir: '/tmp', upload: false };
  for (let i = 0; i < a.length; i++) {
    if (a[i] === '--out') out.out = a[++i];
    else if (a[i] === '--dir') out.dir = a[++i];
    else if (a[i] === '--upload') out.upload = true;
  }
  return out;
}

/** 扫描目录下所有 *-arch-report.json，按 entity 分组。 */
function loadReports(dir) {
  const byEntity = {};
  const files = fs.readdirSync(dir)
    .filter(f => f.endsWith('-arch-report.json'))
    .sort();
  for (const f of files) {
    let rep;
    try { rep = JSON.parse(fs.readFileSync(path.join(dir, f), 'utf8')); }
    catch (e) { console.error(`[skip] 解析失败 ${f}: ${e.message}`); continue; }
    const ent = rep.entity;
    if (!ent) continue;
    (byEntity[ent] = byEntity[ent] || []).push(rep);
  }
  return byEntity;
}

/**
 * 加载凭证列表，建 {(periodYYYYMM, 凭证字号): {日期, 摘要, 借方合计, 贷方合计}}。
 * 凭证号按月重置 → 必须用 (期间+字号) 作键。
 * 表头行（含 '日期'/'凭证字号' 字样）与空凭证字号行跳过。
 */
function loadVoucherIndex(dir, prefix, fy) {
  const file = path.join(dir, `${prefix}-${fy}-voucher.json`);
  if (!fs.existsSync(file)) return null;
  const rows = JSON.parse(fs.readFileSync(file, 'utf8'));
  const idx = {};
  for (const r of rows) {
    if (!Array.isArray(r) || r.length < 7) continue;
    const date = String(r[0] || '');
    const vno = String(r[1] || '').trim();
    if (!date || !vno) continue;
    if (date === '日期' || vno === '凭证字号') continue;       // 表头
    if (/公司|有限|期 至|凭证列表/.test(date) || /公司|有限/.test(vno)) continue; // 标题行
    const m = date.match(/^(\d{4})[-/.](\d{2})/);
    if (!m) continue;
    const period = m[1] + m[2];
    const num = (s) => { const n = Number(String(s).replace(/,/g, '')); return isFinite(n) && s !== '' && s != null ? n : 0; };
    const key = period + '|' + vno;
    if (!idx[key]) idx[key] = { date, summary: '', debit: 0, credit: 0 };
    const summary = String(r[2] || '').trim();
    if (summary && !idx[key].summary) idx[key].summary = summary;
    idx[key].debit += num(r[4]);
    idx[key].credit += num(r[5]);
  }
  return idx;
}

/** 一份报告 → 行数组。voucherIdx 可选用于富化。 */
function reportToRows(rep, voucherIdx) {
  const period = rep.period;            // e.g. 202406
  const fy = rep.fy.replace(/^FY-/, '');
  const month = rep.month;
  const vidx = voucherIdx || {};
  return rep.vouchers.map(v => {
    const key = period + '|' + v.vno;
    const meta = vidx[key] || {};
    const att = v.attachments || [];
    return [
      rep.fy,                              // 财年
      month,                               // 月份
      v.vno,                               // 凭证号
      meta.date || '',                     // 日期
      meta.summary || '',                  // 摘要
      meta.debit || '',                    // 借方金额
      meta.credit || '',                   // 贷方金额
      v.archived_count,                    // 归档附件数
      v.stated_count == null ? '' : v.stated_count, // 凭证注明张数
      v.match,                             // 是否匹配
      att.map(a => a.name).join('；'),     // 附件清单
      [...new Set(att.map(a => a.orig_folder))].join('；'), // 原文件夹
      v.match.startsWith('缺件') ? '归档少于凭证注明张数，drive 缺源单' : '',
    ];
  });
}

function fmtMoney(v) {
  if (v === '' || v == null) return '';
  return Number(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function buildSheet(rows) {
  const aoa = [COLS, ...rows];
  const ws = xlsx.utils.aoa_to_sheet(aoa);
  ws['!cols'] = [
    { wch: 9 }, { wch: 6 }, { wch: 9 }, { wch: 12 }, { wch: 30 },
    { wch: 14 }, { wch: 14 }, { wch: 11 }, { wch: 12 }, { wch: 12 },
    { wch: 60 }, { wch: 24 }, { wch: 26 },
  ];
  return ws;
}

function buildCover(entitiesCovered, perEntity) {
  const lines = [
    ['磐系列电子会计凭证归档 · 累计索引台账'],
    [],
    ['说明'],
    ['本台账按公司分 tab，累计记录各公司已完成归档的全部凭证。'],
    ['节点：每月批次归档报告定稿（含补归、缺件确认）后，由 gen-archive-ledger.js 追加进对应公司 tab。'],
    [],
    ['公司 tab', '范围（FY-2024 批次）', '本期已归档批次数', '凭证行数'],
  ];
  const scope = {
    '磐沄': 'FY-2024 起按月（c.会计凭证/FY-2022~2026 齐全）',
    '磐曜': 'FY-2024（c.会计凭证/FY-2023~2025；已清算，历史凭证归档）',
    '磐晓': 'FY-2025 起（无 FY-2024 会计凭证）',
    '磐旭': 'FY-2025 起（无 FY-2024 会计凭证）',
  };
  for (const t of TABS) {
    lines.push([t.full, scope[t.key], perEntity[t.entity]?.batches ?? 0, perEntity[t.entity]?.rows ?? 0]);
  }
  lines.push([], ['列定义']);
  for (const [i, c] of COLS.entries()) lines.push([`${i + 1}`, c]);
  lines.push([], ['生成时间', new Date().toISOString()]);
  const ws = xlsx.utils.aoa_to_sheet(lines);
  ws['!cols'] = [{ wch: 30 }, { wch: 46 }, { wch: 16 }, { wch: 10 }];
  return ws;
}

function upload(xlsxPath) {
  // 索引台账进 95_归档管理（与输入区同址），复用 upload-one-file.py
  const FOLDER = 'Y4iNfewqrloUAXdgCZycvGzWnZe'; // 95_归档管理（与输入区同址）
  try {
    const out = execFileSync('python3', [path.join(HERE, 'upload-one-file.py'),
      '--folder', FOLDER, xlsxPath], { encoding: 'utf8', cwd: MIG_ROOT });
    const m = out.match(/https:\/\/[^\s]+\/file\/[^\s]+/);
    if (m) { console.log(m[0]); return; }
    console.error('[upload] 未解析到链接，原始输出：\n' + out.slice(0, 500));
  } catch (e) {
    console.error('[upload] 失败：' + (e.stderr || e.message).slice(0, 500));
    console.error('可手动上传到飞书 95_归档管理 文件夹。');
  }
}

function main() {
  const args = parseArgs();
  const reports = loadReports(args.dir);
  const entitiesCovered = Object.keys(reports);

  // 每个公司 tab 的行 + 统计
  const perEntity = {};
  for (const tab of TABS) {
    const reps = reports[tab.entity] || [];
    let voucherIdx = null;
    if (reps.length) {
      // 取第一份报告的 fy 决定凭证列表年份
      const fy = reps[0].fy.replace(/^FY-/, '');
      voucherIdx = loadVoucherIndex(args.dir, VOUCHER_PREFIX[tab.entity] || '', fy);
    }
    let rows = [];
    for (const rep of reps) {
      rows = rows.concat(reportToRows(rep, voucherIdx));
    }
    // 按月份 + 凭证号排序
    rows.sort((a, b) => (a[1] + '').localeCompare(b[1] + '') || (a[2] + '').localeCompare(b[2] + ''));
    // 金额格式化
    rows = rows.map(r => [r[0], r[1], r[2], r[3], r[4], fmtMoney(r[5]), fmtMoney(r[6]), r[7], r[8], r[9], r[10], r[11], r[12]]);
    perEntity[tab.entity] = { rows, batches: reps.length };
  }

  const wb = xlsx.utils.book_new();
  // 说明 tab 置首
  xlsx.utils.book_append_sheet(wb, buildCover(entitiesCovered, perEntity), '说明');
  for (const tab of TABS) {
    xlsx.utils.book_append_sheet(wb, buildSheet(perEntity[tab.entity].rows), tab.key);
  }
  xlsx.writeFile(wb, args.out);
  console.error('wrote ' + args.out);
  // 汇总到 stderr
  for (const tab of TABS) {
    const p = perEntity[tab.entity];
    console.error(`[${tab.key}] ${p.batches} 批 / ${p.rows.length} 行`);
  }
  if (args.upload) upload(args.out);
}

if (require.main === module) main();
