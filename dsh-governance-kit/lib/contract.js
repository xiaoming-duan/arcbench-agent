/**
 * 契约的规范化序列化与哈希 —— 生成与校验**必须共用**本文件。
 *
 * ═══════════════════════════════════════════════════════════════════════
 *  为什么"共用"是硬要求
 * ═══════════════════════════════════════════════════════════════════════
 * 两边各写一套口径是这类校验**最经典的失效方式**：
 *   生成时算 A、校验时算 B，于是永远不等（误报）或永远相等（漏检）。
 *
 * 本文件是纯函数，无依赖、无副作用，便于双向验证。
 * ═══════════════════════════════════════════════════════════════════════
 */

import { createHash } from 'node:crypto';

/** sha256 十六进制。 */
export function sha256(text) {
  return createHash('sha256').update(text, 'utf8').digest('hex');
}

/** YAML 标量转义：只在必要时加引号。 */
function scalar(value) {
  const s = String(value ?? '');
  if (s === '') return "''";
  // 含冒号+空格、井号、引号、换行，或以特殊字符开头 -> 加引号
  if (/[:#\n]/.test(s) || /^[\s\-?*&!|>%@`[\]{},]/.test(s) || /^\s|\s$/.test(s)) {
    return `"${s.replace(/\\/g, '\\\\').replace(/"/g, '\\"').replace(/\n/g, '\\n')}"`;
  }
  return s;
}

/**
 * 规范化序列化：**字段顺序固定**、缩进固定。
 *
 * 排序是必要的 —— 对象键顺序在 JS 里是插入顺序，
 * 若生成与校验时的构造顺序不同，字节就不同、哈希也就不同。
 */
export function serializeContract(contract) {
  const lines = [];
  lines.push(`req_id: ${scalar(contract.req_id)}`);
  lines.push('frozen: true');
  lines.push(`frozen_at: ${scalar(contract.frozen_at)}`);
  lines.push(`frozen_by: ${scalar(contract.frozen_by)}`);
  lines.push(`integrity:`);
  lines.push(`  algorithm: sha256`);
  lines.push(`  sha256: ${scalar(contract.integrity.sha256)}`);
  lines.push('cross_module_calls:');
  for (const c of contract.cross_module_calls ?? []) {
    lines.push(`  - upstream: ${scalar(c.upstream)}`);
    lines.push(`    symbol: ${scalar(c.symbol)}`);
    if (c.signature) lines.push(`    signature: ${scalar(c.signature)}`);
    if (c.semantics) lines.push(`    semantics: ${scalar(c.semantics)}`);
    lines.push(`    declared_arity: ${c.declared_arity ?? ''}`);
  }
  return lines.join('\n') + '\n';
}

/** 从签名文本解析形参个数：`f(a, b, c)` -> 3；解析不出返回 null。 */
export function declaredArity(signature) {
  const m = /\(([^)]*)\)/.exec(String(signature ?? ''));
  if (!m) return null;
  const body = m[1].trim();
  if (!body) return 0;
  let depth = 0;
  let count = 1;
  for (const ch of body) {
    if ('([{'.includes(ch)) depth += 1;
    else if (')]}'.includes(ch)) depth -= 1;
    else if (ch === ',' && depth === 0) count += 1;
  }
  return count;
}

/**
 * **正文哈希**：排除 `integrity` 自身后的序列化哈希。
 *
 * 自指无解 —— 哈希写进文件后再算哈希，值就变了。
 * 所以内嵌字段只能覆盖正文，用于**可移植自证**；
 * 「连 integrity 字段一起改」的情形由**侧车**（覆盖完整文件字节）兜住。
 */
export function bodyHash(contract) {
  const clone = { ...contract, integrity: { algorithm: 'sha256', sha256: '' } };
  return sha256(serializeContract(clone));
}

/** 三态判定名。 */
export const VERDICT = {
  MISSING: 'CONTRACT_MISSING',
  TAMPERED: 'CONTRACT_TAMPERED',
  MISMATCH: 'CONTRACT_MISMATCH',
  OK: 'CONTRACT_OK',
};
