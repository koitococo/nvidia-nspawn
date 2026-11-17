# nvidia-nspawn：运行中容器 NVIDIA 设备注入规格

## 1. 目标

开发宿主机侧工具，为一个**已经运行的 systemd-nspawn 容器**注入 NVIDIA GPU 访问能力，并在不重启容器的前提下完成以下真实验证：

1. 容器内普通用户可以运行 `nvidia-smi`，看到选定的 GPU。
2. 容器内普通用户可以运行 NVIDIA CUDA Samples 的 `vectorAdd`，通过 GPU 完成计算并验证结果。

首次目标环境为 Fedora 45 宿主机、NVIDIA 专有驱动、cgroup v2，以及 Debian 系 nspawn 容器。具体驱动、Toolkit、systemd、CUDA 版本在实现前盘点并记录，不在本规格中假定。

本规格是待实现的契约，不代表已经完成兼容性验证。

## 2. 范围与非目标

### 本次范围

- 从宿主机识别运行中的目标容器、容器 leader PID、挂载命名空间和实际 cgroup。
- 复用 NVIDIA Container Toolkit 的 CDI 发现结果，选择并注入 GPU 设备及所需用户态驱动组件。
- 对运行中的设备访问策略进行增量放行，不重启容器。
- 处理设备节点权限、动态链接器配置及本场景需要的 CDI hooks。
- 提供计划预览、应用、状态检查和真实验证命令。
- 明确处理宿主机与容器发行版不同带来的驱动库路径问题。

### 非目标

- GPU 图形桌面、Xorg、Wayland、VirtualGL、VNC、NVENC 或视频解码。
- 在容器内安装 NVIDIA 内核模块。
- 自动安装或升级宿主机驱动、CUDA Toolkit、系统软件包。
- 持久化到容器重启后、开机自动注入、驱动升级热更新。
- GPU 热拔插、MIG 配置、MPS、vGPU、嵌套容器 GPU 分发。
- 不可信租户之间的强 GPU 隔离或 GPU 资源配额。
- 自动卸载运行中应用正在使用的 GPU 驱动组件。

## 3. 关键约束

### 3.1 必须是真正的运行时注入

- 注入前后，容器 leader PID 保持不变。
- 不通过停止容器、修改启动配置后重启来满足需求。
- 已运行的普通进程不被结束；注入后启动的新进程可以使用 GPU。
- 不承诺让已经加载了旧驱动库或缓存了设备状态的进程自动获得 GPU 能力。
- 仅修改 `.nspawn` 文件或 systemd drop-in 不构成实现完成；这些配置不能代替向现有挂载命名空间注入设备和库。

### 3.2 在宿主机执行

工具以宿主机管理员权限运行，通过 systemd/machined 获取目标信息，不依赖容器内安装特权守护进程。权限不足时在变更前失败，不降级为关闭隔离或重启容器。

支持独立用户命名空间与无用户映射的 nspawn 容器。独立用户命名空间的 UID/GID 映射不得包含宿主机 ID 0。无用户映射目标的程序和 createContainer hooks 必须经容器自身 systemd transient service 执行；宿主工作器仅进行受控文件、挂载操作，不直接 exec 容器程序。guest systemd 或系统总线不可用时，在注入变更前拒绝，不降级为继承宿主 capabilities 的执行方式。超时必须停止并核实整个 guest service，而不只终止宿主客户端。

### 3.3 不扩大无关权限

- 只开放选定 GPU 及计算、管理查询所需的共享设备。
- 不绑定整个宿主机 `/dev` 或驱动库目录。
- 不使用 `chmod 666`、关闭 SELinux、关闭用户命名空间或放开全部设备访问作为通用解决办法。
- 保留原有设备策略及允许项，不覆盖或清空用户配置。
- 设备可见性、cgroup 许可、UID/GID/DAC 和 SELinux 分别验证。

## 4. 建议接口

以下为 CLI 行为契约，名称可在实现时统一调整，但能力不可省略。

```text
nvidia-nspawn plan   --machine NAME --gpu GPU-UUID
nvidia-nspawn attach --machine NAME --gpu GPU-UUID
nvidia-nspawn status --machine NAME
nvidia-nspawn verify --machine NAME --user USER --cuda-sample /path/in/container/vectorAdd
```

