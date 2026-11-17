# nvidia-nspawn

为**已经运行的 systemd-nspawn 容器**注入 NVIDIA 设备和用户态驱动。宿主机侧 Python CLI，复用 NVIDIA Container Toolkit 的 CDI 描述；不重启容器，不安装容器内核驱动。

需求契约见 [SPEC.md](SPEC.md)。

## 当前验证状态

已实现 `plan`、`attach`、`status`、`verify`，本地 **52 个测试通过**，并在 openSUSE Leap 16.0 和 Fedora 45 专用 GPU VM 上完成在线注入、普通用户 CUDA、重复注入和设备拒绝负向验收。

Fedora 实测保留 SELinux enforcing 和 Secure Boot。标准 nspawn 模板的实际进程域为 `unconfined_service_t`；这不代表已验证自定义 `container_t` 等受限域。

## 前提

宿主机：

- Linux、cgroup v2、运行中的 systemd-nspawn 容器，以及提供 `open_tree`、`move_mount`、`mount_setattr` 的内核和 glibc。
- Python 3.11+、`machinectl`、`busctl`。
- NVIDIA 专有驱动已经加载，宿主机 `nvidia-smi` 正常。
- NVIDIA Container Toolkit，提供 `nvidia-ctk cdi generate --format=json --device-name-strategy=uuid` 及生成描述所引用的 hooks。
- 启用 SELinux 时，需要 `libselinux.so.1`。工具不关闭 SELinux。
- 以宿主机 root 执行，而非在目标容器里执行。

容器：

- 必须有独立用户命名空间，且 UID/GID 映射均不得包含宿主机 ID 0；标准 nspawn `-U` 配置符合此要求。共享宿主用户命名空间、关闭用户映射或映射宿主 root 的目标在变更前被拒绝。

- 容器中有 `ldconfig`、`setpriv`、`id`、`readlink`，以及已有普通用户。
- 尚未安装或注入冲突的 NVIDIA 驱动文件。
- 已准备真实 NVIDIA CUDA Samples `vectorAdd` 可执行文件及其 runtime 依赖，CUDA 版本与 GPU/宿主驱动兼容。
- CUDA 编译器无需长期留在容器中。`nvidia-smi` 的 CUDA version 字段不代表已安装 CUDA Toolkit。

首次验收建议使用只有一张可见 GPU 的专用 VM 和可丢弃容器，保留实际 SELinux、用户命名空间配置，不通过关闭隔离来制造成功。

### Fedora SELinux 根目录标签

从 tar 自行展开的容器根目录可能继承 `var_lib_t`，不能直接作为 SELinux 域转换的可执行入口。应在注入前对独立 rootfs 补齐标准文件标签，例如：

```bash
root=/var/lib/machines/gpu-test
sudo setfiles -r "$root" /etc/selinux/targeted/contexts/files/file_contexts "$root"
```

这里只重标记容器的磁盘目录，不新增 SELinux 放行策略。不要对已经包含宿主机绑定挂载的 `/proc/LEADER/root` 递归重标记，以免修改宿主机驱动文件的标签。

## 使用

可以在源码目录直接运行，不需要安装 Python 依赖：

```bash
python3 -m nvidia_nspawn --help
```

也可以通过 Python 包管理工具安装项目，获得 `nvidia-nspawn` 命令。构建使用 setuptools；运行时仅使用 Python 标准库和上述宿主机工具。

RPM 同时安装系统管理员手册，可通过 `man 8 nvidia-nspawn` 阅读完整参数、示例、退出码、状态文件和安全边界。手册源文件为 `man/nvidia-nspawn.8`；源码目录中也可运行 `man -l man/nvidia-nspawn.8` 预览。

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

- 使用 `open_tree` 在宿主机克隆 detached mount，在公开之前设置 private、只读及 CDI 要求的安全属性，再用 `move_mount` 注入目标现有挂载命名空间；`rbind` 的子挂载递归设置限制，保留容器自身 glibc 和动态链接器。
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

验证子进程进入目标 cgroup 和 namespaces，并在启用 SELinux 时通过容器自己的 `setpriv` 入口进入目标域，再允许程序正常发生 SELinux 类型转换，避免宿主机权限绕过或强制载荷入口域造成错误。CDI 环境变量及 `additionalGids` 只应用于工具启动的验证进程，不修改现有 shell、用户的持久组成员或宿主机设备权限。正常使用 `nvidia-smi` 不依赖临时 `LD_LIBRARY_PATH`；从其他入口启动应用时，管理员仍需为该用户或服务配置对应 GPU 组。

支持已核实的 `vectorAdd` 输出契约，包括历史版本的 `Test PASSED` 和新版统一内存示例的结果匹配标记。源码版本、构建 CUDA 版本、架构参数需由 Sample 提供者另行记录；输出标记不能证明任意二进制的来源。

## 生命周期和失败处理

