# Hermes Agent 可扩展 Terminal Backend 架构计划

## 目标

让第三方 Hermes 插件能够注册自定义 terminal backend，并由 `terminal.backend` 选择它作为 terminal、file、process、code execution 等能力共享的底层运行环境；第三方无需修改 Hermes Agent 源码，也不需要额外暴露 tools。

本计划只定义扩展机制、运行时边界和迁移路径，不设计某个具体 backend 的实现，也不提前拆解到具体源码文件或逐行改动。

## 当前架构判断

### 现有 terminal backend 的关键约束

- `BaseEnvironment` 已经形成稳定的执行协议：backend 负责产生可轮询/终止的进程句柄并释放资源；基类统一处理 shell snapshot、cwd 延续、stdin、超时、中断和输出收集。
- backend 的选择和构造目前是集中式硬编码分支；新增 backend 不仅要加入构造分支，还会遇到散落在配置解析、容器路径判断、远端环境提示、审批策略和 UI 选项中的 backend 名称集合。
- terminal、file 和 code execution 共享同一批环境实例，但后两者通过导入 terminal 实现的内部缓存、锁和构造函数来复用环境，导致“创建环境”尚未成为一个独立、稳定的 host service。
- 环境实例按 task/session 维度创建并缓存，闲置后清理，进程退出时也会清理。因此插件注册的应当是 backend 定义或 factory，而不是一个全局 `BaseEnvironment` 实例。

### Specialized plugin types 的实现模式对比

| 类型 | 发现与注册模式 | 选择与生命周期 | 对 terminal backend 的启示 |
|---|---|---|---|
| Model provider | 延迟发现；模块向中心 registry 注册 profile；按名字/alias 查询 | profile 基本无运行时资源；允许覆盖 | 借鉴按名字查询、惰性发现和 metadata registry；不照搬宽松覆盖策略 |
| Gateway platform | `PluginContext` 注册 descriptor；descriptor 包含 factory、availability、配置/展示能力；支持 deferred loader | 按平台配置实例化 adapter；registry 不持有活动连接 | **最接近目标**：注册 descriptor/factory，而非实例；能力和 setup metadata 与 factory 放在同一扩展契约中 |
| Memory provider | 独立目录扫描和模拟 collector；配置选中唯一 provider | 单实例，具有 initialize/shutdown 和逐 turn 生命周期 | 借鉴“显式配置唯一选择”和完整生命周期；不新增另一套独立扫描器或源码文本启发式 |
| Context engine | 独立 loader，模拟 `PluginContext`；只允许一个活动 engine | 按配置加载单实例 | 说明专用 collector 容易与通用插件系统分叉；backend 应直接接入标准 `PluginContext` |
| Image-generation backend | 通用插件加载后向专用 registry 注册 provider 实例 | 由配置选择活动实例，简单 fallback | 借鉴专用 registry 和明确 fallback；不注册共享实例，因为 terminal environment 必须按 task 创建和回收 |

结论：采用 **Platform descriptor/factory + Context/Memory 的唯一配置选择 + Model/Platform 的惰性发现 + 现有 Environment 生命周期管理** 的混合方案。

## 目标架构

### 1. 建立 Terminal Backend Registry 作为唯一窄腰

引入 host-owned 的 terminal backend registry。registry 只保存不可变的 backend 定义，不保存活动环境。

每个 backend 定义至少表达：

- 稳定、规范化的 backend 名称和用户可读名称；
- 创建 `BaseEnvironment` 实例的 factory；
- 无副作用的 availability/dependency 检查；
- backend 的功能特征，用于 host 决策，而不是靠名字判断；
- 配置 schema、安装提示和诊断 metadata；
- 来源信息（built-in/plugin、所属插件），用于状态展示和冲突诊断。

registry 提供按名字查询、列举、诊断和测试重置能力。活动环境的创建、缓存和清理不进入 registry。

### 2. 通过标准 PluginContext 注册 backend

