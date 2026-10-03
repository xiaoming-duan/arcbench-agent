/**
 * freeze_contract —— 把跨模块调用约定写成**不可篡改的验收合同**。
 *
 * ═══════════════════════════════════════════════════════════════════════
 *  实测动机（来自真实运行）
 * ═══════════════════════════════════════════════════════════════════════
 * 某需求拿到 **3 次**依赖归因提示 + 1 次阻断理由，仍未真实集成上游。
 * **提示词的约束力不够。** 契约必须落成制品 + 门禁，而不是留在提示里。
 *
 * 不可编辑性分三层（缺一层就漏一种情形）：
 *   权限位   0444          纵深防御（同用户可 chmod 回来，**不是安全边界**）
 *   内嵌     integrity     覆盖**正文** -> **可移植**自证
 *   侧车     .sha256       覆盖**完整文件字节** -> **权威**
 * ═══════════════════════════════════════════════════════════════════════
 */

import { chmod, mkdir, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { bodyHash, declaredArity, serializeContract, sha256 } from '../lib/contract.js';

export const name = 'freeze_contract';
export const description =
  '把一个需求的跨模块调用契约冻结成只读合同（0444 + sha256 侧车）。' +
  '合同是验收依据，实现阶段只读 —— 写入后任何改动都会被 verify_contract 检出。';

/** 模型可见的入参 schema（对应 ToolSchema.parameters）。 */
export const parameters = {
  type: 'object',
  properties: {
    dir: {
      type: 'string',
      description: '合同目录（通常是 <输出目录>/.arc/contracts）。不存在则创建。',
    },
    req_id: { type: 'string', description: '需求 id，例如 REQ-11。' },
    frozen_by: {
      type: 'string',
      description: '冻结者标识（审计用）。默认 "dsh-governance-kit"。',
    },
    calls: {
      type: 'array',
      description: '该需求对上游的调用约定。',
      items: {
        type: 'object',
        properties: {
          upstream: { type: 'string', description: '上游需求 id，例如 REQ-7。' },
          symbol: { type: 'string', description: '上游导出的符号名。' },
          signature: {
            type: 'string',
            description: '声明签名，例如 "updateQuantity(sku, quantity, from, to)"。',
          },
          semantics: { type: 'string', description: '该调用的语义。' },
        },
        required: ['upstream', 'symbol'],
        additionalProperties: false,
      },
    },
  },
  required: ['dir', 'req_id', 'calls'],
  additionalProperties: false,
};

/** 工具执行体。 */
export async function execute(args) {
  const { dir, req_id, calls } = args ?? {};
  const frozen_by = args?.frozen_by || 'dsh-governance-kit';

  if (!dir || !req_id) throw new Error('freeze_contract: dir 与 req_id 必填');
  if (!Array.isArray(calls) || calls.length === 0) {
    // 空声明**不生成合同** —— 与可选字段的默认关闭语义一致
    return { written: false, reason: 'NO_CALLS_DECLARED', req_id };
  }

  await mkdir(dir, { recursive: true });
  const contractPath = join(dir, `${req_id}.yaml`);
  const recordPath = join(dir, `${req_id}.sha256`);

  const contract = {
    req_id,
    frozen_at: new Date().toISOString().replace('T', ' ').slice(0, 19),
    frozen_by,
    integrity: { algorithm: 'sha256', sha256: '' },
    cross_module_calls: calls.map((c) => ({
      upstream: String(c.upstream),
      symbol: String(c.symbol),
      signature: String(c.signature ?? ''),
      semantics: String(c.semantics ?? ''),
      declared_arity: declaredArity(c.signature),
    })),
  };
  // 先算正文哈希（排除 integrity 自身），再写进去
  contract.integrity.sha256 = bodyHash(contract);

  const text = serializeContract(contract);

  // 重写前先解除只读（上一轮可能留了 0444）—— **两个文件都要解**
  for (const p of [contractPath, recordPath]) {
    try { await chmod(p, 0o644); } catch { /* 不存在，忽略 */ }
  }
  await writeFile(contractPath, text, 'utf8');
  const digest = sha256(text);
  await writeFile(recordPath, `${digest}\n${contract.frozen_at}\n${frozen_by}\n`, 'utf8');

  const result = { written: true, req_id, contract: contractPath, record: recordPath, sha256: digest };
  try { await chmod(contractPath, 0o444); await chmod(recordPath, 0o444); result.readonly = true; }
  catch (e) { result.readonly = false; result.readonly_error = String(e?.message ?? e); }

  return result;
}

/** 渲染给模型看的结果文本。 */
export function render(args, value) {
  if (!value?.written) {
    return [{ type: 'text', text: `未生成合同（${value?.reason ?? '未知原因'}）：${value?.req_id ?? ''}` }];
  }
  return [{
    type: 'text',
    text:
      `已冻结合同：${value.contract}\n` +
      `sha256: ${value.sha256}\n` +
      `只读(0444): ${value.readonly ? '是' : '否（文件系统可能不支持）'}\n` +
      `侧车: ${value.record}`,
  }];
}