- 不自动重启容器或重载宿主驱动。
- 不配置 `ExecStartPost` 或开机自动注入，也不永久修改 nspawn unit。每次目标容器启动或重启后，管理员需先检查残留，再手工执行 `attach`；仅添加自动调用不能解决旧资源冲突。
- `DeviceAllow` 通过 `runtime=true` 更新，systemd 在 `/run/systemd/system.control/` 下记录运行时 drop-in。容器重启不保证撤销这份授权；宿主机重启后该运行时配置不持久保留。
- 运行时挂载不承诺跨容器重启存在，容器文件系统上的配置和占位文件可能保留。重启后先检查 `status` 与新计划；遇到旧的托管文件冲突，人工核实并清理后再注入，不强行覆盖。
- 驱动升级不做热更新，旧驱动版本记录会导致健康检查或验证失败。
- 失败时尝试撤销本次创建的挂载、节点、配置和设备授权，保留失败 journal；不删除原先已有的用户文件。
- 外部命令超时后，先终止并回收受监督的工作进程及后代，再开始失败清理；逐条记录已完成的挂载和文件变更。内核不可中断睡眠可能延长回收等待，超时不是保证立即返回。中断发生在文件操作与日志写入之间时，只报告无法确认的残留候选，不盲删既有资源，也不声称原子回滚。
- 若清理失败、容器实例变化或挂载仍在使用，返回原始错误及残留信息，不假称已回滚。
- `ldconfig` 更新的缓存属于派生数据，回滚后重新运行；不把缓存内容逐字恢复作为契约。
- 不提供运行中 GPU 卸载命令，也不承诺让已经加载库的进程自动切换驱动。

### 卸载 RPM 不等于撤销 GPU

卸载只移除 RPM 拥有的 CLI、Python 模块、文档和 man page；不会自动 detach、撤销 `DeviceAllow` 或清理容器磁盘。运行中的 GPU 挂载、设备节点、符号链接、动态链接器配置/缓存、宿主机 `/run/nvidia-nspawn/` 记录及 systemd 运行时授权可能继续存在，容器仍能访问 GPU。

两版专用 VM 已实测卸载期间普通用户 CUDA 继续成功，重新安装后可继续检查状态。该行为避免打断正在运行的容器，但不能将卸载当作权限回收手段。当前没有自动清理或 detach 命令；需要回收访问权限时，管理员必须另行安排维护并核实容器和授权的实际状态，不能仅删除运行记录。

## 专用 VM 验收清单

1. 记录宿主机和容器发行版、systemd、Toolkit、驱动、CUDA Sample 版本，GPU UUID、SELinux 和用户映射。
2. 注入前记录 leader PID、既有进程 PID、设备/驱动状态，确认容器不能使用该 GPU。
3. 执行 `plan`，确认无状态修改。
4. 执行 `attach`，确认 leader PID 和既有进程不变。
5. 执行 `verify`，保存普通用户 `nvidia-smi`、真实 CUDA 结果、动态加载证据和退出状态。
6. 重复 `attach`，核对无重复挂载，再次 `verify`。
7. 在隔离 fixture 中覆盖冲突资源、拒绝访问及部分失败清理；不在共享容器中制造故障。

## 已完成的实机验收

| 项目 | openSUSE VM | Fedora VM |
|---|---|---|
| 宿主机 | Leap 16.0，kernel 6.12.0-160000.40-default，systemd 257.13 | Fedora 45，kernel 7.3.3-300.fc45.x86_64，systemd 262.1 |
| GPU / 驱动 | Tesla P4 / 580.196.07 | Tesla P4 / RPM Fusion 580xx 580.196.07 |
| NVIDIA Container Toolkit | 1.20.2 | 1.20.2 |
| 容器 | Ubuntu Base 24.04.5，标准 `systemd-nspawn@` 模板，`DevicePolicy=closed`，启用 `-U` 用户映射 | 相同配置 |
| CUDA / Sample | nvcc 12.6.85，`sm_61`；CUDA Samples v12.8，提交 `db3eea23946bca2e90a75eca2b5b3e07158a9e11` | 复用同一已校验二进制，未安装编译器 |
| 安全状态 | SELinux permissive | SELinux enforcing；本机签名模块经 MOK 登记，Secure Boot 保持开启 |

- 注入前，普通用户没有 NVIDIA 设备和 `nvidia-smi`，真实 `vectorAdd` 失败。
- 注入后，UID 1000 的普通用户查询到选定 GPU；`vectorAdd` 执行 50,000 元素向量加法并报告 `Test PASSED`，成功退出。
- NVML 和 libcuda 的实际加载路径均解析到注入的 580.196.07 版本化库。
- 重复 `attach` 前后，容器挂载数保持 95；leader PID 和注入前的常驻 sentinel 进程均不变。
- 从目标宿主机 unit 撤销 GPU `DeviceAllow` 后，同一验证入口在 `nvidia-smi` 阶段因 NVML 初始化失败而被拒绝；恢复后再次完整验证通过。
- 不依赖项目执行工作器，使用容器自己的 systemd 启动普通用户服务，配置对应补充组后，`nvidia-smi` 和 `vectorAdd` 同样成功；未设置 `LD_LIBRARY_PATH`。
- 测试期间的挂载/hook 失败覆盖了部分变更清理路径；发现并修复了 namespace 路径失效、跨 namespace bind 源和 D-Bus DeviceAllow 追加语义问题。
- Fedora 验收另覆盖了 SELinux 文件标签与执行入口：补齐 rootfs 标签，并让 root 命令先通过 `setpriv` 进入目标域，保留 `ldconfig` 等程序的正常类型转换。失败后的设备授权、挂载和文件清理得到实测，未新增自定义 SELinux 策略。