为标准插件上下文增加 terminal backend 注册能���。第三方插件在现有 `register(ctx)` 中提交 backend 定义；无需新的 entry-point group、目录扫描器或源码文本启发式。

加载语义：

- 用户安装的 backend 插件仍受现有 `plugins.enabled` 信任门控制；仅安装不执行代码。
- backend 查找前必须保证通用插件发现已经幂等完成，覆盖 CLI、gateway、cron、TUI/API 和直接 tool 调用等不同启动路径。
- 注册仅声明 descriptor/factory，不连接远端服务、不创建 sandbox，也不运行 backend 初始化。
- 插件卸载、禁用或配置变化不在进程内热切换已有环境；变化在下一次干净启动生效。测试可使用显式 reset。

`plugin.yaml` 继续使用通用 `kind: backend`。是否是 terminal backend 由插件实际调用的注册 API 决定，不再添加互相竞争的 manifest kind。

### 3. 内建 backend 与插件 backend 使用同一 registry

将现有内建 backend 以 host-owned definitions 注册到同一 registry，保留现有名称、默认值、构造行为和错误语义。

选择规则：

1. `terminal.backend` 显式配置时，只选择同名已注册 backend；未注册或不可用时返回精确错误，不静默切换到 local。
2. 未配置时继续默认 local，保持兼容。
3. backend 名称比较采用统一规范化规则，但 registry 中保留 canonical name 用于展示。
4. 重名默认拒绝，尤其禁止第三方静默覆盖内建 backend。若未来确需覆盖，必须通过单独、显式的高信任授权，而不是沿用 model provider 的 last-writer-wins。

### 4. 把环境实例管理提升为 host service

建立独立的 Environment Manager，作为 terminal、file、process、code execution 等调用方共享的唯一环境入口。它负责：

- 根据 task/session 解析有效 backend、cwd、timeout 和 task override；
- 在 per-task creation lock 下通过 registry factory 创建环境；
- 维护活动实例、last activity、闲置清理和进程退出清理；
- 在环境回收后通知依赖层失效 file-operation 等派生缓存；
- 为同一 task 的所有底层工具返回同一个环境实例；
- 将 factory 或初始化失败统一转换为带 backend/plugin 身份的可诊断错误。

这样 file/code execution 不再依赖 terminal tool 的私有全局变量和私有构造函数；插件 backend 只需满足环境契约，就自动服务所有依赖该环境的工具。

### 5. 定义稳定的 Factory Request，而不是不断扩张位置参数

host 向 factory 传入结构化 creation request，分为三层：

- 通用运行参数：backend name、task id、cwd、timeout、profile/Hermes home 等；
- host 已解析的通用 terminal 配置与 task override；
- 当前 backend 的 namespaced options。

建议把插件专属配置放在 `terminal.backends.<backend-name>` 下；通用字段仍保留在 `terminal` 顶层。这样新增 backend 不需要把 vendor-specific key 加入 core 的 env-var bridge 或硬编码解析表。

配置原则：

- factory 收到的是当前 backend 的配置切片，而不是整个 Hermes 配置；
- secret 继续通过 Hermes 的 secret/env 机制提供，不复制到日志或状态输出；
- backend definition 的 schema/availability metadata 驱动 setup、dashboard picker、status 和错误提示；
- 显式配置但缺依赖/凭据时不 fallback，以便用户得到精确修复建议。

### 6. 用能力模型替换 backend 名称集合

需要把 core 中“如果 backend 名字属于某集合”的判断逐步转为 registry capability。至少覆盖：

- execution locality：host-local 或 remote；
- filesystem/path semantics：host filesystem、isolated POSIX filesystem、shared/mounted host filesystem；
- cwd policy：是否接受 host cwd、是否需要 sandbox cwd 映射；
- image/resource model：是否接受 image、CPU、memory、disk 等通用 sandbox 参数；
- process/PTY/file-transfer 等可选能力；
- persistence/reuse 特征和 host-access 状态。

能力值用于 prompt、路径转换、配置展示和工具适配，但不能让插件自行扩大安全权限。

