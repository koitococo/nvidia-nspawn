Name:           nvidia-nspawn
Version:        0.1.2
Release:        1%{?dist}
Summary:        Live NVIDIA GPU injection for systemd-nspawn containers
License:        MIT
Source0:        nvidia_nspawn-%{version}.tar.gz
BuildArch:      noarch

BuildRequires:  python3-devel
%if 0%{?suse_version}
Group:          System/Management
BuildRequires:  python-rpm-macros
BuildRequires:  %{python_module pip}
BuildRequires:  %{python_module setuptools >= 77}
BuildRequires:  %{python_module wheel}
Requires:       python3-base >= 3.11
Requires:       libselinux1
Requires:       dbus-1
%else
BuildRequires:  pyproject-rpm-macros
BuildRequires:  python3-pip
BuildRequires:  python3-setuptools >= 77
BuildRequires:  python3-wheel
Requires:       python3 >= 3.11
Requires:       libselinux
Requires:       dbus
%endif
Requires:       glibc >= 2.36
Requires:       systemd
Requires:       systemd-container
Requires:       nvidia-container-toolkit-base >= 1.20.2

%description
Inject NVIDIA devices and userspace driver files into an already running
systemd-nspawn container using NVIDIA Container Toolkit CDI descriptions.
Verify real GPU access and CUDA computation as an ordinary container user.

The host must already have a working NVIDIA driver, NVML, libcuda and
nvidia-smi. Run as host root with cgroup v2 and the required namespace and
mount permissions. This package does not install or switch GPU drivers,
restart containers, or change SELinux policy or Secure Boot settings.

%prep
%autosetup -n nvidia_nspawn-%{version}

%build
export PIP_NO_INDEX=1
%pyproject_wheel

%install
%pyproject_install
# Fedora labels regular /usr/bin/nvidia.* files as X server entrypoints.
# Keep the public command as a symlink to an ordinary libexec entrypoint.
install -d %{buildroot}%{_libexecdir}/%{name}
mv %{buildroot}%{_bindir}/nvidia-nspawn %{buildroot}%{_libexecdir}/%{name}/cli
ln -s %{_libexecdir}/%{name}/cli %{buildroot}%{_bindir}/nvidia-nspawn
install -D -m 0644 man/nvidia-nspawn.8 %{buildroot}%{_mandir}/man8/nvidia-nspawn.8

%check
PYTHONPATH=%{buildroot}%{python3_sitelib} %{__python3} -P -m unittest discover -s tests -v

%files
%license LICENSE
%doc README.md SPEC.md
%{_bindir}/nvidia-nspawn
%{_libexecdir}/%{name}/
%{python3_sitelib}/nvidia_nspawn/
%{python3_sitelib}/nvidia_nspawn-%{version}.dist-info/
%{_mandir}/man8/nvidia-nspawn.8*

%changelog
* Tue Nov 17 2026 Koi to Coco <koitococo@outlook.com> - 0.1.2-1
- Persist resource ownership and safely reattach after container or host restart.
- Ignore directory timestamp changes during temporary hook cleanup.

* Tue Nov 17 2026 Koi to Coco <koitococo@outlook.com> - 0.1.1-1
- Execute commands and hooks through guest systemd for shared user namespaces.

* Tue Nov 17 2026 Koi to Coco <koitococo@outlook.com> - 0.1.0-4
- Restrict user mappings, secure detached mounts, and supervise worker cleanup.
- Document manual startup and non-revoking package removal.

* Tue Nov 17 2026 Koi to Coco <koitococo@outlook.com> - 0.1.0-3
- Install the administrator manual in section 8.

* Tue Nov 17 2026 Koi to Coco <koitococo@outlook.com> - 0.1.0-2
- Preserve the default SELinux domain with a libexec entrypoint.

* Tue Nov 17 2026 Koi to Coco <koitococo@outlook.com> - 0.1.0-1
- Package the MIT-licensed CLI for Fedora and openSUSE Leap.