- `--machine` 必填，目标必须是正在运行的 nspawn 容器。
- 首次版本一次选择一张完整 GPU，使用 UUID，避免依赖不稳定的索引。
- `--user` 指定容器内已有普通用户；验证不能仅依赖容器 root 成功。
- `--cuda-sample` 是容器内已准备好的真实 CUDA Sample 可执行文件路径。
- `plan` 不修改容器、cgroup、设备权限或动态链接器配置。
- `attach` 完成注入；重复执行相同请求不重复堆叠挂载、不累加配置。
- 已有不同 GPU 的注入状态时，首次版本拒绝替换并说明现状，不隐式卸载或扩展。
- `status` 报告实际挂载、设备和注入实例，不仅回显保存的配置。
- `verify` 不负责安装依赖；缺失项作为失败报告，不伪造或跳过验证。
- 所有命令使用明确的成功/失败退出状态；输出包含失败阶段、目标资源和原始错误。

## 5. 实现模型

### 5.1 目标盘点与变更计划

在任何修改前，确认并输出：

- 容器名称、leader PID、root、挂载命名空间、宿主机 unit 和 cgroup。
- GPU UUID、设备节点及实际 major/minor。
- 主机 NVIDIA 内核驱动和用户态驱动版本。
- 用户命名空间映射、验证用户的 UID/GID 和所需设备访问权限。
- CDI 来源、版本、选择的设备以及公共/设备级 edits。
- 所需挂载、链接、动态链接器配置、hooks、设备许可变更。
- 容器已有 NVIDIA 组件及冲突情况。

在应用前重新确认目标仍为同一运行实例；目标已退出或重启则中止，不能把旧计划应用到另一个实例。

### 5.2 驱动发现：复用 CDI

- 在宿主机使用 `nvidia-ctk` 获取当前驱动对应的 CDI 描述，避免硬编码驱动文件清单。
- 合并顶层与选定 GPU 的 `containerEdits`，处理相关的 mounts、deviceNodes、env 和 hooks。
- 不能只挂载 `nvidia-smi` 和 `/dev/nvidia0`，也不能忽略共享控制节点、UVM 或 NVML/CUDA 所需库。
- 不硬编码 UVM major 或假设所有驱动文件都位于同一目录。
- 选定 CDI 中存在无法处理的必要 edit/hook 时，在变更前报告不支持，不能静默丢弃。
- 若 CDI 包含本次计算场景以外的图形组件，明确说明采用完整 edits 还是经验证的计算子集；不得凭文件名随意裁剪依赖。

### 5.3 注入挂载与驱动库

- 在目标容器现有挂载命名空间中注入设备和文件，不把临时挂载传播到宿主机或其他容器。
- 驱动库和工具只读挂载；不修改宿主机驱动文件。
- 只读和必要安全属性在 detached mount FD 上完成，再公开到目标；不允许先公开可写挂载再收紧权限。
- 保留容器自己的 glibc、动态链接器和通用系统库。
- 处理 Fedora 与 Debian 的路径差异，确保 SONAME、符号链接以及动态链接器搜索路径正确。
- 容器内 `nvidia-smi` 必须在正常登录/执行环境中可用，不能仅在工具临时设置的 `LD_LIBRARY_PATH` 中成功。
- 必要的容器侧配置仅写入本工具管理的独立文件；发现冲突时报告，不覆盖用户已有配置。
- 容器 rootfs 在停止后仍可能保留配置文件，但运行时挂载和 GPU 授权不承诺在重启后存在；不得据此宣称持久注入。

### 5.4 cgroup 与设备权限

- 通过 systemd 支持的运行时属性接口，对实际承载容器的宿主机 unit 增量开放设备访问，由 systemd 管理 cgroup v2 设备过滤。
- 不写 cgroup v1 的 `devices.allow`，不直接操作 systemd 管理的 BPF 程序。
- 保留现有设备策略；若现有策略已允许相关设备，不做无意义改写。
- 检查祖先 cgroup 及实际执行验证命令的子 cgroup；祖先禁止时不能靠子级允许绕过。
- 设备 DAC 权限通过现有映射、组或受控的容器设备节点处理；如必须变更宿主机 ACL，计划中明确列出目标与影响，不能隐式修改共享设备权限。
- Fedora SELinux 拒绝时报告实际拒绝信息；需要额外策略而本次未配置时明确失败，不关闭 enforcing。

### 5.5 CDI hooks 与执行环境

- NVIDIA CDI hooks 原本依赖运行时上下文，不直接把 OCI hook 命令当作普通宿主机命令执行。
- 为本场景涉及的 hooks 提供正确的目标 root、namespace 和参数；需要适配的行为逐项实现并记录。
- 符号链接与动态链接器缓存更新必须作用于目标容器，不得误改宿主机。
- 不把 `Environment=` 或 PID 1 的环境修改视为所有现有会话可见；依赖环境变量的配置必须覆盖新启动的验证进程，并说明普通使用方式。

## 6. CUDA 验证前提

驱动注入与 CUDA 开发环境分开管理。