### 7. 安全边界必须由 host 掌控

Terminal backend 是比普通 provider 更高风险的扩展点：factory 运行于 Hermes 进程内，且 backend 承载命令与文件访问。

因此：

- 用户插件必须先显式 enable，安装不等于信任。
- 自定义 backend 默认采用保守安全策略：视为可能访问 host，危险命令审批保持开启。
- 插件声明“isolated”只能用于展示或兼容路径行为，不能单独触发跳过审批。
- 跳过审批、允许 host mount、覆盖内建 backend 等安全敏感能力必须由 host 可验证或由用户显式策略授权。
- registry 在注册时验证名称、factory 可调用性和 definition 结构；factory 返回值必须是 `BaseEnvironment` 实例，否则拒绝进入活动缓存。
- 插件异常隔离在 discovery/registration/factory 边界并记录来源；不得导致 fallback 到更危险的 local backend。

### 8. 生命周期契约

保持 `BaseEnvironment` 为运行时核心协议，并明确以下阶段：

1. **Discovery**：加载已启用插件并注册 backend definition；无 backend 资源副作用。
2. **Resolution**：读取 `terminal.backend`，查询 definition，检查 availability 与配置。
3. **Creation**：Environment Manager 在 per-task 锁内调用 factory；factory 返回已可使用的 `BaseEnvironment`。
4. **Use**：terminal/file/process/code execution 共享同一环境，继续由 BaseEnvironment 统一执行语义。
5. **Cleanup**：Environment Manager 在 idle、显式 session teardown 和 process exit 时恰好调用一次 `cleanup()`；失败只记录，不阻塞其他环境回收。

现有 backend 可继续在构造阶段完成连接、同步和 `init_session()`。长期可再将初始化显式化，但不是开放第三方注册的前置条件，以避免本次架构改动同时重写全部 backend 生命周期。

## 渐进迁移与 `EXP_BACKEND` 双路径

迁移架构分为两层：

- **迁移期 façade/router**：根据 `EXP_BACKEND` 选择 legacy 或新 runtime；重构完成后删除。
- **永久的 Registry + Environment Manager**：承载插件扩展、环境生命周期以及多工具共享环境；全量切换后继续保留。

最终切换只删除 feature-flag router 和 legacy adapter。terminal、file、process、code execution 最终直接依赖 Registry/Environment Manager 提供的新 API。

### 单一迁移 Façade

所有 backend 相关调用逐步收敛到一个 host-owned runtime façade。它表达领域操作，而不把旧实现的内部 dict、lock 或私有 factory 原样公开。最小职责包括：

- 获取或创建某个 task 的环境；
- 查询有效 backend identity、活动环境和 host-trusted capabilities；
- 注册、读取和清理 task override；
- 标记活动、执行 task cleanup 和全局 cleanup；
- 让 file-operation 等派生缓存随环境生命周期失效；
- 提供 status/diagnostics 所需的只读 snapshot。

迁移期提供两个实现：

- **Legacy runtime adapter**：薄封装现有函数、缓存和锁，不重写行为；`EXP_BACKEND` 未开启时使用。
- **Registry runtime adapter**：使用新 Registry、Factory Request、Environment Manager 和 capability 模型；仅 `EXP_BACKEND=1` 时使用。

调用方不能直接读取“当前选择了哪个 adapter”并自行分支，也不能绕过 façade 回到 legacy globals。feature flag 只能存在于 composition root，不能散落到 terminal/file/process/code execution 各处。

### Flag 语义

- `EXP_BACKEND=1`：整个进程使用 registry runtime。
- 未设置或值为 `0`：整个进程使用 legacy runtime。
- 其他值：记录一次清晰警告并按 legacy 处理，避免拼写错误意外启用实验执行路径。
- runtime 在进程首次初始化时读取并冻结；后续修改环境变量不会切换已运行进程，也不会让同一进程出现 legacy/new 混用。
- 启动日志、status 和 dump 应明确显示当前是 `legacy` 还是 `experimental-registry` runtime，便于反馈和回滚诊断。
- `EXP_BACKEND=1` 只控制 host runtime，不改变 `terminal.backend` 的取值；后者仍负责选择 local/docker/第三方 backend。

