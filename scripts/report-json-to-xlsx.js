#!/usr/bin/env node
/**
 * report-json-to-xlsx.js — 把 gen-archive-report.py 产出的 JSON 报告转成 xlsx
 * 用法：node report-json-to-xlsx.js <in.json> <out.xlsx>
 * 三张 sheet：汇总 / 凭证汇总 / 附件明细（不加 wrap text，长列 overflow）。
 */
const xlsx = require('xlsx');
const fs = require('fs');

const [, , inJson, outXlsx] = process.argv;
if (!inJson || !outXlsx) {
  console.error('用法: node report-json-to-xlsx.js <in.json> <out.xlsx>');
  process.exit(1);
}

const rep = JSON.parse(fs.readFileSync(inJson, 'utf8'));

// Sheet 1: 汇总
const summary = [
  ['公司', rep.entity],
  ['财年', rep.fy],
  ['月份', rep.month],
  ['期间', rep.period],
  ['月份夹', rep.month_folder],
  [],
  ['指标', '数值'],
  ['凭证总数', rep.stats.voucher_folders],
  ['归档附件总数', rep.stats.archived_attachments],
  ['全部匹配', rep.stats.full],
  ['部分匹配', rep.stats.partial],
  ['缺件', rep.stats.missing],
  ['超额', rep.stats.over],
  ['无附件（未注明张数）', rep.stats.no_stated],
  ['凭证列表无该号', rep.stats.not_in_list],
];

// Sheet 2: 凭证汇总（每凭证一行）
const vrows = [['凭证号', '归档附件数', '凭证注明张数', '匹配状态', '归档附件清单']];
for (const v of rep.vouchers) {
  vrows.push([
    v.vno,
    v.archived_count,
    v.stated_count == null ? '' : v.stated_count,
    v.match,
    v.attachments.map(a => a.name).join('；'),
  ]);
}

// Sheet 3: 附件明细（每附件一行，含原文件夹）
const arows = [['凭证号', '附件名', '原文件夹']];
for (const v of rep.vouchers) {
  for (const a of v.attachments) {
    arows.push([v.vno, a.name, a.orig_folder]);
  }
}

const wb = xlsx.utils.book_new();
const sS = xlsx.utils.aoa_to_sheet(summary);
const vS = xlsx.utils.aoa_to_sheet(vrows);
const aS = xlsx.utils.aoa_to_sheet(arows);
sS['!cols'] = [{ wch: 16 }, { wch: 40 }];
vS['!cols'] = [{ wch: 10 }, { wch: 12 }, { wch: 14 }, { wch: 14 }, { wch: 80 }];
aS['!cols'] = [{ wch: 10 }, { wch: 60 }, { wch: 30 }];
xlsx.utils.book_append_sheet(wb, sS, '汇总');
xlsx.utils.book_append_sheet(wb, vS, '凭证汇总');
xlsx.utils.book_append_sheet(wb, aS, '附件明细');
xlsx.writeFile(wb, outXlsx);
console.error('wrote ' + outXlsx);