两台测试 VM 均保留 `nvidia-nspawn-test` 容器、Sample 和源码副本；原始证据位于测试用户的 `~/nvidia-nspawn-test/results/`。openSUSE 容器的测试组为 `nvidia-gpu`（485）和 `nvidia-render`（488）；Fedora 原生普通用户服务配置补充 GID 39（该 Ubuntu rootfs 中的组名为 `irc`），未修改持久用户组成员。

## RPM 打包

采用 [MIT 许可证](LICENSE)。`packaging/nvidia-nspawn.spec` 共用源码，在 Fedora 45 和 openSUSE Leap 16.0 上分别构建 `noarch` RPM；Python ABI 和系统依赖仍由各发行版处理，不能混装两版产物。

### 构建依赖

Fedora：

```bash
sudo dnf install rpm-build python3-devel pyproject-rpm-macros \
  python3-pip python3-setuptools python3-wheel
```

openSUSE Leap 16.0（默认 Python 3.13）：

```bash
sudo zypper install rpm-build python-rpm-macros python3-devel \
  python313-pip python313-setuptools python313-wheel
```

构建后端要求 `setuptools >= 77`，用于 SPDX `MIT` 和许可证文件元数据；这是构建依赖，不是运行依赖。RPM 的 wheel 构建禁用联网依赖解析，使用预先安装的发行版依赖。

### 构建与安装

在目标发行版的源码目录执行：

```bash
python3 -c 'from setuptools.build_meta import build_sdist; build_sdist("dist")'
mkdir -p ~/rpmbuild/SOURCES
cp dist/nvidia_nspawn-0.1.0.tar.gz ~/rpmbuild/SOURCES/
rpmbuild -ba packaging/nvidia-nspawn.spec
```

二进制包在 `~/rpmbuild/RPMS/noarch/`，源码 RPM 在 `~/rpmbuild/SRPMS/`。`%check` 对构建根目录中的 Python 包运行算法测试，不需要 GPU。

```bash
# 示例：Fedora 45；Leap 16.0 对应文件名为 nvidia-nspawn-0.1.0-4.noarch.rpm
# --nosignature 仅用于自己构建的未签名包，不跳过摘要或依赖检查
sudo rpm -Uvh --nosignature ~/rpmbuild/RPMS/noarch/nvidia-nspawn-0.1.0-4.fc45.noarch.rpm
nvidia-nspawn --help
```

本地构建产物默认未签名；正式分发前应签名并正常验证。Fedora 的 RPM 6 默认拒绝未签名包，上面的参数只作用于本次安装，不修改全局校验策略。RPM 不包含安装/卸载脚本，不会安装或切换驱动、登记 MOK、重启容器或修改 SELinux 策略。安装前仍须准备可用的 NVIDIA 驱动、NVML、libcuda 和 `nvidia-smi`，并配置能提供 `nvidia-container-toolkit-base >= 1.20.2` 的可信仓库。

实际入口安装在发行版的 `%{_libexecdir}/nvidia-nspawn/cli`，`/usr/bin/nvidia-nspawn` 是指向它的符号链接。这样沿用标准 SELinux 文件标签，避免 Fedora 将普通 `/usr/bin/nvidia.*` 文件误归为 X server 入口，不需要额外放行策略。

### 运行依赖与边界

| 依赖 | Fedora | openSUSE Leap |
|---|---|---|
| Python ≥ 3.11 | `python3` | `python3-base` |
| 容器与 D-Bus CLI | `systemd`、`systemd-container` | 同左 |
| 系统总线 | `dbus` | `dbus-1` |
| libc 挂载接口 | `glibc >= 2.36` | 同左 |
| SELinux 动态库 | `libselinux` | `libselinux1` |
| CDI 生成与 hooks | `nvidia-container-toolkit-base >= 1.20.2` | 同左 |

内核需提供 `mount_setattr` 等接口（API 下限 Linux 5.12）、cgroup v2 和所需权限；安装某个版本的内核 RPM 不能保证当前启动内核满足要求。GPU 驱动分支由管理员按硬件选择，不固定为 P4 使用的 R580。

宿主 RPM 不向容器安装软件；容器中的 `ldconfig`、`setpriv`、`id`、`readlink`、普通用户和验证用 CUDA Sample 仍需提前准备。不要将 Toolkit、驱动库、Sample、缓存或 `/run/nvidia-nspawn` 的运行状态打入项目 RPM。

## 本地检查

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖本项目的 CDI 合并、资源冲突、权限合并、用户映射、挂载路径解析、GPU 输出证据及进度恢复算法，并以真实本地 fork/超时验证后代进程回收。不模拟 GPU 成功、不作为 VM 验收替代。