迁移期间，第三方注册的 terminal backend 仅在 `EXP_BACKEND=1` 下可用。legacy runtime 收到第三方 backend 名称时应沿用 legacy 的 unknown-backend 失败语义，并附加提示“第三方 backend 需要 EXP_BACKEND=1”，不得 fallback 到 local。

### 验证策略（不做双执行）

terminal backend 会创建容器/远端 sandbox、执行命令和写文件。迁移验证不同时执行 legacy/new 路径，避免重复副作用、资源泄漏和安全风险。

验证方式：

- 同一组无副作用 contract tests 分别运行在两个 runtime；
- 使用 fake backend 对创建、缓存、cleanup 和 capability 决策做对照；
- 对真实 backend 使用独立测试进程和隔离 workspace；
- 只比较解析结果、diagnostic snapshot 等纯 metadata，不 shadow 执行用户命令。

### 兼容与回滚边界

- legacy 路径在迁移期进入功能冻结：除必要 bug/security fix 外不做结构性演进；必须修复时评估是否同步到新 runtime。
- 新路径产生的活动环境不能被 legacy 路径接管；回滚需要重启进程并取消 `EXP_BACKEND=1`，由各自进程正常 cleanup。
- 两个 runtime 必须继续使用相同的 backend 外部配置语义、task id 和 cwd/session 记录格式，避免切换后用户可见行为漂移。
- Registry runtime 的私有 checkpoint/cache 格式不得被声明为长期兼容接口；迁移期不承诺跨 runtime 复用活动实例。
- 默认路径始终保持 legacy，直到迁移验收门全部满足；不能在重构中途悄悄改变默认值。

## 实施阶段

### 阶段 A：建立 Legacy Characterization 基线

- 盘点所有直接读取 terminal backend 私有状态、调用私有 factory 或复制创建流程的调用方。
- 用 characterization tests 固化现有 backend 选择、task override、并发首次创建、cwd 延续、file/code 共用、idle cleanup 和全局 cleanup 行为。
- 记录 legacy 可观察语义和已知缺陷；测试应保护行为兼容，而不是保护内部 dict/lock 结构。

### 阶段 B：先引入 Façade，但只接 Legacy Runtime

- 定义迁移 façade 的最小领域 API，并用 legacy adapter 委托现有实现。
- 逐个把 terminal、file、process、code execution、cleanup、task override 和 status 调用迁到 façade。
- 此阶段无论 `EXP_BACKEND` 取值都只能返回 legacy adapter，确保单独引入边界不会改变生产行为。
- 增加架构守卫测试，阻止新调用方继续导入 legacy globals/private factory。

### 阶段 C：冻结 Feature Flag 与双路径 Composition

- 在唯一 composition root 读取一次 `EXP_BACKEND`，选择 legacy 或 registry adapter，并记录 runtime mode。
- 建立双模式 contract-test harness；默认、不合法 flag、`0` 和 `1` 都有明确测试。
- 验证进程内修改环境变量不会切换 runtime，且不存在按调用动态分流。

### 阶段 D：实现 Registry Runtime 骨架

- 定义 backend definition、factory request、capability 和 availability/error 契约。
- 建立 terminal backend registry、Environment Manager 和 `PluginContext` 注册入口。
- 用 fake backend 固化“注册不实例化”“显式错误不 fallback”“并发只创建一次”“factory 必须返回 BaseEnvironment”“cleanup 恰好一次”。
- 将插件发现时序接到 backend resolution 前，确保 CLI、gateway、cron、TUI/API 和直接 tool 调用一致。

### 阶段 E：逐个迁移内建 Backend 到新路径

