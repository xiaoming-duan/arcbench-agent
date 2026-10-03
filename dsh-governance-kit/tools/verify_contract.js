/**
 * verify_contract —— 校验冻结合同的完整性，返回**三态判定**。
 *
 * ═══════════════════════════════════════════════════════════════════════
 *  三态（与"实现不符合同"分开）
 * ═══════════════════════════════════════════════════════════════════════
 *   CONTRACT_MISSING    合同**不存在**（或未冻结 / 与需求声明**漂移**）
 *   CONTRACT_TAMPERED   合同**存在但被改过**（哈希与冻结时不一致）
 *   CONTRACT_MISMATCH   合同**未被改**，但**实现/调用**不符合它
 *
 * MISSING 里含**漂移**：合同文件原封不动、但需求声明变了 ——
 * 「声明改了而合同没重新冻结」与「合同被改」是**两件不同的事**。
 * 判定顺序：**篡改优先于漂移**（文件被改时报更具体、更可操作的结论）。
 * ═══════════════════════════════════════════════════════════════════════
 */

import { readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { bodyHash, VERDICT, sha256 } from '../lib/contract.js';

export const name = 'verify_contract';
export const description =
  '校验冻结合同：重算 sha256 并与冻结时记录比对，返回 CONTRACT_MISSING / ' +
  'CONTRACT_TAMPERED / CONTRACT_MISMATCH 三态之一。合同是实现阶段的**只读验收依据**。';

export const parameters = {
  type: 'object',
  properties: {
    dir: { type: 'string', description: '合同目录（通常是 <输出目录>/.arc/contracts）。' },
    req_id: { type: 'string', description: '需求 id。' },
    declared: {
      type: 'array',
      description: '需求 YAML 当前声明的调用（用于检测**漂移**）。省略则跳过漂移检查。',
      items: {
        type: 'object',
        properties: {
          upstream: { type: 'string' },
          symbol: { type: 'string' },
          signature: { type: 'string' },
        },
        required: ['upstream', 'symbol'],
        additionalProperties: false,
      },
    },
    actual_signature: {
      type: 'string',
      description:
        '实现/测试里**实际**的调用签名。提供时会与合同比对；不一致返回 CONTRACT_MISMATCH。',
    },
    actual_upstream: {
      type: 'string',
      description: 'actual_signature 对应的上游 req_id。',
    },
  },
  required: ['dir', 'req_id'],
  additionalProperties: false,
};

/** 从合同文本里取回结构化调用（只解析本模块写出的固定格式）。 */
function parseCalls(text) {
  const calls = [];
  let cur = null;
  for (const line of text.split('\n')) {
    const m = /^\s*-?\s*(\w+):\s*(.*)$/.exec(line);
    if (!m) continue;
    const [, key, raw] = m;
    const val = raw.trim().replace(/^"(.*)"$/s, '$1').replace(/\\"/g, '"').replace(/\\n/g, '\n');
    if (!val) continue;
    if (key === 'upstream') { cur = { upstream: val }; calls.push(cur); }
    else if (cur && key === 'symbol') cur.symbol = val;
    else if (cur && key === 'signature') cur.signature = val;
    else if (cur && key === 'declared_arity') cur.declared_arity = Number(val);
  }
  return calls;
}

function arityOf(sig) {
  const m = /\(([^)]*)\)/.exec(String(sig ?? ''));
  if (!m) return null;
  const body = m[1].trim();
  if (!body) return 0;
  let depth = 0, count = 1;
  for (const ch of body) {
    if ('([{'.includes(ch)) depth += 1;
    else if (')]}'.includes(ch)) depth -= 1;
    else if (ch === ',' && depth === 0) count += 1;
  }
  return count;
}

export async function execute(args) {
  const { dir, req_id, declared, actual_signature, actual_upstream } = args ?? {};
  if (!dir || !req_id) throw new Error('verify_contract: dir 与 req_id 必填');

  const contractPath = join(dir, `${req_id}.yaml`);
  const recordPath = join(dir, `${req_id}.sha256`);

  let text;
  try { text = await readFile(contractPath, 'utf8'); }
  catch { return { verdict: VERDICT.MISSING, req_id, detail: `合同不存在: ${contractPath}` }; }

  const recorded = (await readFile(recordPath, 'utf8').catch(() => '')).split('\n');
  const expected = (recorded[0] ?? '').trim();
  const frozenAt = (recorded[1] ?? '').trim();
  const actual = sha256(text);

  // ① 侧车：覆盖**完整文件字节**（权威）—— 连"只改 integrity 字段"也能发现
  if (expected && actual !== expected) {
    return {
      verdict: VERDICT.TAMPERED, req_id,
      expected_sha256: expected, actual_sha256: actual, frozen_at: frozenAt,
      detail: `合同在冻结后被修改 —— 期望 ${expected.slice(0, 16)}… / 实际 ${actual.slice(0, 16)}…` +
              `（冻结于 ${frozenAt || '未知'}）。合同是验收依据，实现阶段不得改动它。`,
      fix: '不要编辑合同文件。若确需变更契约，请改需求声明后**重新冻结**（会重算哈希）。',
    };
  }

  // ② 内嵌 integrity：覆盖**正文**（可移植）—— 侧车不在时仍能发现正文被改
  const inner = /sha256:\s*([0-9a-f]{64})/i.exec(text)?.[1] ?? '';
  const honest = sha256(
    text.replace(/(\bintegrity:\s*\n\s*algorithm:\s*sha256\s*\n\s*sha256:\s*)[0-9a-f]{64}/i, '$1'),
  );
  if (inner && honest !== actual) {
    // 侧车不在或未记录时，退到内嵌校验（粗判：文本长度/结构变了）
    if (!expected) {
      return {
        verdict: VERDICT.TAMPERED, req_id,
        expected_sha256: inner, actual_sha256: actual, frozen_at: frozenAt,
        detail: '侧车缺失，但内嵌 integrity 与文件内容不吻合 —— 合同可能被改过。',
        fix: '重新冻结以重建侧车与内嵌哈希。',
      };
    }
  }

  const calls = parseCalls(text);

  // ③ 漂移：合同未被改，但**需求声明**变了
  if (Array.isArray(declared) && declared.length > 0) {
    const key = (c) => `${c.upstream}/${c.symbol}(${c.signature ?? ''})`;
    const want = new Set(declared.map(key));
    const got = new Set(calls.map(key));
    const missing = [...want].filter((k) => !got.has(k));
    const extra = [...got].filter((k) => !want.has(k));
    if (missing.length || extra.length) {
      return {
        verdict: VERDICT.MISSING, req_id,
        detail:
          '合同与需求声明**不一致（漂移）** —— ' +
          (missing.length ? `冻结件缺少: ${missing.join(', ')}；` : '') +
          (extra.length ? `冻结件多出: ${extra.join(', ')}` : ''),
        fix: '合同文件未动，是需求声明改了。请重新冻结以同步。',
      };
    }
  }

  // ④ 实现不符：合同未被改，但**实际调用签名**不符
  if (actual_signature && actual_upstream) {
    const declaredCall = calls.find((c) => c.upstream === actual_upstream);
    if (declaredCall) {
      const want = declaredCall.declared_arity ?? arityOf(declaredCall.signature);
      const got = arityOf(actual_signature);
      if (want !== null && got !== null && want !== got) {
        return {
          verdict: VERDICT.MISMATCH, req_id,
          expected_sha256: expected, actual_sha256: actual,
          detail:
            `合同未被改，但**调用形状不符** —— 声明 ${declaredCall.signature}（${want} 个参数），` +
            `实际按 **${got} 个参数**调用 ${declaredCall.symbol}。`,
          fix: '按合同里的签名调用（个数与顺序一致）。**不要改合同** —— 改实现。',
        };
      }
    }
  }

  return {
    verdict: VERDICT.OK, req_id,
    sha256: actual, frozen_at: frozenAt,
    calls: calls.length,
    detail: `${calls.length} 条调用契约已冻结且一致`,
  };
}

export function render(args, value) {
  return [{ type: 'text', text: `[${value.verdict}] ${value.req_id}\n${value.detail}` +
    (value.fix ? `\n修正指令: ${value.fix}` : '') }];
}
