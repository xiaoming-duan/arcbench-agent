/**
 * dsh-governance-kit 的功能自测 —— **端到端**，不依赖 DSH 运行时。
 *
 * 运行：node kit-test.mjs
 *
 * ═══════════════════════════════════════════════════════════════════════
 *  为什么必须有这个文件
 * ═══════════════════════════════════════════════════════════════════════
 * 一个"能注册进 DSH"的工具，与一个"真的能判定三态"的工具，是两件事。
 * 本文件验证后者：冻结 -> 完好 -> 漂移 -> 实现不符 -> 篡改 -> 不存在 -> 空声明 -> 重新冻结。
 *
 * 按本套件的纪律（LESSONS 纪律 1/2）：
 *   · 每种判定都给出**正确样本**与**错误样本**
 *   · 修改型夹具带**前提断言**（篡改必须真的改了字节）
 * ═══════════════════════════════════════════════════════════════════════
 */

import { chmod, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import * as freeze from './tools/freeze_contract.js';
import * as verify from './tools/verify_contract.js';

const CASE = [
  {
    upstream: 'REQ-7',
    symbol: 'updateQuantity',
    signature: 'updateQuantity(sku, quantity, from, to)',
    semantics: '更新库存数量并写入一条流水记录',
  },
  { upstream: 'REQ-1', symbol: 'listItems', signature: 'listItems(filter)' },
];

const RESULTS = [];
const ck = (label, ok, detail = '') => {
  RESULTS.push([label, ok]);
  console.log(`${ok ? '✅' : '❌'}  ${label}${detail ? `   ${detail}` : ''}`);
};

const dir = join(await mkdtemp(join(tmpdir(), 'kit-')), '.arc/contracts');

// ---- ① 冻结 ----
const w = await freeze.execute({ dir, req_id: 'REQ-11', calls: CASE, frozen_by: 'kit-test' });
ck('① 冻结写出合同 + 64 位 sha256', w.written === true && w.sha256?.length === 64,
   `sha256=${w.sha256?.slice(0, 12)}…`);
ck('①b 合同与侧车均置 0444 只读', w.readonly === true);

// ---- ② 完好 -> OK（正确样本：证明不误报）----
const v1 = await verify.execute({ dir, req_id: 'REQ-11' });
ck('② 完好合同 -> CONTRACT_OK', v1.verdict === 'CONTRACT_OK', v1.detail);

// ---- ③ 漂移：合同未动、声明变了 ----
const v2 = await verify.execute({ dir, req_id: 'REQ-11', declared: [CASE[0]] });
ck('③ 声明少一条（合同未动）-> CONTRACT_MISSING（漂移）',
   v2.verdict === 'CONTRACT_MISSING', String(v2.detail ?? '').slice(0, 64));

// ---- ④ 实现不符：合同未改、调用签名不符 ----
const v3 = await verify.execute({
  dir, req_id: 'REQ-11',
  actual_signature: 'updateQuantity(sku, from, to)', actual_upstream: 'REQ-7',
});
ck('④ 合同未改但调用 3 参 vs 声明 4 参 -> CONTRACT_MISMATCH',
   v3.verdict === 'CONTRACT_MISMATCH', String(v3.detail ?? '').slice(0, 72));

// ---- ⑤ 篡改（必须先 chmod 解除只读）----
const contractPath = join(dir, 'REQ-11.yaml');
const before = await readFile(contractPath, 'utf8');
await chmod(contractPath, 0o644);
await writeFile(contractPath, before.replace('from, to', 'from'), 'utf8');
// ★ 前提断言：篡改必须真的改了字节，否则后续断言毫无意义（LESSONS 纪律 2）
ck('⑤ 前提断言：篡改确实改变了文件字节', (await readFile(contractPath, 'utf8')) !== before);

const v4 = await verify.execute({ dir, req_id: 'REQ-11' });
ck('⑤b 篡改 -> CONTRACT_TAMPERED', v4.verdict === 'CONTRACT_TAMPERED',
   String(v4.detail ?? '').slice(0, 72));
ck('⑤c 理由含期望哈希 / 实际哈希 / 冻结时间戳',
   Boolean(v4.expected_sha256 && v4.actual_sha256 && v4.frozen_at));
ck('⑤d 理由含可执行修正指令', typeof v4.fix === 'string' && v4.fix.length > 0);

// ---- ⑥ 不存在 ----
const v5 = await verify.execute({ dir, req_id: 'REQ-999' });
ck('⑥ 合同不存在 -> CONTRACT_MISSING', v5.verdict === 'CONTRACT_MISSING');

// ---- ⑦ 空声明不生成合同（可选语义）----
const w2 = await freeze.execute({ dir, req_id: 'REQ-EMPTY', calls: [] });
ck('⑦ 空声明 -> 不生成合同（NO_CALLS_DECLARED）',
   w2.written === false && w2.reason === 'NO_CALLS_DECLARED');

// ---- ⑧ 重新冻结（必须解除**两个**文件的只读）----
const w3 = await freeze.execute({ dir, req_id: 'REQ-11', calls: CASE });
ck('⑧ 重新冻结成功（合同与侧车的只读都被解除）', w3.written === true);
const v6 = await verify.execute({ dir, req_id: 'REQ-11' });
ck('⑧b 重新冻结后校验回到 CONTRACT_OK', v6.verdict === 'CONTRACT_OK');

await rm(dir, { recursive: true, force: true });

const failed = RESULTS.filter(([, ok]) => !ok);
console.log(failed.length
  ? `\n❌ ${failed.length}/${RESULTS.length} 项失败`
  : `\n✅ 全部 ${RESULTS.length} 项通过`);
process.exit(failed.length ? 1 : 0);