- 先迁移 local，建立最小端到端 experimental path；随后按依赖复杂度迁移其他内建 backend。
- 每迁移一个 backend，都在 `EXP_BACKEND=0` 与 `EXP_BACKEND=1` 下运行相同 characterization/contract 场景。
- 保留原有名称、默认值、连接、持久化、cwd、task override、cleanup 和用户可见错误语义。
- legacy 硬编码 factory 在此阶段继续存在，只服务 `EXP_BACKEND!=1`。

### 阶段 F：能力模型与外围消费者切换

- façade 暴露统一 capability 查询；legacy adapter 从已有名称/配置合成等价 capability，新 adapter 读取 registry definition。
- 将 remote/container/path/prompt/approval 等外围判断迁到 façade capability，避免外围代码关心 runtime mode。
- 安全 capability 仍由 host policy 决定，不能因为 experimental path 或插件声明而放宽审批。
- 让 setup、dashboard、status、dump 和诊断界面动态展示 experimental registry，同时明确当前 runtime mode。

### 阶段 G：插件配置与第三方端到端验证

- 支持 namespaced backend options，并确保不同启动面获得相同有效配置。
- 使用最小外部插件验证：插件只注册 backend，不注册 tool/hook；`EXP_BACKEND=1` 时 terminal、file 和 code execution 使用同一插件环境实例。
- 验证 flag 未开时第三方 backend 明确失败并提示开启实验模式，不 fallback local。
- 覆盖插件未启用、名字冲突、缺依赖、factory 抛错、错误返回类型和 cleanup 抛错。

### 阶段 H：扩大 Experimental 覆盖并设定切换门槛

- CI 的 backend 相关测试同时运行 legacy/new；完整测试套件至少保留一个 experimental job。
- 在开发者、可控 gateway/cron 和真实内建 backend 集成环境逐步启用 `EXP_BACKEND=1`。
- 切换门槛：全部内建 backend 已迁移；直接私有导入已清零；双模式 contract 通过；安全审批、cleanup 和并发行为等价；第三方插件端到端通过；有明确回滚记录。
- 在满足门槛前，默认值保持 legacy。

### 阶段 I：全量切换并删除迁移层

- 先让目标部署在一个发布周期内统一显式设置 `EXP_BACKEND=1`，但代码默认仍保持 legacy，以便取消环境变量即可回滚；观察真实运行和资源 cleanup 指标。
- 通过最终切换门槛后，在一个明确版本中直接删除 legacy 路径并让新 runtime 成为唯一实现，不在同一版本内引入“flag 默认值翻转”这个额外过渡状态。
- 稳定后删除 `EXP_BACKEND` router、legacy adapter、legacy 硬编码 factory 和仅服务旧路径的兼容状态。
- 调用方直接依赖永久的 Registry/Environment Manager API，不重新引入 backend-specific 分支。
- 删除双模式测试矩阵，保留新 runtime contract、所有内建 backend 回归和第三方插件测试。
- 更新 Specialized plugin types 文档并移除“实验模式”说明。

## 验证矩阵

### Registry / discovery

- 已启用的 entry-point 插件可注册 backend；未启用插件不会执行注册代码。
- 不同启动面在首次 backend resolution 前都完成幂等 discovery。
- 重名、非法名称、错误 factory、非 BaseEnvironment 返回值均产生确定性诊断。

### Selection / config

- 未配置时选择 local。
- 显式选择内建或插件 backend 时准确命中。
- 显式选择未知/不可用 backend 时失败且不 fallback。
- `terminal.backends.<name>` 只传给对应 factory，secret 不进入日志。

### Shared runtime

- 同 task 的 terminal、file、process、code execution 共享同一实例。
- 不同 task 保持现有隔离/复用规则。
- 并发首次调用只创建一个实例。
- idle cleanup、session teardown、process exit 都不会重复 cleanup；清理后再次调用可正确重建并恢复 cwd。

### Dual-runtime migration