- 宿主机已经加载可用的 NVIDIA 驱动。
- 容器内提前准备 NVIDIA CUDA Samples 的 `vectorAdd`，以及该构建需要的 CUDA runtime 依赖。
- 不要求 CUDA 编译器长期安装在目标容器中，可以预先构建后提供可执行文件和必要依赖。
- Sample 的 CUDA 版本和 GPU 架构必须与宿主机驱动、实际 GPU 兼容；验收记录源码版本、构建版本与编译目标。
- `nvidia-smi` 显示的 CUDA 版本不能作为容器已安装 CUDA Toolkit 或 Sample 可运行的证据。

## 7. 验收标准

在同一个真实运行中的目标容器完成以下流程：

| 阶段 | 必须观察到的结果 |
|---|---|
| 注入前 | 记录 leader PID；确认目标尚不能使用本次 GPU，并记录设备/驱动现状 |
| 计划 | `plan` 列出资源和权限变更，且不改变运行状态 |
| 注入 | `attach` 成功，容器 leader PID 不变，原有进程继续运行 |
| 管理查询 | 以指定普通用户运行 `nvidia-smi -L` 和查询命令，返回选定 GPU UUID 与驱动信息 |
| CUDA 计算 | 同一普通用户运行 `vectorAdd`，进程成功退出，程序报告结果校验通过 |
| 驱动解析 | NVML、CUDA driver 等动态加载组件来自注入的匹配驱动，而非已有冲突版本 |
| 重复应用 | 相同 `attach` 再次执行不增加重复挂载或配置，随后真实验证仍成功 |
| 状态 | `status` 能区分当前运行实例的实际注入与过期记录 |

额外记录：容器与宿主机系统版本、systemd/Toolkit/驱动版本、GPU 型号与 UUID、用户命名空间模式、SELinux 状态，以及完整验证命令和退出状态。

`nvidia-smi` 成功不等于 CUDA 验收成功；不能用 mock、仅枚举设备或打印预设成功消息代替 `vectorAdd` 的真实 GPU 运算。

## 8. 失败行为与生命周期

- 可预见的不支持配置、依赖缺失、驱动冲突在变更前拒绝。
- 应用中途失败时返回失败，报告本次已完成的挂载、权限和文件变更；能安全撤销且由本次创建的资源应清理，不删除预先存在的用户资源。
- 超时先终止并回收工作进程及后代，再观察和清理。逐条进度记录恢复中断前已完成的变更；无法确认归属的残留仅报告，不宣称文件操作和日志记录具有原子性。
- 不承诺跨重启或驱动升级保持有效。容器重启后需要重新注入；驱动更新后停止继续验证并要求重新规划，不能混用新旧驱动。
- 不自动重启容器或宿主机，不重载正在使用的 NVIDIA 内核模块。
- 不配置 `ExecStartPost` 或启动自动注入。卸载 RPM 不会 detach 或回收 GPU 权限，容器资源、宿主运行记录和 systemd 运行时授权可以保留；管理员必须另行安排权限回收。
- 如果挂载命名空间、systemd 运行时设备策略、用户映射或 SELinux 无法按本规格安全支持，输出具体阻塞点；不将重启式方案伪装为运行时支持。

## 9. 实现前必须验证的技术点

以下为实现门槛，不是已验证结论：

1. 当前 systemd 版本能否对目标 unit 在线增量更新设备许可，且不改变其余设备访问行为。
2. 当前 nspawn 的 mount propagation 和用户命名空间模式是否允许宿主机向现有容器注入所需挂载。
3. 当前 NVIDIA CDI 的必要 hooks 如何在非 OCI、已运行容器中正确执行。
4. Fedora 用户态驱动与 Debian 容器 ABI 是否兼容，以及需要哪些动态链接器适配。
5. 普通用户的设备 DAC 与 SELinux 访问是否成立。

应先用一张 GPU、一个隔离 nspawn、一次真实 `nvidia-smi` 和 `vectorAdd` 验证这些门槛，再固化 CLI 实现。不以完整 OCI/CDI 通用运行时为本项目目标。

## 10. 参考

- NVIDIA Container Toolkit CDI：<https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/cdi-support.html>
- NVIDIA CDI hooks：<https://github.com/NVIDIA/nvidia-container-toolkit/blob/main/cmd/nvidia-cdi-hook/README.md>
- systemd-nspawn 配置：<https://www.freedesktop.org/software/systemd/man/latest/systemd.nspawn.html>
- systemd 设备资源控制：<https://www.freedesktop.org/software/systemd/man/latest/systemd.resource-control.html>
- NVIDIA CUDA Samples：<https://github.com/NVIDIA/cuda-samples>
