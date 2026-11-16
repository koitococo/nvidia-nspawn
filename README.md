# nvidia-nspawn

为**已经运行的 systemd-nspawn 容器**注入 NVIDIA 设备和用户态驱动。宿主机侧 Python CLI，复用 NVIDIA Container Toolkit 的 CDI 描述；不重启容器，不安装容器内核驱动。

需求契约见 [SPEC.md](SPEC.md)。

## 当前验证状态

已实现 `plan`、`attach`、`status`、`verify`，本地 **35 个算法测试通过**，并完成专用 GPU VM 的在线注入、普通用户 CUDA、重复注入和设备拒绝负向验收。

本次实测宿主机是 **openSUSE Leap 16.0，不是 Fedora**；SELinux 原有状态为 permissive，未修改。Fedora 45 / SELinux enforcing 尚待独立验收。

## 前提

宿主机：

- Linux、cgroup v2、运行中的 systemd-nspawn 容器，以及提供 `open_tree`、`move_mount`、`mount_setattr` 的内核和 glibc。
- Python 3.11+、`machinectl`、`busctl`。
- NVIDIA 专有驱动已经加载，宿主机 `nvidia-smi` 正常。
- NVIDIA Container Toolkit，提供 `nvidia-ctk cdi generate --format=json --device-name-strategy=uuid` 及生成描述所引用的 hooks。
- 启用 SELinux 时，需要 `libselinux.so.1`。工具不关闭 SELinux。
- 以宿主机 root 执行，而非在目标容器里执行。

容器：

- 容器中有 `ldconfig`、`setpriv`、`id`、`readlink`，以及已有普通用户。
- 尚未安装或注入冲突的 NVIDIA 驱动文件。
- 已准备真实 NVIDIA CUDA Samples `vectorAdd` 可执行文件及其 runtime 依赖，CUDA 版本与 GPU/宿主驱动兼容。
- CUDA 编译器无需长期留在容器中。`nvidia-smi` 的 CUDA version 字段不代表已安装 CUDA Toolkit。

首次验收建议使用只有一张可见 GPU 的专用 VM 和可丢弃容器，保留实际 SELinux、用户命名空间配置，不通过关闭隔离来制造成功。

## 使用

可以在源码目录直接运行，不需要安装 Python 依赖：

```bash
python3 -m nvidia_nspawn --help
```

也可以通过 Python 包管理工具安装项目，获得 `nvidia-nspawn` 命令。构建使用 setuptools；运行时仅使用 Python 标准库和上述宿主机工具。

以下命令在宿主机源码目录运行。替换容器名、完整 GPU UUID、普通用户名和容器内 Sample 路径：

```bash
sudo python3 -m nvidia_nspawn plan \
  --machine gpu-test --gpu GPU-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx

sudo python3 -m nvidia_nspawn attach \
  --machine gpu-test --gpu GPU-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx

sudo python3 -m nvidia_nspawn status --machine gpu-test

sudo python3 -m nvidia_nspawn verify \
  --machine gpu-test --user developer \
  --cuda-sample /opt/cuda-samples/vectorAdd
```

正常结果为 JSON，错误写入 stderr 并返回非零。`status` 的 JSON 中 `healthy` 表示记录的挂载、节点和驱动匹配，不代替 GPU 功能验证；必须执行 `verify`。

### plan

只读盘点并生成计划，包括容器运行实例、cgroup、UID/GID 映射、设备节点、驱动文件、hooks 和设备授权。发现冲突文件、会向宿主机传播的共享挂载或无法解释的 CDI edit 时拒绝继续；nspawn 内部独立的 shared peer group 不作为宿主机传播风险。

不生成持久化 `.nspawn` 或 systemd drop-in。

### attach

- 使用 `open_tree` 在宿主机克隆 detached mount，再用 `move_mount` 注入目标现有挂载命名空间；支持 CDI `rbind`，递归设置只读等属性，保留容器自身 glibc 和动态链接器。
- 在容器文件系统中创建设备节点，使用 CDI 指定的权限和经 UID/GID 映射的所有者，不修改宿主机设备 chmod/ACL。
- 通过 systemd D-Bus 运行时更新 `DeviceAllow`，保留既有规则。非空数组在该接口中是追加语义，因此在同一请求内先清空再设置完整合并列表，确保失败回滚也能撤销新增许可。若 `DevicePolicy=auto` 且允许列表为空，不添加首条规则而意外收紧策略。
- 适配 CDI 符号链接与动态链接器 hooks，配置使用 `/etc/ld.so.conf.d/nvidia-nspawn.conf`，执行容器自己的 `ldconfig`。
- `createRuntime` hooks 在宿主机执行；其他支持的 `createContainer` hooks 通过预先打开的可执行文件描述符在目标挂载/PID 命名空间执行，提供 bundle、目标 root 和容器状态，不要求容器安装 Toolkit。支持 Toolkit 1.20.2 的内存限制 hook，但本项目不配置 GPU 内存配额，并显式拒绝相应配额环境变量。
- 用 `/run/nvidia-nspawn/<machine>.json` 记录运行实例、资源指纹、变更 journal 和失败信息。

同一运行实例、同一 GPU 的成功注入可以重复执行，不叠加挂载。切换 GPU、失败后的残留状态和已有驱动冲突不自动覆盖。

### verify

以指定普通用户执行：