- `EXP_BACKEND` 未设置、为 `0` 或为非法值时只进入 legacy；为 `1` 时只进入 registry runtime。
- runtime mode 在首次初始化后冻结；同一进程不会产生 legacy/new 两套活动环境。
- façade contract 在两个 runtime 下运行相同场景并对齐可观察结果；涉及命令/写文件的场景使用不同隔离进程，不做双执行。
- flag 未开启时，现有全部 backend 和配置行为保持原样；第三方 backend 明确报实验模式要求且不 fallback。
- status/dump/log 能定位当前 runtime mode、effective backend 和 backend source。

### Capabilities / security

- remote/path/prompt 行为由 capability 决定，不依赖第三方名字进入硬编码集合。
- 自定义 backend 即使声明 isolated，也不会自动绕过危险命令审批。
- 显式配置失败不会意外降级到 local。
- 内建 backend 的现有审批和 host mount 行为保持不变。

### 回归验证命令

实现阶段应至少执行：

```bash
EXP_BACKEND=0 uv run pytest -q tests/tools tests/hermes_cli
EXP_BACKEND=1 uv run pytest -q tests/tools tests/hermes_cli
EXP_BACKEND=0 uv run pytest -q
EXP_BACKEND=1 uv run pytest -q
```

完整套件双跑成本过高时，PR 必须双跑 backend contract/工具测试，并至少由一个 CI job 完整运行 `EXP_BACKEND=1` 套件；legacy 完整套件仍是默认 required check，直至最终切换。依赖外部凭据或运行时的 backend 集成测试应继续受 integration marker/显式环境变量控制，不能让默认测试依赖 Docker、SSH、Modal、Daytona 或第三方服务。

## 主要风险与对策

- **插件发现过晚**：Environment Manager 在首次 resolution 前显式触发幂等 discovery，而不是假设某个 CLI import 已完成。
- **周边仍按名字分支**：建立 capability 使用清单，并用一个未知名称的测试 backend 验证端到端，避免测试仅覆盖内建名字。
- **插件 metadata 伪造隔离性**：安全决策不直接信任插件声明；默认保守，放宽必须由 host/user policy 授权。
- **创建逻辑仍被多个工具复制**：先收敛 Environment Manager，再开放稳定插件契约，避免第三方被迫依赖私有 terminal 状态。
- **配置 schema 无限扩张 core**：插件专属字段 namespaced，并由 definition 提供 schema/setup metadata。
- **覆盖内建 backend 导致供应链风险**：重复名字默认拒绝；将覆盖设计成单独的高信任能力，而非 registry 常规行为。
- **插件加载失败导致危险 fallback**：显式选择的 backend 失败即失败，绝不回退 local。
- **feature flag 分支散落**：只允许 composition root 读取 `EXP_BACKEND`；用架构测试阻止业务调用方直接检查该变量。
- **同进程混用两个 runtime**：runtime 选择首次初始化后冻结；不支持运行中切换，回滚必须重启。
- **Legacy adapter 变成永久包袱**：为每阶段设置直接私有导入清零和最终删除门槛；新功能只落到目标 runtime，legacy 除 bug/security fix 外冻结。
- **双路径测试成本和行为漂移**：共用 runtime contract suite，真实副作用场景分进程执行；每迁移一个内建 backend 即建立 parity，不等到最后统一对账。
- **为了比较而双执行命令**：明确禁止 shadow execution，仅对纯解析/metadata 做同输入比较。

## 非目标

- 不改变 terminal/file/process/code execution 的 tool schema。
- 不在本次工作中实现 Coder 或其他具体 backend。
- 不要求 backend 插件暴露额外 tools、hooks 或 slash commands。
- 不重写 BaseEnvironment 已有的 shell snapshot/执行协议。
- 不支持运行中热卸载或无损切换已有环境。
- 不支持运行中修改 `EXP_BACKEND` 后热切换 runtime。
- 不长期保留 `EXP_BACKEND`、迁移 façade 或 legacy adapter；它们只服务渐进重构。
- 不把第三方 backend 隔离到独立进程；Python 插件仍是受信任的进程内扩展，安全依赖显式 enable 和保守 host policy。
