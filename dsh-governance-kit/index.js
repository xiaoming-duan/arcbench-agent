/**
 * dsh-governance-kit —— DSH 的**质量治理层**。
 *
 * ═══════════════════════════════════════════════════════════════════════
 *  与任务规格的两处**必要偏离**（都在此说明，不藏在代码里）
 * ═══════════════════════════════════════════════════════════════════════
 * ① 用 `index.js` 而非任务写的 `index.ts`
 *    DSH 的 Host-only bundle **不需要构建工具**（见 cordis-plugin-development
 *    的 host-plugin 参考："A Host-only bundle needs no dependencies, install
 *    scripts, or build tool"，导出 `apply`）。`install_bundle` 不跑 TypeScript
 *    构建，`.ts` 会得到一个无法加载的模块。
 * ② 用 `ctx.tools.register(definition)` 而非任务写的 `defineTool`
 *    这是本会话经 `cordis_inspect_query(Service, 'tools')` **实查**到的 API：
 *      register(definition: ToolDefinition): () => void
 *    任务里的 `defineTool` 在 DSH 中不存在，照抄会静默失效。
 *
 * ═══════════════════════════════════════════════════════════════════════
 *  本包交付什么
 * ═══════════════════════════════════════════════════════════════════════
 *  模块 1  UI 契约提取     skills/（SKILL.md，独立、无依赖）
 *  模块 2  契约冻结        本文件注册的两个 Tool
 *
 * 模块 3（分类型重试预算）**不在本版** —— 它依赖 Agent Loop 的钩子接口，
 * 需先问清接口再动。
 * ═══════════════════════════════════════════════════════════════════════
 */

import * as freeze from './tools/freeze_contract.js';
import * as verify from './tools/verify_contract.js';

/** 本插件需要 `tools` 服务；缺席时不激活（而非崩溃）。 */
export const inject = ['tools'];

/** 合法的三态判定名（供上层断言用，避免拼写漂移）。 */
export const VERDICTS = ['CONTRACT_MISSING', 'CONTRACT_TAMPERED', 'CONTRACT_MISMATCH', 'CONTRACT_OK'];

/** 把一个工具模块转成 ToolDefinition。 */
function toDefinition(mod) {
  return {
    name: mod.name,
    description: mod.description,
    parameters: mod.parameters,
    output: {
      // 输出 schema 声明成对象，具体字段随 verdict 变化
      schema: { type: 'object' },
      render: mod.render,
    },
    async execute(args) {
      return mod.execute(args);
    },
  };
}

/**
 * 插件入口。
 *
 * 每个资源都在 `apply` 内用 `ctx.effect` 注册并返回其清理函数 ——
 * 这是 DSH 对插件的要求：**不可在 apply 之外注册**，否则 HMR/卸载时泄漏。
 */
export function apply(ctx) {
  if (!ctx.tools) return;                       // 无 tools 服务 -> 不激活
  ctx.effect(() => ctx.tools.register(toDefinition(freeze)));
  ctx.effect(() => ctx.tools.register(toDefinition(verify)));
}

export default { name: 'dsh-governance-kit', inject, apply };