1. `id`，确认不是 UID 0。
2. `nvidia-smi -L` 及 UUID/驱动版本查询。
3. 真实 `vectorAdd`，要求成功退出且计算结果校验通过。
4. 用 glibc `LD_DEBUG=libs` 的实际初始化记录和容器路径解析，核对 NVML/libcuda 来自注入的驱动，而不是仅检查 `ldd` 或库搜索候选。
5. 前后核对容器运行实例及宿主机驱动。

验证子进程进入目标 cgroup 和 namespaces，并在启用 SELinux 时设置目标执行标签，避免宿主机权限绕过造成假阳性。CDI 环境变量及 `additionalGids` 只应用于工具启动的验证进程，不修改现有 shell、用户的持久组成员或宿主机设备权限。正常使用 `nvidia-smi` 不依赖临时 `LD_LIBRARY_PATH`；从其他入口启动应用时，管理员仍需为该用户或服务配置对应 GPU 组。

支持已核实的 `vectorAdd` 输出契约，包括历史版本的 `Test PASSED` 和新版统一内存示例的结果匹配标记。源码版本、构建 CUDA 版本、架构参数需由 Sample 提供者另行记录；输出标记不能证明任意二进制的来源。

## 生命周期和失败处理

- 不自动重启容器或重载宿主驱动。
- 运行时挂载不承诺跨容器重启存在，容器文件系统上的配置和占位文件可能保留。重启后先检查 `status` 与新计划；遇到旧的托管文件冲突，人工核实并清理后再注入，不强行覆盖。
- 驱动升级不做热更新，旧驱动版本记录会导致健康检查或验证失败。
- 失败时尝试撤销本次创建的挂载、节点、配置和设备授权，保留失败 journal；不删除原先已有的用户文件。
- 若清理失败、容器实例变化或挂载仍在使用，返回原始错误及残留信息，不假称已回滚。
- `ldconfig` 更新的缓存属于派生数据，回滚后重新运行；不把缓存内容逐字恢复作为契约。
- 不提供运行中 GPU 卸载命令，也不承诺让已经加载库的进程自动切换驱动。

## 专用 VM 验收清单

1. 记录宿主机和容器发行版、systemd、Toolkit、驱动、CUDA Sample 版本，GPU UUID、SELinux 和用户映射。
2. 注入前记录 leader PID、既有进程 PID、设备/驱动状态，确认容器不能使用该 GPU。
3. 执行 `plan`，确认无状态修改。
4. 执行 `attach`，确认 leader PID 和既有进程不变。
5. 执行 `verify`，保存普通用户 `nvidia-smi`、真实 CUDA 结果、动态加载证据和退出状态。
6. 重复 `attach`，核对无重复挂载，再次 `verify`。
7. 在隔离 fixture 中覆盖冲突资源、拒绝访问及部分失败清理；不在共享容器中制造故障。

## 已完成的实机验收

| 项目 | 实测环境 |
|---|---|
| 宿主机 | openSUSE Leap 16.0，kernel 6.12.0-160000.40-default，systemd 257.13 |
| GPU / 驱动 | Tesla P4 / 580.196.07 |
| NVIDIA Container Toolkit | 1.20.2 |
| 容器 | Ubuntu Base 24.04.5，标准 `systemd-nspawn@` 模板，`DevicePolicy=closed`，启用 `-U` 用户映射 |
| CUDA / Sample | nvcc 12.6.85，`sm_61`；CUDA Samples v12.8，提交 `db3eea23946bca2e90a75eca2b5b3e07158a9e11` |
| 安全状态 | SELinux permissive，未关闭用户命名空间、未扩大宿主机设备 DAC |

- 注入前，普通用户没有 NVIDIA 设备和 `nvidia-smi`，真实 `vectorAdd` 失败。
- 注入后，UID 1000 的普通用户查询到选定 GPU；`vectorAdd` 执行 50,000 元素向量加法并报告 `Test PASSED`，成功退出。
- NVML 和 libcuda 的实际加载路径均解析到注入的 580.196.07 版本化库。
- 重复 `attach` 前后，容器挂载数保持 95；leader PID 和注入前的常驻 sentinel 进程均不变。
- 从目标宿主机 unit 撤销 GPU `DeviceAllow` 后，同一验证入口在 `nvidia-smi` 阶段因 NVML 初始化失败而被拒绝；恢复后再次完整验证通过。
- 不依赖项目执行工作器，使用容器自己的 systemd 启动普通用户服务，配置对应补充组后，`nvidia-smi` 和 `vectorAdd` 同样成功；未设置 `LD_LIBRARY_PATH`。
- 测试期间的挂载/hook 失败覆盖了部分变更清理路径；发现并修复了 namespace 路径失效、跨 namespace bind 源和 D-Bus DeviceAllow 追加语义问题。

测试 VM 上保留 `nvidia-nspawn-test` 容器、Sample 和源码副本；原始 JSON 证据位于测试用户的 `~/nvidia-nspawn-test/results/`。该容器的测试组名为 `nvidia-gpu`（485）和 `nvidia-render`（488）。

## 本地检查

```bash
python3 -m unittest discover -s tests -v
```

单元测试只约束本项目的 CDI 合并、资源冲突、权限合并、用户映射、挂载路径解析与 GPU 输出证据解析，不模拟 GPU 成功、不作为 VM 验收替代。
